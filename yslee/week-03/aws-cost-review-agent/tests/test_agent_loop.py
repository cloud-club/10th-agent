"""Scripted model runs through the real Strands event loop; no AWS, MCP, or network."""

import json
from pathlib import Path
from typing import Any

from strands import Agent, tool
from strands.models import Model

import agent as agent_module
import app
from billing_mcp import ALLOWED_TOOLS
from filters import load_exceptions
from hooks import ReadOnlyBillingHook
from models import ReviewAnalysis, ReviewPeriod

ROOT = Path(__file__).resolve().parents[1]
PERIOD = ReviewPeriod("2026-07", "2026-08")
DATES = PERIOD.tool_dates()
WRONG_DATES = ReviewPeriod("2026-06", "2026-07").tool_dates()
ANALYSIS = {
    "summary": "2026-07 대비 2026-08 비용이 증가했습니다",
    "cost_change_explanation": "EC2 중심의 증가입니다",
    "priority_explanation": "절감액이 큰 순서로 검토합니다",
    "limitations": ["확인 불가 항목이 있습니다"],
}
COMPARISONS = ("cost-comparison", {"operation": "getCostAndUsageComparisons", "metric_for_comparison": "UnblendedCost", **DATES})
DRIVERS = ("cost-comparison", {"operation": "getCostComparisonDrivers", "metric_for_comparison": "UnblendedCost", **DATES})
RECOMMENDATIONS = ("cost-optimization", {"operation": "list_recommendations"})
STRUCTURED = ("structured_output", ANALYSIS)
# Billing MCP 0.0.37 list_recommendations shape: snake_case without tags/restart/rollback/source.
SNAKE_FIELDS = {
    "resourceId": "resource_id", "region": "region", "actionType": "action_type",
    "estimatedMonthlySavings": "estimated_monthly_savings",
    "estimatedSavingsPercentage": "estimated_savings_percentage",
    "implementationEffort": "implementation_effort", "currencyCode": "currency_code",
}


def fixture(name: str) -> dict[str, Any]:
    payload = json.loads((ROOT / "fixtures" / name).read_text(encoding="utf-8"))
    return {"status": payload["status"], "data": payload["data"]}


@tool(name="cost-comparison")
def fake_cost_comparison(
    operation: str, baseline_start_date: str, baseline_end_date: str,
    comparison_start_date: str, comparison_end_date: str, metric_for_comparison: str,
    group_by: str | None = None,
) -> str:
    """Fake Billing MCP cost-comparison returning fixture JSON text like the server."""
    name = "cost_comparison.json" if operation == "getCostAndUsageComparisons" else "cost_comparison_drivers.json"
    return json.dumps(fixture(name))


@tool(name="cost-optimization")
def fake_cost_optimization(operation: str) -> str:
    """Fake Billing MCP cost-optimization in the server's snake_case shape."""
    items = json.loads((ROOT / "fixtures/recommendations.json").read_text(encoding="utf-8"))["items"]
    recommendations = [{snake: item.get(camel) for camel, snake in SNAKE_FIELDS.items()} for item in items]
    return json.dumps({"status": "success", "data": {"recommendations": recommendations}})


class ScriptedModel(Model):
    """Replays scripted tool calls, then the structured output, through the real Strands loop."""

    def __init__(self, steps: list[tuple[str, dict[str, Any]]]) -> None:
        self.steps = steps
        self.calls = 0
        self.config: dict[str, Any] = {}

    def update_config(self, **model_config: Any) -> None:
        self.config.update(model_config)

    def get_config(self) -> dict[str, Any]:
        return self.config

    async def structured_output(self, output_model, prompt, system_prompt=None, **kwargs):
        raise NotImplementedError("structured output goes through the tool path")
        yield  # pragma: no cover

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        self.calls += 1
        usage = {"metadata": {"usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2}, "metrics": {"latencyMs": 0}}}
        yield {"messageStart": {"role": "assistant"}}
        if self.calls > len(self.steps):
            yield {"contentBlockStart": {"start": {}}}
            yield {"contentBlockDelta": {"delta": {"text": "done"}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "end_turn"}}
            yield usage
            return
        name, payload = self.steps[self.calls - 1]
        if name == "structured_output":
            name = next(spec["name"] for spec in tool_specs or [] if spec["name"] not in ALLOWED_TOOLS)
        yield {"contentBlockStart": {"start": {"toolUse": {"toolUseId": f"call-{self.calls}", "name": name}}}}
        yield {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps(payload)}}}}
        yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": "tool_use"}}
        yield usage


def scripted_agent(steps, hook: ReadOnlyBillingHook) -> Agent:
    return Agent(
        model=ScriptedModel(steps), tools=[fake_cost_comparison, fake_cost_optimization],
        system_prompt=agent_module.SYSTEM_PROMPT, hooks=[hook],
        load_tools_from_directory=False, callback_handler=None,
    )


def tool_results(agent: Agent) -> list[dict[str, Any]]:
    return [block["toolResult"] for message in agent.messages for block in message["content"] if "toolResult" in block]


def run_app(monkeypatch, steps, output_dir) -> int:
    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    monkeypatch.setattr(app, "make_billing_client", lambda *args: FakeClient())
    monkeypatch.setattr(app, "discover_billing_tools", lambda _: (sorted(ALLOWED_TOOLS), []))
    monkeypatch.setattr(app, "make_agent", lambda tools, *, hook, **kwargs: scripted_agent(steps, hook))
    return app.main(["--previous", "2026-07", "--current", "2026-08", "--output-dir", str(output_dir)])


def test_wrong_month_call_is_cancelled_then_retry_succeeds():
    hook = ReadOnlyBillingHook(load_exceptions(ROOT / "context/exceptions.yaml"), period=PERIOD)
    wrong = ("cost-comparison", {**COMPARISONS[1], **WRONG_DATES})
    agent = scripted_agent([wrong, COMPARISONS, DRIVERS, RECOMMENDATIONS, STRUCTURED], hook)
    result = agent(agent_module.review_request(PERIOD), structured_output_model=ReviewAnalysis)
    results = tool_results(agent)
    assert results[0]["status"] == "error"
    assert "기간이 요청한 월과 다릅니다" in results[0]["content"][0]["text"]
    assert "comparison_end_date=2026-09-01" in results[0]["content"][0]["text"]
    assert [item["status"] for item in results[1:4]] == ["success", "success", "success"]
    assert set(results[3]["content"][0]["json"]) == {"included_recommendations", "excluded_recommendations"}
    assert isinstance(result.structured_output, ReviewAnalysis)
    assert set(hook.snapshot.cost_comparison_raw) == {"getCostAndUsageComparisons", "getCostComparisonDrivers"}
    assert [item["metric_for_comparison"] for item in hook.snapshot.comparison_requests] == ["UnblendedCost", "UnblendedCost"]
    assert hook.snapshot.missing_sources() == [] and hook.snapshot.errors == []


def test_report_refused_when_drivers_missing(monkeypatch, capsys, tmp_path):
    assert run_app(monkeypatch, [COMPARISONS, RECOMMENDATIONS, STRUCTURED], tmp_path) == 2
    err = capsys.readouterr().err
    assert "리포트를 생성하지 않습니다" in err and "cost-comparison getCostComparisonDrivers" in err
    assert list(tmp_path.iterdir()) == []


def test_complete_run_writes_live_report_with_comparison_tables(monkeypatch, capsys, tmp_path):
    assert run_app(monkeypatch, [COMPARISONS, DRIVERS, RECOMMENDATIONS, STRUCTURED], tmp_path) == 0
    assert "LIVE report:" in capsys.readouterr().out
    text = (tmp_path / "cost_review_2026-08.md").read_text(encoding="utf-8")
    assert "mode: LIVE" in text and "## 월간 비용 비교" in text
    assert "| UnblendedCost | 1000.0 | 1200.0 | 200.0 | USD |" in text
    assert "MOCK usage increase | USAGE_INCREASE" in text
    assert "metric_for_comparison: UnblendedCost (getCostAndUsageComparisons), UnblendedCost (getCostComparisonDrivers)" in text
    assert "2026-07 대비 2026-08 비용이 증가했습니다" in text
    excluded = text.split("## 업무 예외로 제외된 권고", 1)[1].split("## 분석", 1)[0]
    assert "i-example-batch" in excluded and "vol-example-archive" not in excluded
    assert "tags를 전달하지 않은 권고의 태그 예외 적용 여부는 확인 불가" in text
