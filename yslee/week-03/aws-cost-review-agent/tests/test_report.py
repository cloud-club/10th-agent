"""Source-owned tables and MOCK provenance tests."""

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
from models import ReviewAnalysis

ROOT = Path(__file__).resolve().parents[1]


def test_importable_boundaries_and_synthetic_fixtures():
    assert agent.SYSTEM_PROMPT and hooks.ReadOnlyBillingHook
    assert billing_mcp.ALLOWED_TOOLS == {"cost-comparison", "cost-optimization"}
    assert report.REPORT_SUFFIX == ".md"
    assert callable(app.main)
    fixture_dir = ROOT / "fixtures"
    for filename in ("cost_comparison.json", "recommendations.json"):
        payload = json.loads((fixture_dir / filename).read_text(encoding="utf-8"))
        assert payload["synthetic"] is True


def test_report_sections_and_source_amounts(tmp_path):
    payload = json.loads((ROOT / "fixtures/recommendations.json").read_text(encoding="utf-8"))
    guard = hooks.ReadOnlyBillingHook(load_exceptions(ROOT / "context/exceptions.yaml"))
    guard.process_optimization(payload)
    path = report.write_report(
        previous="2026-07", current="2026-08", mode="MOCK",
        analysis=app.mock_analysis(), snapshot=guard.snapshot, directory=tmp_path,
    )
    text = path.read_text(encoding="utf-8")
    review = text.split("## 검토할 비용 최적화 후보", 1)[1].split("## 업무 예외로 제외된 권고", 1)[0]
    excluded = text.split("## 업무 예외로 제외된 권고", 1)[1].split("## 분석", 1)[0]
    assert "i-example-batch" not in review and "vol-example-archive" not in review
    assert "i-example-batch" in excluded and "vol-example-archive" in excluded
    assert review.index("db-example-report") < review.index("i-example-web") < review.index("fn-example-idle")
    for item in guard.snapshot.included_recommendations + guard.snapshot.excluded_recommendations:
        amount = item.get("estimatedMonthlySavings")
        if amount is not None:
            section = excluded if item in guard.snapshot.excluded_recommendations else review
            assert f"USD {amount}" in section
    assert "fn-example-idle | us-east-1 | Review | N/A" in review
    assert "mode: MOCK" in text and "MOCK fixture" in text


def test_analysis_schema_rejects_generated_monetary_amounts():
    for prose in ("추정 절감액 USD 999", "절감액은 999로 예상됩니다"):
        with pytest.raises(ValidationError):
            ReviewAnalysis(
                summary=prose, cost_change_explanation="설명",
                priority_explanation="설명", limitations=[],
            )
