import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import pytest
from google.auth.exceptions import RefreshError

from multy_acc_gmail_mcp.accounts import SCOPES, Account, GmailError, KeychainStore


def credential_json(expired=False):
    return json.dumps(
        {
            "token": "test-token",
            "refresh_token": "test-refresh",
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_id": "test-client",
            "client_secret": "test-secret",
            "scopes": SCOPES,
            "expiry": (datetime.now(timezone.utc) + timedelta(hours=-1 if expired else 1))
            .isoformat()
            .replace("+00:00", "Z"),
        }
    )


def connect(manager, email="alice@example.org", alias="work", expired=False):
    account = Account(email, alias)
    manager.registry.save(account, manager.store, credential_json(expired))
    return account


def test_alias_and_email_route_without_fallback(account_manager):
    alice = connect(account_manager)
    finance = connect(account_manager, "finance@example.org", "finance")
    assert account_manager.registry.resolve("WORK") == alice
    assert account_manager.registry.resolve("finance@example.org") == finance
    with pytest.raises(GmailError, match="Unknown account"):
        account_manager.registry.resolve("unknown")


def test_alias_collision_does_not_replace_tokens(account_manager):
    connect(account_manager)
    before = dict(account_manager.store.values)
    with pytest.raises(GmailError, match="already belongs"):
        connect(account_manager, "finance@example.org", "WORK")
    assert account_manager.store.values == before


def test_registry_has_no_credentials_and_private_permissions(account_manager):
    connect(account_manager)
    metadata = account_manager.registry.path.read_text()
    assert "test-token" not in metadata and "test-secret" not in metadata
    assert account_manager.registry.path.stat().st_mode & 0o777 == 0o600
    assert account_manager.list_accounts()[0]["auth_status"] == "connected"


def test_refresh_is_per_account_and_serialized(account_manager):
    connect(account_manager, expired=True)
    connect(account_manager, "finance@example.org", "finance")
    original_finance = account_manager.store.values["finance@example.org"]

    def refresh(credentials, request):
        credentials.token = "new-token"
        credentials.expiry = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)

    with patch(
        "google.oauth2.credentials.Credentials.refresh", autospec=True, side_effect=refresh
    ) as refresh_call:
        with ThreadPoolExecutor(max_workers=3) as executor:
            results = list(executor.map(lambda _: account_manager.credentials("work"), range(3)))
    assert refresh_call.call_count == 1
    assert all(result[1].token == "new-token" for result in results)
    assert account_manager.store.values["finance@example.org"] == original_finance


def test_revoked_credentials_request_reauthorization(account_manager):
    connect(account_manager, expired=True)
    with patch(
        "google.oauth2.credentials.Credentials.refresh", side_effect=RefreshError("do not expose tokens")
    ):
        with pytest.raises(GmailError, match="revoked") as error:
            account_manager.credentials("work")
    assert "do not expose tokens" not in str(error.value)


def test_missing_credentials_and_removal(account_manager):
    account = connect(account_manager)
    account_manager.store.delete(account.email)
    assert account_manager.list_accounts()[0]["auth_status"] == "sign_in_required"
    with pytest.raises(GmailError, match="Sign in again"):
        account_manager.credentials("work")
    account_manager.registry.remove("work", account_manager.store)
    assert account_manager.registry.list() == []


def test_keychain_does_not_select_plaintext_backend():
    with patch("multy_acc_gmail_mcp.accounts.sys.platform", "linux"):
        with pytest.raises(GmailError, match="macOS Keychain"):
            KeychainStore().get("alice@example.org")


def test_each_service_gets_its_own_transport(account_manager):
    connect(account_manager)
    with patch("multy_acc_gmail_mcp.accounts.build", return_value=Mock()) as build:
        with account_manager.service("work"):
            pass
        with account_manager.service("work"):
            pass
        assert build.call_args_list[0].kwargs["http"] is not build.call_args_list[1].kwargs["http"]
        assert build.return_value.close.call_count == 2


def test_oauth_identity_mismatch_saves_nothing(account_manager, tmp_path):
    client = tmp_path / "client.json"
    client.write_text(json.dumps({"installed": {"client_id": "test"}}))
    flow = Mock()
    flow.run_local_server.return_value.refresh_token = "refresh"
    service = Mock()
    service.users().getProfile().execute.return_value = {"emailAddress": "wrong@example.org"}
    with (
        patch("multy_acc_gmail_mcp.accounts.InstalledAppFlow.from_client_config", return_value=flow),
        patch("multy_acc_gmail_mcp.accounts.build", return_value=service),
    ):
        with pytest.raises(GmailError, match="No account was saved"):
            account_manager.authorize("work", client, "alice@example.org")
    assert account_manager.registry.list() == []
    assert account_manager.store.values == {}


@pytest.mark.parametrize("selection", ["default", "remembered", "explicit"])
def test_oauth_client_lookup_and_remembering(account_manager, tmp_path, monkeypatch, selection):
    monkeypatch.setattr("multy_acc_gmail_mcp.accounts.Path.home", lambda: tmp_path)
    default = tmp_path / ".multi-acc-gmail-mcp" / "client_secret.json"
    default.parent.mkdir()
    remembered = tmp_path / "remembered.json"
    explicit = tmp_path / "explicit.json"
    paths = {"default": default, "remembered": remembered, "explicit": explicit}
    for name, path in paths.items():
        path.write_text(json.dumps({"installed": {"client_id": name}}))
    pointer = account_manager.registry.root / "oauth-client-path.txt"
    if selection != "default":
        pointer.parent.mkdir()
        pointer.write_text(str(remembered) + "\n")
    flow = Mock()
    flow.run_local_server.return_value.refresh_token = "refresh"
    authorized_credentials = credential_json()
    flow.run_local_server.return_value.to_json.return_value = authorized_credentials
    service = Mock()
    service.users().getProfile().execute.return_value = {"emailAddress": "alice@example.org"}
    with (
        patch("multy_acc_gmail_mcp.accounts.InstalledAppFlow.from_client_config", return_value=flow) as load,
        patch("multy_acc_gmail_mcp.accounts.build", return_value=service),
    ):
        account = account_manager.authorize(
            "work", explicit if selection == "explicit" else None, "alice@example.org"
        )
    assert load.call_args.args[0]["installed"]["client_id"] == selection
    assert pointer.read_text().strip() == str(paths[selection])
    assert pointer.stat().st_mode & 0o777 == 0o600
    assert account_manager.registry.list() == [account]
    assert account_manager.store.values[account.email] == authorized_credentials


@pytest.mark.parametrize(
    "contents",
    [
        None,
        "{invalid secret-value",
        "[]",
        '{"web": {"client_secret": "secret-value"}}',
        '{"installed": {"client_secret": "secret-value"}}',
    ],
)
def test_invalid_default_oauth_client_is_actionable(account_manager, tmp_path, monkeypatch, contents):
    monkeypatch.setattr("multy_acc_gmail_mcp.accounts.Path.home", lambda: tmp_path)
    client = tmp_path / ".multi-acc-gmail-mcp" / "client_secret.json"
    if contents is not None:
        client.parent.mkdir()
        client.write_text(contents)
    with patch("multy_acc_gmail_mcp.accounts.InstalledAppFlow.run_local_server") as login:
        with pytest.raises(GmailError) as error:
            account_manager.authorize("work")
    assert str(client) in str(error.value)
    assert "Desktop" in str(error.value)
    assert "Google Auth platform > Clients" in str(error.value)
    assert "secret-value" not in str(error.value)
    login.assert_not_called()
    assert account_manager.registry.list() == []
    assert account_manager.store.values == {}


@pytest.mark.parametrize("selection", ["remembered", "explicit"])
def test_missing_selected_oauth_client_does_not_fall_back(account_manager, tmp_path, monkeypatch, selection):
    monkeypatch.setattr("multy_acc_gmail_mcp.accounts.Path.home", lambda: tmp_path)
    default = tmp_path / ".multi-acc-gmail-mcp" / "client_secret.json"
    default.parent.mkdir()
    default.write_text(json.dumps({"installed": {"client_id": "default"}}))
    missing = tmp_path / "missing.json"
    pointer = account_manager.registry.root / "oauth-client-path.txt"
    pointer.parent.mkdir()
    pointer.write_text(str(missing if selection == "remembered" else default))
    with patch("multy_acc_gmail_mcp.accounts.InstalledAppFlow.from_client_config") as load:
        with pytest.raises(GmailError) as error:
            account_manager.authorize("work", missing if selection == "explicit" else None)
    assert str(missing) in str(error.value)
    assert "--client-secret" in str(error.value)
    load.assert_not_called()
