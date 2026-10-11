import base64
from email import message_from_bytes
from email.policy import default

import pytest
from googleapiclient.errors import HttpError
from httplib2 import Response

from multy_acc_gmail_mcp.accounts import GmailError
from multy_acc_gmail_mcp.gmail import decode_data, safe_filename, save_attachment


def encoded(text):
    data = text.encode() if isinstance(text, str) else text
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def message(payload=None):
    return {
        "id": "abc123",
        "threadId": "thread123",
        "internalDate": "1000",
        "payload": payload
        or {
            "mimeType": "text/plain",
            "body": {"data": encoded("Hello")},
            "headers": [
                {"name": "Subject", "value": "Test"},
                {"name": "From", "value": "sender@example.org"},
            ],
        },
    }


def service_for(manager, email="alice@example.org"):
    return manager.services[email]


def test_search_returns_account_identity_and_pagination(gmail, manager):
    for index, service in enumerate(manager.services.values()):
        service.users().messages().list().execute.return_value = {
            "messages": [{"id": "abc123"}],
            "nextPageToken": f"p{index}",
        }
        service.users().messages().get().execute.return_value = message()
    result = gmail.search_messages("is:unread")
    assert [bucket["account"] for bucket in result["accounts"]] == [
        "alice@example.org",
        "finance@example.org",
    ]
    assert result["accounts"][0]["messages"][0]["account"] == "alice@example.org"
    assert result["accounts"][1]["next_page_token"] == "p1"
    gmail.search_messages("is:unread", account="finance", page_tokens={"finance@example.org": "p1"})
    assert (
        service_for(manager, "finance@example.org").users().messages().list.call_args.kwargs["pageToken"]
        == "p1"
    )
    with pytest.raises(GmailError, match="page_tokens"):
        gmail.search_messages("test", "work", page_tokens={"finance@example.org": "wrong"})


def test_partial_auth_failure_preserves_other_results(gmail, manager):
    manager.failures["alice@example.org"] = "Sign in again"
    service_for(manager, "finance@example.org").users().messages().list().execute.return_value = {}
    buckets = gmail.search_messages("invoice")["accounts"]
    assert buckets[0]["error"] == "Sign in again"
    assert buckets[1]["error"] is None


def test_nested_mime_body_and_attachment_refs(gmail, manager):
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            {
                "mimeType": "multipart/alternative",
                "parts": [
                    {"mimeType": "text/plain", "body": {"data": encoded("Hello world")}},
                    {"mimeType": "text/html", "body": {"data": encoded("<b>Hello world</b>")}},
                ],
            },
            {
                "mimeType": "application/pdf",
                "filename": "invoice.pdf",
                "body": {"attachmentId": "remote", "size": 4},
            },
        ],
    }
    service_for(manager).users().messages().get().execute.return_value = message(payload)
    result = gmail.read_message("work", "abc123", max_body_chars=5)
    assert result["body"] == "Hello" and result["body_truncated"]
    assert result["attachments"][0]["attachment_ref"] == "0.1"
    assert result["content_trust"] == "untrusted_email_content"
    assert manager.requested[-1] == "alice@example.org"


def test_html_only_and_separately_stored_body(gmail, manager):
    payload = {"mimeType": "text/html", "body": {"attachmentId": "body-id"}}
    service = service_for(manager)
    service.users().messages().get().execute.return_value = message(payload)
    service.users().messages().attachments().get().execute.return_value = {
        "data": encoded("<p>Hello &amp; world</p><script>bad()</script>")
    }
    result = gmail.read_message("work", "abc123")
    assert result["body"] == "Hello & world"
    assert "bad()" not in result["body"]


def test_thread_messages_are_chronological(gmail, manager):
    older, newer = message(), message()
    older["id"], newer["id"] = "old", "new"
    newer["internalDate"] = "2000"
    service_for(manager).users().threads().get().execute.return_value = {"messages": [newer, older]}
    assert [m["message_id"] for m in gmail.read_thread("work", "thread123")["messages"]] == ["old", "new"]


@pytest.mark.parametrize("inline", [False, True])
def test_download_attachment_and_preserve_existing_file(gmail, manager, inline):
    content = b"%PDF test"
    part = {
        "mimeType": "application/pdf",
        "filename": "../../invoice.pdf",
        "body": {"data": encoded(content)} if inline else {"attachmentId": "remote"},
    }
    service = service_for(manager)
    service.users().messages().get().execute.return_value = message(
        {"mimeType": "multipart/mixed", "parts": [part]}
    )
    service.users().messages().attachments().get().execute.return_value = {"data": encoded(content)}
    first = gmail.download_attachment("work", "abc123", "0.0")
    second = gmail.download_attachment("work", "abc123", "0.0")
    from pathlib import Path

    assert Path(first["path"]).read_bytes() == content
    assert first["size"] == len(content)
    assert first["filename"] == "invoice.pdf" and second["filename"] == "invoice-1.pdf"
    assert Path(first["path"]).stat().st_mode & 0o777 == 0o600


def test_attachment_directory_symlink_is_refused(tmp_path):
    root, elsewhere = tmp_path / "downloads", tmp_path / "elsewhere"
    root.mkdir()
    elsewhere.mkdir()
    (root / "account").symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(GmailError, match="symbolic links"):
        save_attachment(root, "account", "abc123", "file.pdf", b"data")
    assert not list(elsewhere.iterdir())


def test_invalid_attachment_and_message_refs_do_not_write(gmail, manager):
    service_for(manager).users().messages().get().execute.return_value = message()
    with pytest.raises(GmailError, match="Unknown attachment_ref"):
        gmail.download_attachment("work", "abc123", "0")
    with pytest.raises(GmailError, match="message ID"):
        gmail.download_attachment("work", "../outside", "0")
    assert not gmail.download_root.exists()


def test_new_draft_is_utf8_and_never_sends(gmail, manager):
    service = service_for(manager)
    service.users().drafts().create().execute.return_value = {
        "id": "draft123",
        "message": {"id": "message123"},
    }
    result = gmail.create_draft("work", "Bob <bob@example.org>", "שלום", "Hello שלום", cc="cc@example.org")
    created = service.users().drafts().create.call_args.kwargs["body"]["message"]
    parsed = message_from_bytes(base64.urlsafe_b64decode(created["raw"]), policy=default)
    assert parsed["From"] == "alice@example.org"
    assert parsed["Subject"] == "שלום"
    assert "שלום" in parsed.get_content()
    assert result["sent"] is False
    service.users().messages().send.assert_not_called()
    service.users().drafts().send.assert_not_called()
    assert service.users().drafts().create().execute.call_args.kwargs == {}


def test_reply_draft_keeps_thread_and_headers(gmail, manager):
    service = service_for(manager)
    original = message(
        {
            "headers": [
                {"name": "Subject", "value": "Invoice"},
                {"name": "From", "value": "sender@example.org"},
                {"name": "Reply-To", "value": "reply@example.org"},
                {"name": "Message-ID", "value": "<original@example.org>"},
                {"name": "References", "value": "<previous@example.org>"},
            ]
        }
    )
    service.users().messages().get().execute.return_value = original
    service.users().drafts().create().execute.return_value = {
        "id": "draft123",
        "message": {"threadId": "thread123"},
    }
    gmail.create_draft("work", None, None, "Reply", reply_to_message_id="abc123")
    created = service.users().drafts().create.call_args.kwargs["body"]["message"]
    parsed = message_from_bytes(base64.urlsafe_b64decode(created["raw"]), policy=default)
    assert created["threadId"] == "thread123"
    assert parsed["To"] == "reply@example.org"
    assert parsed["Subject"] == "Invoice"
    assert parsed["In-Reply-To"] == "<original@example.org>"
    assert parsed["References"] == "<previous@example.org> <original@example.org>"


@pytest.mark.parametrize(
    "to,subject", [("a@example.org\r\nBcc: attacker@example.org", "ok"), ("bob@example.org", "bad\nsubject")]
)
def test_draft_header_injection_is_refused_before_network(gmail, manager, to, subject):
    with pytest.raises(GmailError, match="newlines"):
        gmail.create_draft("work", to, subject, "body")
    assert not manager.requested


def test_http_error_is_actionable_and_does_not_expose_body(gmail, manager):
    error = HttpError(Response({"status": "403"}), b'{"error":{"message":"sensitive-server-detail"}}')
    service_for(manager).users().messages().get().execute.side_effect = error
    with pytest.raises(GmailError, match="HTTP 403") as caught:
        gmail.read_message("work", "abc123")
    assert "sensitive-server-detail" not in str(caught.value)


def test_filename_and_base64_edge_cases():
    assert safe_filename("..\\..\\invoice.pdf", "application/pdf", "0.1") == "invoice.pdf"
    assert safe_filename("\x00...", "image/png", "0.1") == "attachment-0-1.png"
    with pytest.raises(GmailError, match="encoding"):
        decode_data("not!!base64")


def test_inline_plaintext_is_body_not_attachment(gmail, manager):
    payload = {
        "mimeType": "text/plain",
        "headers": [{"name": "Content-Disposition", "value": "inline"}],
        "body": {"data": encoded("Normal inline body")},
    }
    service_for(manager).users().messages().get().execute.return_value = message(payload)
    result = gmail.read_message("work", "abc123")
    assert result["body"] == "Normal inline body"
    assert result["attachments"] == []


def test_network_failure_preserves_other_account(gmail, manager):
    from google.auth.exceptions import TransportError

    service_for(manager).users().messages().list().execute.side_effect = TransportError("network unavailable")
    service_for(manager, "finance@example.org").users().messages().list().execute.return_value = {}
    result = gmail.search_messages("invoice")["accounts"]
    assert "retry" in result[0]["error"]
    assert result[1]["error"] is None
