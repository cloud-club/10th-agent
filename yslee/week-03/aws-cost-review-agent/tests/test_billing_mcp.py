"""No-network discovery and fail-closed allow-list tests."""

from types import SimpleNamespace

import pytest

from billing_mcp import ALLOWED_TOOLS, BillingMCPError, discover_billing_tools, server_environment


def tool(name: str):
    return SimpleNamespace(mcp_tool=SimpleNamespace(name=name), tool_name=name)


class Page(list):
    def __init__(self, items, token=None):
        super().__init__(items)
        self.pagination_token = token


class FakeClient:
    def __init__(self, names):
        self.names = names
        self.filters_seen = []

    def list_tools_sync(self, pagination_token=None, tool_filters=None):
        self.filters_seen.append(tool_filters)
        names = self.names[:2] if pagination_token is None else self.names[2:]
        if tool_filters is None:
            names = [name for name in names if name in ALLOWED_TOOLS]
        next_token = "page-2" if pagination_token is None and len(self.names) > 2 else None
        return Page([tool(name) for name in names], next_token)


def test_discovery_validates_unfiltered_names_then_exposes_only_two():
    client = FakeClient(["budgets", "cost-comparison", "cost-optimization"])
    discovered, exposed = discover_billing_tools(client)
    assert discovered == ["budgets", "cost-comparison", "cost-optimization"]
    assert {item.tool_name for item in exposed} == ALLOWED_TOOLS
    assert client.filters_seen == [{}, {}, None, None]


def test_missing_tool_reports_actual_discovered_names():
    with pytest.raises(BillingMCPError, match="발견된 tool names: budgets, cost-comparison"):
        discover_billing_tools(FakeClient(["budgets", "cost-comparison"]))


def test_server_environment_inherits_and_overrides(monkeypatch):
    monkeypatch.setenv("AWS_PROFILE", "from-shell")
    monkeypatch.setenv("AWS_REGION", "ap-northeast-2")
    monkeypatch.setenv("KEEP_ME", "yes")
    env = server_environment(profile="from-cli", region="us-east-1")
    assert env["AWS_PROFILE"] == "from-cli"
    assert env["AWS_REGION"] == "us-east-1"
    assert env["FASTMCP_LOG_LEVEL"] == "ERROR"
    assert env["KEEP_ME"] == "yes"
