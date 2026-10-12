# Multi-account Gmail MCP server

A local Gmail MCP server for AI assistants such as Claude Desktop, Claude Code, and Cursor.
Search multiple Gmail inboxes, read email threads, download attachments, and create reply drafts across
Google Workspace accounts from your Mac. Connect your mailboxes once, then use natural-language
requests through an MCP client.

Looking for a Gmail MCP integration for ChatGPT? See [ChatGPT connection requirements](#chatgpt-connection-requirements)
for the additional connection setup needed for this local server.

The package and command are named `multy-acc-gmail-mcp`. Built with Google's official Gmail API client
and the official Model Context Protocol (MCP) Python SDK.

- **Search multiple accounts:** find messages across connected inboxes with Gmail search syntax.
- **Read conversations and attachments:** retrieve messages, threads, and attachment references; save
  attachments locally.
- **Create drafts from the right account:** compose new messages or threaded replies, then review and
  send them in Gmail.
- **Use your own OAuth client:** each user or Workspace organization controls its Google Cloud project
  and mailbox authorization.
- **Store tokens in macOS Keychain:** run the server locally with separate credentials for each mailbox.

## Example requests

Once connected to your MCP client, try prompts such as:

- "Find PDF invoices from the last month across all my connected inboxes."
- "Read the latest thread about the contract in my work account."
- "Download the invoice attached to this message from my finance account."
- "Create a reply draft in my work account thanking the sender and asking for a revised quote."

Use the aliases you assigned during setup. Drafts are saved in Gmail for you to review and send.

## Privacy and permissions

The server runs on your Mac and connects to the Gmail API using your OAuth client. Tokens are stored in
macOS Keychain; account metadata and downloaded attachments are stored locally. Email content returned
to your MCP client may be sent to its model provider according to that client's settings and policies.

The requested scopes are `gmail.readonly` and `gmail.compose`. The compose scope grants draft management
**and sending** at Google's API layer. This server exposes draft creation only; it has no sending,
deletion, archiving, or label-editing tool.

## Quick start

Requirements: macOS, Python 3.12+, [uv](https://docs.astral.sh/uv/), an MCP client that supports local
stdio servers, and a Google Cloud project owned by your Google Workspace organization. The setup below
targets mailboxes in one Workspace organization.

Clone the repository and install the dependencies:

```sh
git clone https://github.com/dubilyer/multi-account-gmail-mcp.git
cd multi-account-gmail-mcp
uv sync --locked
```

### 1. Create your own Internal OAuth client

Each user or Workspace organization supplies their own Google Cloud project and OAuth client. This
project does not distribute shared OAuth credentials. One client can authorize multiple mailboxes
within its Workspace organization.

In the [Google Cloud console](https://console.cloud.google.com/):

1. Select or create a project owned by your Google Workspace organization.
2. Enable the **Gmail API** for that project.
3. Open **Google Auth platform → Branding** ([direct link](https://console.cloud.google.com/auth/branding))
   and enter your app name and support email. If you see **Get started**, complete the setup wizard.
4. Open **Audience** ([direct link](https://console.cloud.google.com/auth/audience)) and choose **Internal**.
5. In **Data Access**, add these scopes:
   - `https://www.googleapis.com/auth/gmail.readonly`
   - `https://www.googleapis.com/auth/gmail.compose`
6. Under **Clients**, create an OAuth client with application type **Desktop app**.
7. Download its JSON and place it at `~/.multi-acc-gmail-mcp/client_secret.json`, outside the repository:

   ```sh
   mkdir -p "$HOME/.multi-acc-gmail-mcp"
   chmod 700 "$HOME/.multi-acc-gmail-mcp"
   cp "$HOME/Downloads/workspace-gmail-client.json" "$HOME/.multi-acc-gmail-mcp/client_secret.json"
   chmod 600 "$HOME/.multi-acc-gmail-mcp/client_secret.json"
   ```

   Replace the source filename with your downloaded file's name. Keep the downloaded JSON contents
   unchanged; a Web application client cannot be converted by renaming its `web` field to `installed`.

The same client authorizes all your Workspace mailboxes, but each mailbox completes consent once. If Internal is unavailable, ensure the Cloud project belongs to the Workspace organization. Workspace administrators can restrict OAuth apps; ask your administrator to allow this client if access is blocked.

No service account or domain-wide delegation is required.

References: [Google credential setup](https://developers.google.com/workspace/guides/create-credentials), [Gmail scopes](https://developers.google.com/workspace/gmail/api/auth/scopes), [Internal OAuth apps](https://developers.google.com/identity/protocols/oauth2/production-readiness/sensitive-scope-verification).

### 2. Authorize each mailbox

Replace the example addresses with your actual Workspace mailboxes:

```sh
uv run --locked multy-acc-gmail-mcp accounts add \
  --alias work --email you@your-company.com

uv run --locked multy-acc-gmail-mcp accounts add \
  --alias finance --email finance@your-company.com

uv run --locked multy-acc-gmail-mcp accounts list
```

Select the matching account in the browser and approve both scopes. `--email` prevents saving the wrong signed-in account.

OAuth client lookup uses an explicit `--client-secret /path/to/client.json` first, then a previously
remembered client path, then `~/.multi-acc-gmail-mcp/client_secret.json`. The successfully used path is
remembered for subsequent enrollments. Keep that file in place; if a remembered file moves, pass
`--client-secret` with its new path. A missing or invalid selected file reports an error rather than
silently switching to another client.

The `~/.multi-acc-gmail-mcp/` folder holds only the OAuth client JSON you provide. Account metadata and
tokens continue to use the locations below.

Tokens are stored in **macOS Keychain** under service `multy-acc-gmail-mcp`, keyed by mailbox email. Account aliases are stored in `~/Library/Application Support/multy-acc-gmail-mcp/accounts.json`. No plaintext token-file fallback is used. macOS may ask to allow Python access to its Keychain item during enrollment or first use.

Aliases use letters, digits, underscores, and hyphens. Re-enroll an existing mailbox to refresh authorization or change its alias. A duplicate alias cannot replace another mailbox's credentials.

To disconnect locally:

```sh
uv run --locked multy-acc-gmail-mcp accounts remove finance
```

This removes the local connection and its Keychain entry. It does not delete downloaded files or revoke Google's authorization grant. Revoke that separately in the Google account's third-party access settings if desired.

### 3. Connect an MCP client

Generate the stdio configuration for the installed environment:

```sh
uv run --locked multy-acc-gmail-mcp mcp-config
```

Copy the generated entry into your MCP client's configuration, then restart or reload the client as
needed. The output includes the absolute Python path for your installation. Its JSON structure is:

```json
{
  "mcpServers": {
    "multy-acc-gmail-mcp": {
      "command": "/absolute/path/to/multi-account-gmail-mcp/.venv/bin/python",
      "args": ["-m", "multy_acc_gmail_mcp", "serve"]
    }
  }
}
```

Use the generated configuration to get your actual Python path; the path above is a placeholder. A
Finder-launched client may not inherit your terminal PATH. The server does not depend on its current
working directory. Configuration formats vary by client; use its instructions for registering a local
stdio MCP server. Nothing is automatically added to your existing clients.

Confirm that your client exposes `list_accounts` and that it returns your connected mailboxes before
trying the example requests above.

For a manual server launch:

```sh
uv run --locked multy-acc-gmail-mcp serve
```

This speaks MCP on stdin/stdout, not a human CLI. Diagnostics go to stderr.

### Connect Gmail to Claude Desktop

After authorizing your mailboxes, run the `mcp-config` command above. Merge its
`multy-acc-gmail-mcp` entry into the `mcpServers` object in:

```text
~/Library/Application Support/Claude/claude_desktop_config.json
```

If the file does not exist, open Claude Desktop once to initialize its configuration directory, then
create the file with the generated JSON. Preserve any existing server entries. Fully quit and reopen
Claude Desktop, then ask it to list your connected Gmail accounts before searching or creating drafts.

### Connect Gmail to Claude Code

Register the server using the absolute Python path returned by `mcp-config`:

```sh
claude mcp add multy-acc-gmail-mcp -- /absolute/path/to/multi-account-gmail-mcp/.venv/bin/python -m multy_acc_gmail_mcp serve
```

Replace the placeholder path with your installation's path. Run `/mcp` inside Claude Code to check the
connection and available tools, then ask it to list your connected accounts.

### Connect Gmail to Cursor

Merge the generated `mcpServers` entry into your project's `.cursor/mcp.json`. Use the absolute Python
path from `mcp-config`, reload the client as needed, and check Cursor's MCP settings for the server and
its tools before trying the example requests.

These instructions follow the clients' local stdio configuration formats; they are not a claim of
end-to-end testing in every client. See the [MCP SDK's client setup guide](https://py.sdk.modelcontextprotocol.io/get-started/real-host/)
for configuration details.

### ChatGPT connection requirements

ChatGPT's custom MCP connection requires a reachable HTTPS MCP endpoint or OpenAI's Secure MCP Tunnel.
The generated `mcpServers` JSON above configures a local stdio client; it cannot be pasted directly into
ChatGPT as a server connection.

This project provides a local stdio server. Connecting it to ChatGPT requires additional infrastructure,
such as Secure MCP Tunnel configured to launch the generated Python command, plus the necessary
account and workspace access. This repository does not include or test that integration. Follow
[OpenAI's ChatGPT connection guide](https://developers.openai.com/plugins/deploy/connect-chatgpt)
and [Secure MCP Tunnel setup](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)
for current requirements.

## Tools

| Tool | Arguments | Result |
|---|---|---|
| `list_accounts` | None | Connected emails, aliases, account IDs, local authentication status |
| `search_messages` | `query`, optional `account`, `max_results=20`, `page_tokens` | Per-account message summaries, pagination tokens, independent errors |
| `read_message` | `account`, `message_id`, `max_body_chars=20000` | Headers, decoded text, labels, and attachment references |
| `read_thread` | `account`, `thread_id`, `max_body_chars=20000` | Up to 100 messages in chronological order |
| `create_draft` | `account`, `body`, optional `to`, `subject`, `cc`, `bcc`, `reply_to_message_id` | Draft/message/thread IDs and `sent: false` |
| `download_attachment` | `account`, `message_id`, `attachment_ref` | Absolute local path, filename, byte size, and MIME type |

All tools return structured JSON plus a text representation. Account-specific operations require an explicit alias or email. Omitting `account` is allowed only for search, where it means every connected mailbox. Unknown accounts are errors, never a fallback to another account.

### Search and pagination

`query` uses normal Gmail syntax, e.g. `has:attachment filename:pdf newer_than:30d`. `max_results` applies **per account**, from 1 to 100.

Search returns an `accounts` array. Each entry contains `account`, `alias`, `messages`, `next_page_token`, and `error`. Every message also identifies its account. One account's authorization/network failure does not discard the others' results.

For the next page of one inbox, pass its token under its canonical email and select that inbox:

```json
{
  "query": "has:attachment",
  "account": "work",
  "page_tokens": {"you@your-company.com": "TOKEN_FROM_PREVIOUS_RESULT"}
}
```

For a continued cross-account search, include the tokens for each selected mailbox. Accounts without tokens start from page one; narrow the search to a single account when only that mailbox has more results.

### Reading and downloading attachments

Use `read_message` or `read_thread` to discover attachment references. For example:

```json
{"account": "work", "message_id": "MESSAGE_ID", "attachment_ref": "0.1"}
```

Attachment references identify MIME parts within **that message**, not global attachment IDs. Downloads handle both inline data and Gmail's separate attachment endpoint.

Files are saved beneath:

```text
~/Downloads/multy-acc-gmail-mcp/<account-id>/<message-id>/<filename>
```

Filenames are sanitized, files are created with private permissions, and duplicate names receive numeric suffixes. Subdirectory symbolic links are refused. Downloads are never opened or executed. Some MCP clients cannot access local files automatically; the returned path is usable on the same Mac.

Bodies prefer plaintext; HTML-only messages are converted to text without scripts/styles. Bodies default to 20,000 characters per message, adjustable to 200,000. Responses explicitly report truncation. Email content, filenames, and snippets are marked as untrusted data.

### Drafts

For a new draft, supply `account`, `to`, `subject`, and `body`. Bodies are UTF-8 plain text; attachment uploads and HTML composition are not included.

For a reply draft, supply `reply_to_message_id`. Omitted recipients come from Reply-To/From; the original subject and threading headers are preserved. A changed reply subject is rejected to avoid silently breaking Gmail's thread association.

The selected account determines From; there is no sender override. Draft creation is not automatically retried, because a timeout after Google creates a draft could otherwise produce duplicates. Review drafts in Gmail and send them yourself.

## Configuration

Optional environment variables, set in the MCP client's `env` configuration:

| Variable | Default |
|---|---|
| `MULTY_GMAIL_DATA_DIR` | `~/Library/Application Support/multy-acc-gmail-mcp` |
| `MULTY_GMAIL_DOWNLOAD_DIR` | `~/Downloads/multy-acc-gmail-mcp` |

Keep data directories separate from your repository. Tokens always use Keychain; changing the metadata directory does not create a separate credential vault.

`list_accounts` checks local token state, not live Google authorization. An expired access token with a refresh token reports `refresh_available` and is refreshed when a Gmail tool is called. Revoked credentials produce a reconnect error. Token refresh is serialized per account, including across multiple local server processes. Each request uses a separate HTTP client.

## Development and verification

```sh
uv sync --locked
uv run --locked pytest -q
uv run --locked ruff check src tests
uv run --locked ruff format --check src tests
```

Tests use fake Gmail services and an in-memory credential store. They do not access real mail, Keychain credentials, or OAuth consent. Coverage includes account routing, refresh isolation/concurrency, OAuth identity checks, MIME bodies, attachment downloads/path safety, drafts, partial failures, and a real stdio MCP handshake.

After authorization, verify two accounts with `list_accounts`, search a known message across both inboxes, download a known attachment, and create a test draft. Real-mail verification requires your OAuth client and browser consent; the automated test suite alone does not verify your organization's access policies.
