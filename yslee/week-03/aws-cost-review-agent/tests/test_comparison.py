"""Server-shape validation, selector labels, and source-preserving ordering tests."""

import json
from pathlib import Path

import pytest

from comparison import ComparisonPayloadError, comparison_data, comparison_rows, driver_rows, selector_label, total_rows

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    fixture = json.loads((ROOT / "fixtures" / name).read_text(encoding="utf-8"))
    return {"status": fixture["status"], "data": fixture["data"]}


def test_fixtures_follow_server_shape_and_values_are_copied():
    data = comparison_data("getCostAndUsageComparisons", load("cost_comparison.json"))
    assert total_rows(data) == [{
        "metric": "UnblendedCost", "baseline_time_period_amount": "1000.0",
        "comparison_time_period_amount": "1200.0", "difference": "200.0", "unit": "USD",
    }]
    assert [row["selector"] for row in comparison_rows(data)] == [
        "SERVICE=MOCK Example Compute", "SERVICE=MOCK Example Database", "SERVICE=MOCK Example Storage",
    ]
    drivers = driver_rows(comparison_data("getCostComparisonDrivers", load("cost_comparison_drivers.json")))
    assert drivers[0]["selector"] == "SERVICE=MOCK Example Compute, USAGE_TYPE=MOCK-BoxUsage:example.large"
    assert [row["driver"] for row in drivers] == ["(selector 합계)", "MOCK usage increase", "MOCK price change"]
    assert drivers[1]["difference"] == "140.0" and drivers[1]["type"] == "USAGE_INCREASE"


def test_unparsable_differences_sort_last_in_source_order():
    data = {"cost_and_usage_comparisons": [
        {"cost_and_usage_selector": {}, "metrics": {"UnblendedCost": {"difference": "abc"}}},
        {"cost_and_usage_selector": {}, "metrics": {"UnblendedCost": {"difference": "-5"}}},
        {"cost_and_usage_selector": {}, "metrics": {"UnblendedCost": {}}},
        {"cost_and_usage_selector": {}, "metrics": {"UnblendedCost": {"difference": "2"}}},
    ]}
    assert [row["difference"] for row in comparison_rows(data)] == ["-5", "2", "abc", None]
    assert comparison_rows(data)[0]["selector"] == "(전체)"


def test_bad_shapes_and_error_status_are_rejected():
    with pytest.raises(ComparisonPayloadError, match="AccessDeniedException"):
        comparison_data("getCostAndUsageComparisons", {"status": "error", "data": {"error_code": "AccessDeniedException"}})
    with pytest.raises(ComparisonPayloadError):
        comparison_data("getCostAndUsageComparisons", {"status": "success", "data": {"cost_and_usage_comparisons": "x"}})
    with pytest.raises(ComparisonPayloadError):
        comparison_data("getCostComparisonDrivers", {"status": "success", "data": {"cost_comparison_drivers": [{"cost_drivers": [1]}]}})
    with pytest.raises(ComparisonPayloadError):
        comparison_data("other", {"status": "success", "data": {}})


def test_selector_labels():
    assert selector_label(None) == "(전체)"
    assert selector_label({"Tags": {"Key": "Team", "Values": ["web", "api"]}}) == "TAG:Team=web/api"
    assert selector_label({"Not": {"Dimensions": {"Key": "REGION", "Values": ["us-east-1"]}}}) == "NOT REGION=us-east-1"
