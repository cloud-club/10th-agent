"""Read-only operation and duplicate-call guard tests."""

import json
from pathlib import Path
from types import SimpleNamespace

from filters import load_exceptions
from hooks import ReadOnlyBillingHook

ROOT = Path(__file__).resolve().parents[1]


def event(name, operation):
    return SimpleNamespace(tool_use={"name": name, "input": {"operation": operation}}, cancel_tool=False)


def test_only_list_recommendations_and_one_call_allowed():
    guard = ReadOnlyBillingHook()
    unsafe = event("cost-optimization", "enable_opt_in")
    guard.before_tool_call(unsafe)
    assert unsafe.cancel_tool
    first = event("cost-optimization", "list_recommendations")
    guard.before_tool_call(first)
    assert not first.cancel_tool
    second = event("cost-optimization", "list_recommendations")
    guard.before_tool_call(second)
    assert second.cancel_tool


def test_comparison_duplicates_and_other_tools_rejected():
    guard = ReadOnlyBillingHook()
    first = event("cost-comparison", "getCostAndUsageComparisons")
    guard.before_tool_call(first)
    assert not first.cancel_tool
    duplicate = event("cost-comparison", "getCostAndUsageComparisons")
    guard.before_tool_call(duplicate)
    assert duplicate.cancel_tool
    driver = event("cost-comparison", "getCostComparisonDrivers")
    guard.before_tool_call(driver)
    assert not driver.cancel_tool
    unknown = event("cost-comparison", "unknown")
    guard.before_tool_call(unknown)
    assert unknown.cancel_tool
    other = event("cost-anomaly", "get_anomalies")
    guard.before_tool_call(other)
    assert other.cancel_tool
    structured = event("ReviewAnalysis", "format")
    structured.selected_tool = SimpleNamespace(tool_type="structured_output")
    guard.before_tool_call(structured)
    assert not structured.cancel_tool


def after_event(content, *, structured=None):
    result = {"toolUseId": "test-1", "status": "success", "content": content}
    if structured is not None:
        result["structuredContent"] = structured
    return SimpleNamespace(
        tool_use={"name": "cost-optimization", "toolUseId": "test-1"},
        result=result, cancel_message=None,
    )


def test_after_tool_call_replaces_json_result_before_model_reads_it():
    payload = json.loads((ROOT / "fixtures/recommendations.json").read_text(encoding="utf-8"))
    guard = ReadOnlyBillingHook(load_exceptions(ROOT / "context/exceptions.yaml"))
    event = after_event([{"text": json.dumps(payload)}])
    guard.after_tool_call(event)
    assert event.result["status"] == "success"
    assert "structuredContent" not in event.result
    filtered = event.result["content"][0]["json"]
    assert len(filtered["included_recommendations"]) == 3
    assert len(filtered["excluded_recommendations"]) == 2
    assert guard.snapshot.optimization_raw["items"][0]["estimatedMonthlySavings"] == 120.0


def test_after_tool_call_accepts_json_content_and_fails_closed_on_bad_text():
    guard = ReadOnlyBillingHook(load_exceptions(ROOT / "context/exceptions.yaml"))
    good = after_event([{"json": {"items": []}}])
    guard.after_tool_call(good)
    assert good.result["status"] == "success"
    bad = after_event([{"text": "not JSON"}])
    guard.after_tool_call(bad)
    assert bad.result["status"] == "error"
    assert bad.result["content"][0]["text"].startswith("cost-optimization 결과 처리 실패")
    assert guard.snapshot.optimization_raw["content"] == [{"text": "not JSON"}]
    assert guard.snapshot.errors


def test_after_tool_call_prefers_structured_content_and_captures_comparison():
    guard = ReadOnlyBillingHook()
    event = after_event([{"text": "not JSON"}], structured={"items": []})
    guard.after_tool_call(event)
    assert event.result["status"] == "success"
    comparison = SimpleNamespace(
        tool_use={"name": "cost-comparison"},
        result={"status": "success", "toolUseId": "test-2", "content": [{"json": {"status": "success"}}]},
        cancel_message=None,
    )
    guard.after_tool_call(comparison)
    assert guard.snapshot.cost_comparison_raw[0]["toolUseId"] == "test-2"
