"""Local account enrollment and MCP startup. OAuth is never initiated by a tool call."""

import argparse
import json
import logging
import sys
from pathlib import Path

from google.auth.exceptions import GoogleAuthError
from googleapiclient.errors import HttpError

from .accounts import AccountManager, GmailError


def main():
    parser = argparse.ArgumentParser(description="Multi-account Workspace Gmail MCP (local, read + draft).")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve", help="Run the MCP server on stdio")
    commands.add_parser("mcp-config", help="Print a client configuration for this Python environment")
    accounts = commands.add_parser("accounts", help="Manage local account connections")
    actions = accounts.add_subparsers(dest="action", required=True)
    add = actions.add_parser("add", help="Authorize a mailbox and store credentials in macOS Keychain")
    add.add_argument("--alias", required=True, help="Short account name, e.g. work or finance")
    add.add_argument(
        "--client-secret",
        type=Path,
        help="Desktop OAuth client JSON; defaults to remembered path, then "
        "~/.multi-acc-gmail-mcp/client_secret.json",
    )
    add.add_argument("--email", help="Expected mailbox; refuse to save a different signed-in account")
    actions.add_parser("list", help="List accounts and authentication status without fetching mail")
    remove = actions.add_parser(
        "remove", help="Remove local credentials; does not revoke Google's OAuth grant"
    )
    remove.add_argument("account", help="Email address or alias")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    try:
        if args.command == "serve":
            from .server import create_server

            create_server().run(transport="stdio")
        elif args.command == "mcp-config":
            print(
                json.dumps(
                    {
                        "mcpServers": {
                            "multy-acc-gmail-mcp": {
                                "command": sys.executable,
                                "args": ["-m", "multy_acc_gmail_mcp", "serve"],
                            }
                        }
                    },
                    indent=2,
                )
            )
        else:
            manager = AccountManager()
            if args.action == "add":
                account = manager.authorize(args.alias, args.client_secret, args.email)
                print(
                    f"Connected {account.email} as '{account.alias}'. Credentials stored in macOS Keychain."
                )
            elif args.action == "list":
                print(json.dumps({"accounts": manager.list_accounts()}, indent=2))
            else:
                account = manager.registry.remove(args.account, manager.store)
                print(f"Disconnected {account.email} locally. Google's OAuth grant was not revoked.")
    except GmailError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except (HttpError, GoogleAuthError):
        print(
            "Error: Google authorization failed. Check the OAuth client, consent, and Workspace access.",
            file=sys.stderr,
        )
        return 1
    except OSError as error:
        print(f"Error: local file or network operation failed ({type(error).__name__}).", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0
