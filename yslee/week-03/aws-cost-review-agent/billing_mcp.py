"""One read-only Billing MCP connection, with exact-name tool discovery."""

import os
import shutil
from pathlib import Path
from typing import Any

from mcp import StdioServerParameters, stdio_client
from strands.tools.mcp import MCPClient

BILLING_MCP_COMMAND = ("uvx", "awslabs.billing-cost-management-mcp-server@latest")
ALLOWED_TOOLS = frozenset({"cost-comparison", "cost-optimization"})


class BillingMCPError(RuntimeError):
    """The requested Billing MCP tools cannot be used safely."""


def server_environment(profile: str | None = None, region: str | None = None) -> dict[str, str]:
    """Inherit the shell environment, overriding only explicit server settings."""
    env = os.environ.copy()
    if profile:
        env["AWS_PROFILE"] = profile
    env["AWS_REGION"] = region or env.get("AWS_REGION") or "us-east-1"
    env["FASTMCP_LOG_LEVEL"] = "ERROR"
    return env


def make_billing_client(profile: str | None = None, region: str | None = None) -> MCPClient:
    """Create the official Strands stdio MCP client without starting it."""
    env = server_environment(profile, region)
    # A project-local uv install is usable without changing the requested command.
    project_bin = str(Path(__file__).resolve().parent / ".venv" / "bin")
    env["PATH"] = os.pathsep.join((project_bin, env.get("PATH", "")))
    env.setdefault("UV_CACHE_DIR", str(Path(__file__).resolve().parent / ".venv" / "uv-cache"))
    if shutil.which(BILLING_MCP_COMMAND[0], path=env["PATH"]) is None:
        raise BillingMCPError("uvx를 찾을 수 없습니다. uv를 설치하거나 --mock으로 오프라인 실행하세요.")
    params = StdioServerParameters(
        command=BILLING_MCP_COMMAND[0],
        args=[BILLING_MCP_COMMAND[1]],
        env=env,
    )
    return MCPClient(
        lambda: stdio_client(params),
        tool_filters={"allowed": sorted(ALLOWED_TOOLS)},
        startup_timeout=60,
    )


def _all_tool_pages(client: MCPClient, *, unfiltered: bool) -> list[Any]:
    """Walk all MCP tool pages; discovery overrides the constructor filter only here."""
    found: list[Any] = []
    token: str | None = None
    seen_tokens: set[str] = set()
    while True:
        page = client.list_tools_sync(pagination_token=token, tool_filters={} if unfiltered else None)
        found.extend(page)
        token = page.pagination_token
        if token is None:
            return found
        if token in seen_tokens:
            raise BillingMCPError("MCP tool discovery에서 반복된 pagination token을 받았습니다.")
        seen_tokens.add(token)


def discover_billing_tools(client: MCPClient) -> tuple[list[str], list[Any]]:
    """Validate server-side names, then retrieve only constructor-filtered tools."""
    discovered = sorted({tool.mcp_tool.name for tool in _all_tool_pages(client, unfiltered=True)})
    missing = ALLOWED_TOOLS.difference(discovered)
    if missing:
        raise BillingMCPError(
            f"필수 Billing MCP tool이 없습니다: {', '.join(sorted(missing))}. "
            f"발견된 tool names: {', '.join(discovered) or '(없음)'}"
        )

    selected = _all_tool_pages(client, unfiltered=False)
    exposed = [tool.tool_name for tool in selected]
    if len(selected) != len(ALLOWED_TOOLS) or set(exposed) != ALLOWED_TOOLS:
        raise BillingMCPError(
            f"tool filter 결과가 허용 목록과 다릅니다. "
            f"발견된 tool names: {', '.join(discovered)}; 노출 후보: {', '.join(exposed) or '(없음)'}"
        )
    return discovered, selected
