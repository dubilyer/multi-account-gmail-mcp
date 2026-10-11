"""Account routing, atomic metadata, and Keychain-backed OAuth credentials."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterator

from google.auth.exceptions import GoogleAuthError, RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_httplib2 import AuthorizedHttp
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from httplib2 import Http

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly", "https://www.googleapis.com/auth/gmail.compose"]
APP = "multy-acc-gmail-mcp"


class GmailError(Exception):
    """An actionable error whose message is safe to return to an MCP client."""


@dataclass(frozen=True)
class Account:
    email: str
    alias: str

    @property
    def id(self) -> str:
        return hashlib.sha256(self.email.encode()).hexdigest()[:20]


def data_dir() -> Path:
    override = os.environ.get("MULTY_GMAIL_DATA_DIR")
    return Path(override).expanduser() if override else Path.home() / "Library/Application Support" / APP


@contextmanager
def file_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(descriptor, "a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


class Registry:
    def __init__(self, root: Path | None = None):
        self.root = root if root is not None else data_dir()
        self.path = self.root / "accounts.json"

    def list(self) -> list[Account]:
        if not self.path.exists():
            return []
        try:
            rows = json.loads(self.path.read_text())["accounts"]
            accounts = [Account(email=row["email"], alias=row["alias"]) for row in rows]
            if len({a.email for a in accounts}) != len(accounts):
                raise ValueError("Duplicate account")
            return accounts
        except (KeyError, ValueError, TypeError) as error:
            raise GmailError(f"Invalid account registry at {self.path}; restore or repair it.") from error

    def resolve(self, selector: str) -> Account:
        matches = [a for a in self.list() if selector.casefold() in (a.email.casefold(), a.alias.casefold())]
        if len(matches) == 1:
            return matches[0]
        if matches:
            raise GmailError("Ambiguous account; use its full email address.")
        raise GmailError(f"Unknown account '{selector}'. Use list_accounts to choose a connected mailbox.")

    def _write(self, accounts: list[Account]) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary = tempfile.mkstemp(prefix=".accounts-", dir=self.root)
        try:
            with os.fdopen(descriptor, "w") as stream:
                json.dump({"version": 1, "accounts": [asdict(a) for a in accounts]}, stream, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def save(self, account: Account, store, credentials: str) -> None:
        with file_lock(self.root / ".registry.lock"):
            accounts = self.list()
            if any(
                a.email != account.email and a.alias.casefold() == account.alias.casefold() for a in accounts
            ):
                raise GmailError(f"Alias '{account.alias}' already belongs to another account.")
            store.set(account.email, credentials)
            self._write([a for a in accounts if a.email != account.email] + [account])

    def remove(self, selector: str, store) -> Account:
        account = self.resolve(selector)
        with file_lock(self.root / f".account-{account.id}.lock"):
            with file_lock(self.root / ".registry.lock"):
                store.delete(account.email)
                self._write([a for a in self.list() if a.email != account.email])
        return account


class KeychainStore:
    """Explicit macOS backend: never fall back to a plaintext keyring."""

    def __init__(self):
        self._backend = None

    @property
    def backend(self):
        if self._backend is None:
            if sys.platform != "darwin":
                raise GmailError("This version requires macOS Keychain for credential storage.")
            from keyring.backends.macOS import Keyring

            self._backend = Keyring()
        return self._backend

    def get(self, email: str) -> str | None:
        try:
            return self.backend.get_password(APP, email)
        except GmailError:
            raise
        except Exception as error:
            raise GmailError("Cannot read macOS Keychain. Unlock it and retry.") from error

    def set(self, email: str, value: str) -> None:
        try:
            self.backend.set_password(APP, email, value)
        except GmailError:
            raise
        except Exception as error:
            raise GmailError("Cannot save credentials to macOS Keychain.") from error

    def delete(self, email: str) -> None:
        if self.get(email) is not None:
            try:
                self.backend.delete_password(APP, email)
            except Exception as error:
                raise GmailError("Cannot remove credentials from macOS Keychain.") from error


class AccountManager:
    def __init__(self, registry=None, store=None):
        self.registry = registry if registry is not None else Registry()
        self.store = store if store is not None else KeychainStore()

    def list_accounts(self) -> list[dict]:
        result = []
        for account in self.registry.list():
            try:
                raw = self.store.get(account.email)
                if raw is None:
                    status = "sign_in_required"
                else:
                    credentials = Credentials.from_authorized_user_info(json.loads(raw))
                    status = (
                        "connected"
                        if credentials.valid
                        else ("refresh_available" if credentials.refresh_token else "sign_in_required")
                    )
            except GmailError:
                status = "keychain_unavailable"
            except (ValueError, KeyError, TypeError):
                status = "sign_in_required"
            result.append({**asdict(account), "account_id": account.id, "auth_status": status})
        return result

    def credentials(self, selector: str) -> tuple[Account, Credentials]:
        account = self.registry.resolve(selector)
        with file_lock(self.registry.root / f".account-{account.id}.lock"):
            account = self.registry.resolve(account.email)
            raw = self.store.get(account.email)
            if raw is None:
                raise GmailError(
                    f"Sign in again with: multy-acc-gmail-mcp accounts add --alias {account.alias}"
                )
            try:
                credentials = Credentials.from_authorized_user_info(json.loads(raw))
                if not credentials.has_scopes(SCOPES):
                    raise GmailError(
                        f"Account {account.alias} needs read-and-draft authorization; add it again."
                    )
                if not credentials.valid:
                    if not credentials.refresh_token:
                        raise GmailError(f"Account {account.alias} needs to sign in again.")
                    credentials.refresh(Request())
                    self.store.set(account.email, credentials.to_json())
            except RefreshError as error:
                raise GmailError(
                    f"Authorization expired or was revoked for {account.alias}; add it again."
                ) from error
            except GoogleAuthError as error:
                raise GmailError(
                    f"Cannot refresh {account.alias}; check your connection and retry."
                ) from error
            except (ValueError, KeyError, TypeError) as error:
                raise GmailError(f"Invalid stored credentials for {account.alias}; add it again.") from error
            return account, credentials

    @contextmanager
    def service(self, selector: str):
        account, credentials = self.credentials(selector)
        # A new HTTP client per call; no shared httplib2 transport between threads.
        service = build(
            "gmail", "v1", http=AuthorizedHttp(credentials, http=Http(timeout=30)), cache_discovery=False
        )
        try:
            yield account, service
        finally:
            service.close()

    def authorize(self, alias: str, client_secret: Path | None = None, expected_email: str | None = None):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", alias):
            raise GmailError("Alias must contain 1–64 letters, digits, underscores, or hyphens.")
        config_path = self.registry.root / "oauth-client-path.txt"
        if client_secret is None:
            if config_path.exists():
                remembered_path = config_path.read_text().strip()
                if not remembered_path:
                    raise GmailError(
                        f"Empty remembered OAuth client path at {config_path}. "
                        "Supply --client-secret /path/to/downloaded-desktop-client.json."
                    )
                client_secret = Path(remembered_path)
            else:
                client_secret = Path.home() / ".multi-acc-gmail-mcp" / "client_secret.json"
        client_secret = client_secret.expanduser().resolve()
        try:
            config = json.loads(client_secret.read_text())
        except (OSError, ValueError) as error:
            raise GmailError(
                f"Could not read OAuth client JSON at {client_secret}. "
                "Create a Desktop app client in Google Auth platform > Clients and download its JSON "
                "to this path, or supply --client-secret /path/to/downloaded-desktop-client.json."
            ) from error
        if not isinstance(config, dict) or not isinstance(config.get("installed"), dict):
            raise GmailError(
                f"Invalid OAuth client at {client_secret}. Use a Google OAuth client of type Desktop app: "
                "create it in Google Auth platform > Clients and download its JSON as-is."
            )
        try:
            flow = InstalledAppFlow.from_client_config(config, SCOPES, autogenerate_code_verifier=True)
        except (KeyError, TypeError, ValueError) as error:
            raise GmailError(
                f"Invalid Desktop OAuth client at {client_secret}. "
                "Download its JSON again from Google Auth platform > Clients."
            ) from error
        try:
            credentials = flow.run_local_server(
                port=0, prompt="consent select_account", access_type="offline", timeout_seconds=180
            )
        except (OSError, ValueError) as error:
            raise GmailError(
                "Could not load the OAuth client or complete login. Check the client file."
            ) from error
        if credentials is None or not credentials.refresh_token:
            raise GmailError("Google did not return offline credentials. Repeat authorization with consent.")
        service = build("gmail", "v1", credentials=credentials, cache_discovery=False)
        try:
            email = service.users().getProfile(userId="me").execute()["emailAddress"].casefold()
        finally:
            service.close()
        if expected_email and email != expected_email.casefold():
            raise GmailError(f"Signed in as {email}, not {expected_email}. No account was saved.")
        account = Account(email=email, alias=alias)
        with file_lock(self.registry.root / f".account-{account.id}.lock"):
            self.registry.save(account, self.store, credentials.to_json())
        config_path.write_text(str(client_secret) + "\n")
        config_path.chmod(0o600)
        return account
