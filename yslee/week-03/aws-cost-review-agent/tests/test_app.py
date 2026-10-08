"""CLI validation and connection-lifecycle tests without AWS calls."""

import argparse
from types import SimpleNamespace

import pytest

import app


def test_month_format_and_order_rejected():
    for value in ("2026-8", "2026-13", "2026-00", "2026-08-01"):
        with pytest.raises(argparse.ArgumentTypeError):
            app.parse_month(value)
    with pytest.raises(SystemExit) as exc:
        app.main(["--previous", "2026-08", "--current", "2026-07", "--discover-only"])
    assert exc.value.code == 2


def test_discovery_only_closes_client_without_model_or_cost_calls(monkeypatch, capsys):
    class FakeClient:
        closed = False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.closed = True

    client = FakeClient()
    monkeypatch.setattr(app, "make_billing_client", lambda *args: client)
    monkeypatch.setattr(app, "discover_billing_tools", lambda _: (
        ["cost-comparison", "cost-optimization", "budgets"],
        [SimpleNamespace(tool_name="cost-comparison"), SimpleNamespace(tool_name="cost-optimization")],
    ))
    monkeypatch.setattr(app, "make_agent", lambda *args, **kwargs: SimpleNamespace(
        tool_names=["cost-comparison", "cost-optimization"]
    ))
    assert app.main(["--previous", "2026-07", "--current", "2026-08", "--discover-only"]) == 0
    assert client.closed
    assert "Agent 노출 tool names: cost-comparison, cost-optimization" in capsys.readouterr().err


def test_mock_mode_never_connects_to_mcp_or_model(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(app, "make_billing_client", lambda *args: pytest.fail("MCP must not start"))
    monkeypatch.setattr(app, "make_agent", lambda *args, **kwargs: pytest.fail("model must not start"))
    assert app.main(["--previous", "2026-07", "--current", "2026-08", "--mock", "--output-dir", str(tmp_path)]) == 0
    output = capsys.readouterr().out
    assert "MOCK report:" in output
    assert "included=3, excluded=2" in output
    text = (tmp_path / "cost_review_2026-08.md").read_text(encoding="utf-8")
    assert "mode: MOCK" in text and "## 월간 비용 비교" in text
    assert "| UnblendedCost | 1000.0 | 1200.0 | 200.0 | USD |" in text
    assert "MOCK usage increase" in text


def test_mock_mode_rejects_other_months(capsys, tmp_path):
    assert app.main(["--previous", "2026-06", "--current", "2026-07", "--mock", "--output-dir", str(tmp_path)]) == 2
    assert "비교 월이 CLI 입력과 다릅니다" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []
