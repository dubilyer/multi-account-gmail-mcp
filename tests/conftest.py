from contextlib import contextmanager
from unittest.mock import Mock

import pytest

from multy_acc_gmail_mcp.accounts import Account, AccountManager, GmailError, Registry
from multy_acc_gmail_mcp.gmail import Gmail


class MemoryStore:
    def __init__(self):
        self.values = {}

    def get(self, email):
        return self.values.get(email)

    def set(self, email, value):
        self.values[email] = value

    def delete(self, email):
        self.values.pop(email, None)


class FakeManager:
    def __init__(self, root):
        self.registry = Registry(root)
        self.accounts = [Account("alice@example.org", "work"), Account("finance@example.org", "finance")]
        self.registry._write(self.accounts)
        self.services = {account.email: Mock() for account in self.accounts}
        self.failures = {}
        self.requested = []

    def list_accounts(self):
        return [{"email": a.email, "alias": a.alias} for a in self.accounts]

    @contextmanager
    def service(self, selector):
        account = self.registry.resolve(selector)
        self.requested.append(account.email)
        if account.email in self.failures:
            raise GmailError(self.failures[account.email])
        yield account, self.services[account.email]


@pytest.fixture
def manager(tmp_path):
    return FakeManager(tmp_path / "state")


@pytest.fixture
def gmail(manager, tmp_path):
    return Gmail(manager, tmp_path / "downloads")


@pytest.fixture
def account_manager(tmp_path):
    registry = Registry(tmp_path / "state")
    return AccountManager(registry, MemoryStore())
