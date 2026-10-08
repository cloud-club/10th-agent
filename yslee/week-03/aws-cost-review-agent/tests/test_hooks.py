"""Read-only operation, review-period, and duplicate-call guard tests."""

import json
from pathlib import Path
from types import SimpleNamespace

from filters import load_exceptions
from hooks import ReadOnlyBillingHook
from models import ReviewPeriod

ROOT = Path(__file__).resolve().parents[1]
PERIOD = ReviewPeriod("2026-07", "2026-08")
DATES = PERIOD.tool_dates()


def comparison_input(operation, **overrides):
    return {"operation": operation, "metric_for_comparison": "UnblendedCost", **DATES, **overrides}


def event(name, params):
    return SimpleNamespace(tool_use={"name": name, "input": params}, cancel_tool=False)


def test_review_period_month_boundaries():
    assert DATES == {
        "baseline_start_date": "2026-07-01", "baseline_end_date": "2026-08-01",
        "comparison_start_date": "2026-08-01", "comparison_end_date": "2026-09-01",
    }
    assert ReviewPeriod("2025-12", "2026-01").tool_dates()["baseline_end_date"] == "2026-01-01"


def test_only_list_recommendations_and_one_call_allowed():
    guard = ReadOnlyBillingHook()
    unsafe = event("cost-optimization", {"operation": "enable_opt_in"})
    guard.before_tool_call(unsafe)
    assert unsafe.cancel_tool
    first = event("cost-optimization", {"operation": "list_recommendations"})
    guard.before_tool_call(first)
    assert not first.cancel_tool
    second = event("cost-optimization", {"operation": "list_recommendations"})
    guard.before_tool_call(second)
    assert second.cancel_tool


def test_wrong_period_or_missing_metric_is_cancelled_without_spending_budget():
    guard = ReadOnlyBillingHook(period=PERIOD)
    wrong = event("cost-comparison", comparison_input(
        "getCostAndUsageComparisons", baseline_start_date="2026-06-01", baseline_end_date="2026-07-01"
    ))
    guard.before_tool_call(wrong)
    assert "기간이 요청한 월과 다릅니다" in wrong.cancel_tool
    assert "baseline_start_date=2026-07-01" in wrong.cancel_tool
    no_metric = event("cost-comparison", comparison_input("getCostAndUsageComparisons", metric_for_comparison=""))
    guard.before_tool_call(no_metric)
    assert "metric_for_comparison" in no_metric.cancel_tool
    assert guard.snapshot.comparison_requests == []
    correct = event("cost-comparison", comparison_input("getCostAndUsageComparisons"))
    guard.before_tool_call(correct)
    assert not correct.cancel_tool
    assert guard.snapshot.comparison_requests == [
        {"operation": "getCostAndUsageComparisons", "metric_for_comparison": "UnblendedCost", **DATES}
    ]


def test_comparison_requires_configured_period():
    guard = ReadOnlyBillingHook()
    call = event("cost-comparison", comparison_input("getCostAndUsageComparisons"))
    guard.before_tool_call(call)
    assert "리뷰 기간" in call.cancel_tool


def test_comparison_duplicates_and_other_tools_rejected():
    guard = ReadOnlyBillingHook(period=PERIOD)
    first = event("cost-comparison", comparison_input("getCostAndUsageComparisons"))
    guard.before_tool_call(first)
    assert not first.cancel_tool
    duplicate = event("cost-comparison", comparison_input("getCostAndUsageComparisons"))
    guard.before_tool_call(duplicate)
    assert duplicate.cancel_tool
    driver = event("cost-comparison", comparison_input("getCostComparisonDrivers", metric_for_comparison="AmortizedCost"))
    guard.before_tool_call(driver)
    assert not driver.cancel_tool
    assert [r["metric_for_comparison"] for r in guard.snapshot.comparison_requests] == ["UnblendedCost", "AmortizedCost"]
    unknown = event("cost-comparison", comparison_input("unknown"))
    guard.before_tool_call(unknown)
    assert unknown.cancel_tool
    other = event("cost-anomaly", {"operation": "get_anomalies"})
    guard.before_tool_call(other)
    assert other.cancel_tool
    structured = event("ReviewAnalysis", {"operation": "format"})
    structured.selected_tool = SimpleNamespace(tool_type="structured_output")
    guard.before_tool_call(structured)
    assert not structured.cancel_tool


def after_event(content, *, name="cost-optimization", operation=None, structured=None):
    result = {"toolUseId": "test-1", "status": "success", "content": content}
    if structured is not None:
        result["structuredContent"] = structured
    tool_use = {"name": name, "toolUseId": "test-1", "input": {"operation": operation} if operation else {}}
    return SimpleNamespace(tool_use=tool_use, result=result, cancel_message=None)


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
    assert guard.snapshot.optimization_succeeded


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
    assert not guard.snapshot.optimization_succeeded


def test_after_tool_call_prefers_structured_content():
    guard = ReadOnlyBillingHook()
    event = after_event([{"text": "not JSON"}], structured={"items": []})
    guard.after_tool_call(event)
    assert event.result["status"] == "success"


def test_after_tool_call_stores_comparison_payload_unchanged_and_tracks_completion():
    guard = ReadOnlyBillingHook(period=PERIOD)
    fixture = json.loads((ROOT / "fixtures/cost_comparison.json").read_text(encoding="utf-8"))
    payload = {"status": fixture["status"], "data": fixture["data"]}
    event = after_event([{"text": json.dumps(payload)}], name="cost-comparison", operation="getCostAndUsageComparisons")
    guard.after_tool_call(event)
    assert event.result["status"] == "success"
    stored = guard.snapshot.cost_comparison_raw["getCostAndUsageComparisons"]
    assert stored == payload
    assert stored["data"]["total_cost_and_usage"]["UnblendedCost"]["difference"] == "200.0"
    assert guard.snapshot.missing_sources() == [
        "cost-comparison getCostComparisonDrivers", "cost-optimization list_recommendations",
    ]
    failed = after_event(
        [{"json": {"status": "error", "data": {"error_code": "AccessDeniedException"}}}],
        name="cost-comparison", operation="getCostComparisonDrivers",
    )
    guard.after_tool_call(failed)
    assert "getCostComparisonDrivers" not in guard.snapshot.cost_comparison_raw
    assert any("AccessDeniedException" in error for error in guard.snapshot.errors)
    assert "cost-comparison getCostComparisonDrivers" in guard.snapshot.missing_sources()
