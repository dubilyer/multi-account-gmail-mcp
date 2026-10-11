"""Small Gmail facade: explicit identities, MIME decoding, and draft-only writes."""

from __future__ import annotations

import base64
import binascii
import mimetypes
import os
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import getaddresses
from html.parser import HTMLParser
from pathlib import Path

from google.auth.exceptions import GoogleAuthError
from googleapiclient.errors import HttpError
from httplib2 import HttpLib2Error

from .accounts import AccountManager, GmailError


class TextFromHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1
        elif not self.hidden and tag in ("br", "p", "div", "li", "tr"):
            self.text.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.text.append(data)


def decode_data(encoded: str) -> bytes:
    try:
        return base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
    except (ValueError, binascii.Error) as error:
        raise GmailError("Gmail returned invalid attachment/body encoding.") from error


def parts(payload: dict, path="0"):
    yield path, payload
    for index, child in enumerate(payload.get("parts", [])):
        yield from parts(child, f"{path}.{index}")


def headers(part: dict) -> dict[str, str]:
    return {header["name"].casefold(): header["value"] for header in part.get("headers", [])}


def is_attachment(part: dict) -> bool:
    mime = part.get("mimeType", "")
    disposition = headers(part).get("content-disposition", "").casefold()
    if part.get("filename") or disposition.startswith("attachment"):
        return True
    return bool(mime and mime not in ("text/plain", "text/html") and not mime.startswith("multipart/"))


def attachment_metadata(payload: dict) -> list[dict]:
    result = []
    for path, part in parts(payload):
        if is_attachment(part):
            result.append(
                {
                    "attachment_ref": path,
                    "filename": part.get("filename", ""),
                    "mime_type": part.get("mimeType", "application/octet-stream"),
                    "size": part.get("body", {}).get("size"),
                    "inline": headers(part).get("content-disposition", "").casefold().startswith("inline"),
                }
            )
    return result


def safe_filename(filename: str, mime: str, reference: str) -> str:
    filename = filename.replace("\\", "/").split("/")[-1]
    filename = "".join(c for c in filename if not unicodedata.category(c).startswith("C"))
    filename = filename.strip(" .")[:160]
    if not filename:
        filename = "attachment-" + reference.replace(".", "-") + (mimetypes.guess_extension(mime) or ".bin")
    return filename


def save_attachment(root: Path, account_id: str, message_id: str, filename: str, data: bytes) -> Path:
    """Open directories without following links, and never replace an existing file."""
    root = root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptors = []
    try:
        descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        descriptors.append(descriptor)
        for component in (account_id, message_id):
            if not re.fullmatch(r"[A-Za-z0-9_-]+", component):
                raise GmailError("Invalid attachment destination identifier.")
            try:
                os.mkdir(component, mode=0o700, dir_fd=descriptor)
            except FileExistsError:
                pass
            descriptor = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            descriptors.append(descriptor)
        stem, suffix = os.path.splitext(filename)
        for index in range(10000):
            candidate = filename if index == 0 else f"{stem}-{index}{suffix}"
            try:
                file_descriptor = os.open(
                    candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600, dir_fd=descriptor
                )
            except FileExistsError:
                continue
            try:
                with os.fdopen(file_descriptor, "wb") as stream:
                    stream.write(data)
            except Exception:
                os.unlink(candidate, dir_fd=descriptor)
                raise
            return root / account_id / message_id / candidate
        raise GmailError("Too many files with this attachment name.")
    except OSError as error:
        raise GmailError(
            "Cannot save the attachment; check directory permissions and symbolic links."
        ) from error
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def validate_id(value: str, label: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", value):
        raise GmailError(f"Invalid {label}.")


def validate_header(value: str, label: str):
    if any(character in value for character in ("\r", "\n", "\x00")):
        raise GmailError(f"{label} must not contain newlines or null characters.")


class Gmail:
    def __init__(self, manager=None, download_root: Path | None = None):
        self.manager = manager if manager is not None else AccountManager()
        configured = os.environ.get("MULTY_GMAIL_DOWNLOAD_DIR")
        self.download_root = download_root or (
            Path(configured).expanduser() if configured else Path.home() / "Downloads/multy-acc-gmail-mcp"
        )

    @contextmanager
    def service(self, account: str):
        try:
            with self.manager.service(account) as pair:
                yield pair
        except HttpError as error:
            status = error.resp.status
            hint = {
                401: "Reconnect this account.",
                403: "Check OAuth scopes and Workspace admin access.",
                404: "Message, thread, or attachment was not found in this account.",
                429: "Gmail rate limit reached; retry later.",
            }.get(status, "Retry the request later.")
            raise GmailError(f"Gmail API HTTP {status}. {hint}") from error
        except (TimeoutError, ConnectionError, OSError, GoogleAuthError, HttpLib2Error) as error:
            raise GmailError(
                "Cannot reach Gmail or the local credential store; retry the request."
            ) from error

    @staticmethod
    def _body_data(service, message_id: str, part: dict) -> bytes:
        body = part.get("body", {})
        if "data" in body:
            return decode_data(body["data"])
        if body.get("attachmentId"):
            response = (
                service.users()
                .messages()
                .attachments()
                .get(userId="me", messageId=message_id, id=body["attachmentId"])
                .execute(num_retries=2)
            )
            return decode_data(response.get("data", ""))
        return b""

    def _message(self, account, service, message: dict, max_body_chars: int) -> dict:
        payload = message.get("payload", {})
        text, html = [], []
        for _, part in parts(payload):
            mime = part.get("mimeType", "")
            if part.get("parts") or is_attachment(part) or mime not in ("text/plain", "text/html"):
                continue
            content_type = headers(part).get("content-type", "")
            charset = re.search(r'charset=["\']?([^;"\'\s]+)', content_type, re.I)
            encoding = charset.group(1) if charset else "utf-8"
            data = self._body_data(service, message["id"], part)
            try:
                decoded = data.decode(encoding, errors="replace")
            except LookupError:
                decoded = data.decode("utf-8", errors="replace")
            (text if mime == "text/plain" else html).append(decoded)
        content = "\n".join(text)
        if not text:
            parser = TextFromHTML()
            parser.feed("\n".join(html))
            content = "".join(parser.text).strip()
        all_headers = headers(payload)
        return {
            "account": account.email,
            "alias": account.alias,
            "message_id": message["id"],
            "thread_id": message.get("threadId"),
            "headers": all_headers,
            "label_ids": message.get("labelIds", []),
            "snippet": message.get("snippet", ""),
            "body": content[:max_body_chars],
            "body_truncated": len(content) > max_body_chars,
            "attachments": attachment_metadata(payload),
            "content_trust": "untrusted_email_content",
        }

    def search_messages(
        self,
        query: str,
        account: str | None = None,
        max_results: int = 20,
        page_tokens: dict[str, str] | None = None,
    ) -> dict:
        if not query.strip():
            raise GmailError("Provide a nonempty Gmail search query.")
        if not 1 <= max_results <= 100:
            raise GmailError("max_results must be between 1 and 100 per account.")
        accounts = [self.manager.registry.resolve(account)] if account else self.manager.registry.list()
        tokens = page_tokens or {}
        if any(key not in {a.email for a in accounts} for key in tokens):
            raise GmailError("page_tokens must be keyed by the selected account email addresses.")

        def search(selected):
            try:
                with self.service(selected.email) as (identity, service):
                    response = (
                        service.users()
                        .messages()
                        .list(
                            userId="me", q=query, maxResults=max_results, pageToken=tokens.get(selected.email)
                        )
                        .execute(num_retries=2)
                    )
                    summaries = []
                    for reference in response.get("messages", []):
                        message = (
                            service.users()
                            .messages()
                            .get(
                                userId="me",
                                id=reference["id"],
                                format="metadata",
                                metadataHeaders=["From", "To", "Subject", "Date"],
                            )
                            .execute(num_retries=2)
                        )
                        summaries.append(
                            {
                                "account": identity.email,
                                "alias": identity.alias,
                                "message_id": message["id"],
                                "thread_id": message.get("threadId"),
                                "headers": headers(message.get("payload", {})),
                                "snippet": message.get("snippet", ""),
                            }
                        )
                    return {
                        "account": identity.email,
                        "alias": identity.alias,
                        "messages": summaries,
                        "next_page_token": response.get("nextPageToken"),
                        "error": None,
                    }
            except GmailError as error:
                return {
                    "account": selected.email,
                    "alias": selected.alias,
                    "messages": [],
                    "next_page_token": None,
                    "error": str(error),
                }

        with ThreadPoolExecutor(max_workers=min(4, max(1, len(accounts)))) as executor:
            results = list(executor.map(search, accounts))
        return {"accounts": results, "content_trust": "untrusted_email_content"}

    def read_message(self, account: str, message_id: str, max_body_chars: int = 20000) -> dict:
        validate_id(message_id, "message ID")
        if not 1 <= max_body_chars <= 200000:
            raise GmailError("max_body_chars must be between 1 and 200000.")
        with self.service(account) as (identity, service):
            message = (
                service.users()
                .messages()
                .get(userId="me", id=message_id, format="full")
                .execute(num_retries=2)
            )
            return self._message(identity, service, message, max_body_chars)

    def read_thread(self, account: str, thread_id: str, max_body_chars: int = 20000) -> dict:
        validate_id(thread_id, "thread ID")
        if not 1 <= max_body_chars <= 200000:
            raise GmailError("max_body_chars must be between 1 and 200000 per message.")
        with self.service(account) as (identity, service):
            thread = (
                service.users().threads().get(userId="me", id=thread_id, format="full").execute(num_retries=2)
            )
            messages = sorted(thread.get("messages", []), key=lambda m: int(m.get("internalDate", 0)))
            return {
                "account": identity.email,
                "thread_id": thread_id,
                "messages": [self._message(identity, service, m, max_body_chars) for m in messages[:100]],
                "messages_truncated": len(messages) > 100,
                "content_trust": "untrusted_email_content",
            }

    def download_attachment(self, account: str, message_id: str, attachment_ref: str) -> dict:
        validate_id(message_id, "message ID")
        with self.service(account) as (identity, service):
            message = (
                service.users()
                .messages()
                .get(userId="me", id=message_id, format="full")
                .execute(num_retries=2)
            )
            part = next(
                (part for path, part in parts(message.get("payload", {})) if path == attachment_ref), None
            )
            if part is None or not is_attachment(part):
                raise GmailError(
                    "Unknown attachment_ref; choose one returned by read_message or read_thread."
                )
            body = part.get("body", {})
            if "data" not in body and not body.get("attachmentId") and body.get("size", 0) != 0:
                raise GmailError("Gmail did not provide downloadable data for this attachment.")
            data = self._body_data(service, message_id, part)
            mime = part.get("mimeType", "application/octet-stream")
            filename = safe_filename(part.get("filename", ""), mime, attachment_ref)
            path = save_attachment(self.download_root, identity.id, message_id, filename, data)
            return {
                "account": identity.email,
                "message_id": message_id,
                "attachment_ref": attachment_ref,
                "path": str(path),
                "filename": path.name,
                "size": len(data),
                "mime_type": mime,
            }

    def create_draft(
        self,
        account: str,
        to: str | None,
        subject: str | None,
        body: str,
        cc: str | None = None,
        bcc: str | None = None,
        reply_to_message_id: str | None = None,
    ) -> dict:
        for label, value in (("To", to), ("Subject", subject), ("Cc", cc), ("Bcc", bcc)):
            if value is not None:
                validate_header(value, label)
        if reply_to_message_id:
            validate_id(reply_to_message_id, "reply message ID")
        with self.service(account) as (identity, service):
            reply_headers = {}
            thread_id = None
            if reply_to_message_id:
                original = (
                    service.users()
                    .messages()
                    .get(
                        userId="me",
                        id=reply_to_message_id,
                        format="metadata",
                        metadataHeaders=["From", "Reply-To", "Subject", "Message-ID", "References"],
                    )
                    .execute(num_retries=2)
                )
                reply_headers = headers(original.get("payload", {}))
                thread_id = original["threadId"]
                # Preserve the original subject to satisfy Gmail's threading requirements.
                original_subject = reply_headers.get("subject", "")
                if subject is not None and subject != original_subject:
                    raise GmailError("Reply draft subject must match the original thread subject.")
                subject = original_subject
                to = to or reply_headers.get("reply-to") or reply_headers.get("from")
            if not to or subject is None:
                raise GmailError("New drafts require recipients and a subject.")
            for label, value in (("To", to), ("Subject", subject), ("Cc", cc), ("Bcc", bcc)):
                if value is not None:
                    validate_header(value, label)
                    if label != "Subject":
                        addresses = getaddresses([value])
                        if not addresses or any(
                            not re.fullmatch(r"[^\s@<>]+@[^\s@<>]+", email) for _, email in addresses
                        ):
                            raise GmailError(f"Invalid {label} recipient list.")
            draft = EmailMessage(policy=SMTP)
            draft["From"] = identity.email
            draft["To"] = to
            draft["Subject"] = subject
            if cc:
                draft["Cc"] = cc
            if bcc:
                draft["Bcc"] = bcc
            if reply_to_message_id:
                original_id = reply_headers.get("message-id")
                if not original_id:
                    raise GmailError(
                        "Original message has no Message-ID; cannot reliably create a threaded reply."
                    )
                validate_header(original_id, "Message-ID")
                references = reply_headers.get("references", "")
                validate_header(references, "References")
                draft["In-Reply-To"] = original_id
                draft["References"] = (references + " " + original_id).strip()
            draft.set_content(body, charset="utf-8")
            message = {"raw": base64.urlsafe_b64encode(draft.as_bytes()).decode()}
            if thread_id:
                message["threadId"] = thread_id
            # No automatic retry: a timeout after creation could otherwise duplicate drafts.
            result = service.users().drafts().create(userId="me", body={"message": message}).execute()
            return {
                "account": identity.email,
                "draft_id": result["id"],
                "message_id": result.get("message", {}).get("id"),
                "thread_id": result.get("message", {}).get("threadId"),
                "sent": False,
            }
