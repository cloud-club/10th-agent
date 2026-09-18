"""Exact-match filtering, stable sorting, and source-preservation tests."""

import json
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError

from filters import filter_recommendations, load_exceptions
from models import ExceptionContext

ROOT = Path(__file__).resolve().parents[1]


def fixture_and_context():
    payload = json.loads((ROOT / "fixtures/recommendations.json").read_text(encoding="utf-8"))
    return payload, load_exceptions(ROOT / "context/exceptions.yaml")


def test_exception_context_fixture_is_structured():
    _, context = fixture_and_context()
    assert context.resources[0].resource_id == "i-example-batch"
    assert context.tag_rules[0].value == "true"
    with pytest.raises(ValidationError):
        ExceptionContext.model_validate({"fuzzy_rules": [{"action": "exclude"}]})


def test_resource_id_exact_match_precedes_tag_match():
    payload, context = fixture_and_context()
    payload["items"][0]["tags"] = [{"key": "OptimizationExempt", "value": "true"}]
    result = filter_recommendations(payload, context)
    batch = next(item for item in result.excluded if item["resourceId"] == "i-example-batch")
    assert batch["exclusion_reason"] == "월말 배치 처리를 위해 현재 사양 유지"
    payload["items"][0]["resourceId"] = "i-example-batch-suffix"
    assert filter_recommendations(payload, ExceptionContext(resources=context.resources)).excluded == []


def test_tag_key_and_value_exact_match_and_missing_tags():
    payload, context = fixture_and_context()
    result = filter_recommendations(payload, context)
    assert [item["resourceId"] for item in result.excluded] == [
        "i-example-batch", "vol-example-archive"
    ]
    payload["items"][1]["tags"] = [{"key": "OptimizationExempt", "value": "TRUE"}]
    payload["items"][2].pop("tags")
    result = filter_recommendations(payload, context)
    assert "vol-example-archive" in [item["resourceId"] for item in result.included]
    assert "i-example-web" in [item["resourceId"] for item in result.included]


def test_savings_sort_missing_amount_last_and_original_amount_unchanged():
    payload, context = fixture_and_context()
    original = deepcopy(payload)
    result = filter_recommendations(payload, context)
    assert [item["resourceId"] for item in result.included] == [
        "db-example-report", "i-example-web", "fn-example-idle"
    ]
    assert "estimatedMonthlySavings" not in result.included[-1]
    assert result.included[0]["estimatedMonthlySavings"] == 140.0
    assert payload == original


def test_billing_server_snake_case_wrapper_is_supported():
    payload = {"status": "success", "data": {"recommendations": [
        {"resource_id": "i-example-batch", "estimated_monthly_savings": 12.5},
        {"resource_id": "i-example-unrelated", "estimated_monthly_savings": 5.0},
    ]}}
    result = filter_recommendations(payload, fixture_and_context()[1])
    assert result.excluded[0]["resource_id"] == "i-example-batch"
    assert result.included[0]["resource_id"] == "i-example-unrelated"
