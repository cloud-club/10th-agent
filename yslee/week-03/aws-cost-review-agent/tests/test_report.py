"""Source-owned tables, MOCK provenance, and narrative validator tests."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

import agent
import app
import billing_mcp
from filters import load_exceptions
import hooks
import report
from models import ReviewAnalysis, ReviewPeriod

ROOT = Path(__file__).resolve().parents[1]
PERIOD = ReviewPeriod("2026-07", "2026-08")


def analysis(prose):
    return ReviewAnalysis(summary=prose, cost_change_explanation="설명", priority_explanation="설명", limitations=[])


def load_comparisons(guard):
    for name, operation in (
        ("cost_comparison.json", "getCostAndUsageComparisons"),
        ("cost_comparison_drivers.json", "getCostComparisonDrivers"),
    ):
        fixture = json.loads((ROOT / "fixtures" / name).read_text(encoding="utf-8"))
        request = {"operation": operation, "metric_for_comparison": "UnblendedCost", **PERIOD.tool_dates()}
        assert guard.accept_comparison_request(request) is None
        guard.process_comparison(operation, {"status": fixture["status"], "data": fixture["data"]})


def test_importable_boundaries_and_synthetic_fixtures():
    assert agent.SYSTEM_PROMPT and hooks.ReadOnlyBillingHook
    assert billing_mcp.ALLOWED_TOOLS == {"cost-comparison", "cost-optimization"}
    assert report.REPORT_SUFFIX == ".md"
    assert callable(app.main)
    fixture_dir = ROOT / "fixtures"
    for filename in ("cost_comparison.json", "cost_comparison_drivers.json", "recommendations.json"):
        payload = json.loads((fixture_dir / filename).read_text(encoding="utf-8"))
        assert payload["synthetic"] is True


def test_report_sections_and_source_amounts(tmp_path):
    payload = json.loads((ROOT / "fixtures/recommendations.json").read_text(encoding="utf-8"))
    guard = hooks.ReadOnlyBillingHook(load_exceptions(ROOT / "context/exceptions.yaml"), period=PERIOD)
    load_comparisons(guard)
    guard.process_optimization(payload)
    assert guard.snapshot.missing_sources() == []
    path = report.write_report(
        previous="2026-07", current="2026-08", mode="MOCK",
        analysis=app.mock_analysis(), snapshot=guard.snapshot, directory=tmp_path,
    )
    text = path.read_text(encoding="utf-8")
    comparison = text.split("## 월간 비용 비교", 1)[1].split("## 검토할 비용 최적화 후보", 1)[0]
    review = text.split("## 검토할 비용 최적화 후보", 1)[1].split("## 업무 예외로 제외된 권고", 1)[0]
    excluded = text.split("## 업무 예외로 제외된 권고", 1)[1].split("## 분석", 1)[0]
    assert "- 비교 기간: baseline 2026-07-01~2026-08-01, comparison 2026-08-01~2026-09-01" in comparison
    assert "- metric_for_comparison: UnblendedCost (getCostAndUsageComparisons), UnblendedCost (getCostComparisonDrivers)" in comparison
    assert "| Metric | 2026-07 (baseline) | 2026-08 (comparison) | Difference | Unit |" in comparison
    assert "| UnblendedCost | 1000.0 | 1200.0 | 200.0 | USD |" in comparison
    assert comparison.index("MOCK Example Compute") < comparison.index("MOCK Example Database") < comparison.index("MOCK Example Storage")
    assert "| SERVICE=MOCK Example Storage | UnblendedCost | 300.0 | 290.0 | -10.0 | USD |" in comparison
    assert "| SERVICE=MOCK Example Compute, USAGE_TYPE=MOCK-BoxUsage:example.large | MOCK usage increase | USAGE_INCREASE | UnblendedCost | 500.0 | 640.0 | 140.0 | USD |" in comparison
    assert "i-example-batch" not in review and "vol-example-archive" not in review
    assert "i-example-batch" in excluded and "vol-example-archive" in excluded
    assert review.index("db-example-report") < review.index("i-example-web") < review.index("fn-example-idle")
    for item in guard.snapshot.included_recommendations + guard.snapshot.excluded_recommendations:
        amount = item.get("estimatedMonthlySavings")
        if amount is not None:
            section = excluded if item in guard.snapshot.excluded_recommendations else review
            assert f"USD {amount}" in section
    assert "fn-example-idle | us-east-1 | Review | N/A" in review
    assert "mode: MOCK" in text and "MOCK fixture: fixtures/cost_comparison_drivers.json" in text


def test_missing_comparison_payloads_render_as_no_data():
    text = report.render_report(
        previous="2026-07", current="2026-08", mode="LIVE",
        analysis=app.mock_analysis(), snapshot=hooks.SourceSnapshot(),
    )
    assert "- getCostAndUsageComparisons 데이터 없음" in text
    assert "- getCostComparisonDrivers 데이터 없음" in text
    assert "- metric_for_comparison: N/A" in text


def test_comparison_rows_are_capped_with_omission_note():
    entries = [
        {"cost_and_usage_selector": {"Dimensions": {"Key": "SERVICE", "Values": [f"svc{i}"]}},
         "metrics": {"UnblendedCost": {"difference": str(i), "unit": "USD"}}}
        for i in range(25)
    ]
    snapshot = hooks.SourceSnapshot(cost_comparison_raw={
        "getCostAndUsageComparisons": {"status": "success", "data": {"cost_and_usage_comparisons": entries}},
    })
    text = report.render_report(previous="2026-07", current="2026-08", mode="LIVE", analysis=app.mock_analysis(), snapshot=snapshot)
    assert "원본 25건 중 20건만 표시했습니다." in text
    assert "SERVICE=svc24" in text and "SERVICE=svc4 " not in text


def test_analysis_schema_rejects_currency_amounts_but_allows_month_labels():
    for prose in (
        "추정 절감액 USD 999", "절감액은 999달러로 예상됩니다", "$999 절감",
        "비용이 140.0에서 늘었습니다", "총 1,200 수준", "999 KRW",
    ):
        with pytest.raises(ValidationError):
            analysis(prose)
    for prose in (
        "2026-07 대비 2026-08 비용이 증가했습니다",
        "7월 대비 8월에 EC2 비용이 약 20% 증가했습니다",
        "2026년 8월 1일 기준 t3.large에서 m5.2xlarge로 변경 권고",
        "데이터 없음/확인 불가",
    ):
        analysis(prose)
    with pytest.raises(ValidationError):
        ReviewAnalysis(summary="요약", cost_change_explanation="설명", priority_explanation="설명", limitations=["USD 10 추정"])
