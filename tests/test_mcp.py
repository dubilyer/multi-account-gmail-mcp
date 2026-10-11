import sys

from mcp import Client
from mcp.client.stdio import StdioServerParameters

from multy_acc_gmail_mcp.server import create_server

EXPECTED = {
    "list_accounts",
    "search_messages",
    "read_message",
    "read_thread",
    "create_draft",
    "download_attachment",
}


async def test_tools_and_error_routing_over_mcp(gmail):
    async with Client(create_server(gmail)) as client:
        tools = await client.list_tools()
        assert {tool.name for tool in tools.tools} == EXPECTED
        result = await client.call_tool("list_accounts", {})
        assert result.structured_content["accounts"][0]["email"] == "alice@example.org"
        result = await client.call_tool("read_message", {"account": "unknown", "message_id": "abc123"})
        assert result.is_error
        assert "Unknown account" in result.content[0].text


async def test_real_stdio_handshake_and_empty_account_list(tmp_path):
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "multy_acc_gmail_mcp", "serve"],
        env={"MULTY_GMAIL_DATA_DIR": str(tmp_path / "empty-registry")},
    )
    async with Client(parameters) as client:
        tools = await client.list_tools()
        assert {tool.name for tool in tools.tools} == EXPECTED
        result = await client.call_tool("list_accounts", {})
        assert not result.is_error
        assert result.structured_content == {"accounts": []}
