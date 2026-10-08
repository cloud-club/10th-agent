"""Validate Billing MCP cost-comparison payloads and extract source rows for Python tables.

The awslabs Billing and Cost Management MCP server wraps Cost Explorer results as
{"status": "success", "data": {...}} with snake_case keys:
- getCostAndUsageComparisons: data.total_cost_and_usage{metric: amounts} and
  data.cost_and_usage_comparisons[{cost_and_usage_selector, metrics{metric: amounts}}]
- getCostComparisonDrivers: data.cost_comparison_drivers[{cost_selector, metrics,
  cost_drivers[{name, type, metrics}]}]
Amounts stay exactly as the server returned them (strings); nothing is recalculated.
"""

import json
from decimal import Decimal, InvalidOperation
from typing import Any

COMPARISONS_OPERATION = "getCostAndUsageComparisons"
DRIVERS_OPERATION = "getCostComparisonDrivers"
COMPARISON_OPERATIONS = frozenset({COMPARISONS_OPERATION, DRIVERS_OPERATION})
AMOUNT_FIELDS = ("baseline_time_period_amount", "comparison_time_period_amount", "difference", "unit")
_LIST_KEYS = {
    COMPARISONS_OPERATION: "cost_and_usage_comparisons",
    DRIVERS_OPERATION: "cost_comparison_drivers",
}


class ComparisonPayloadError(ValueError):
    """The cost-comparison payload cannot be rendered reliably; do not report from it."""


def _check_metrics(metrics: Any) -> None:
    if metrics is None:
        return
    if not isinstance(metrics, dict) or not all(isinstance(value, dict) for value in metrics.values()):
        raise ComparisonPayloadError("metrics 구조가 예상과 다릅니다.")


def comparison_data(operation: str, payload: Any) -> dict[str, Any]:
    """Return the server's data object for one operation without modifying it."""
    if operation not in COMPARISON_OPERATIONS:
        raise ComparisonPayloadError(f"알 수 없는 cost-comparison operation입니다: {operation}")
    if not isinstance(payload, dict):
        raise ComparisonPayloadError("Cost Explorer 응답이 JSON 객체가 아닙니다.")
    status = payload.get("status")
    if status not in (None, "success"):
        data = payload.get("data")
        code = data.get("error_code") if isinstance(data, dict) else None
        detail = f" (error_code={code})" if code else ""
        raise ComparisonPayloadError(f"Cost Explorer 응답 상태가 {status}입니다{detail}.")
    data = payload.get("data", payload)
    if not isinstance(data, dict):
        raise ComparisonPayloadError("Cost Explorer data가 JSON 객체가 아닙니다.")
    key = _LIST_KEYS[operation]
    entries = data.get(key)
    if not isinstance(entries, list) or not all(isinstance(entry, dict) for entry in entries):
        raise ComparisonPayloadError(f"{key} 목록을 확인할 수 없습니다.")
    for entry in entries:
        _check_metrics(entry.get("metrics"))
        if operation == DRIVERS_OPERATION:
            drivers = entry.get("cost_drivers", [])
            if not isinstance(drivers, list) or not all(isinstance(driver, dict) for driver in drivers):
                raise ComparisonPayloadError("cost_drivers 목록을 확인할 수 없습니다.")
            for driver in drivers:
                _check_metrics(driver.get("metrics"))
    if operation == COMPARISONS_OPERATION:
        _check_metrics(data.get("total_cost_and_usage"))
    return data


def metric_items(metrics: Any) -> list[tuple[str, dict[str, Any]]]:
    """(metric name, amount fields) pairs in source order; absent fields render as N/A later."""
    if not isinstance(metrics, dict):
        return []
    return [
        (str(name), {field: amounts.get(field) for field in AMOUNT_FIELDS})
        for name, amounts in metrics.items()
        if isinstance(amounts, dict)
    ]


def selector_label(expression: Any) -> str:
    """Compact Key=Value label for a Cost Explorer Expression; an empty selector is the whole account."""
    if not expression:
        return "(전체)"
    if not isinstance(expression, dict):
        return json.dumps(expression, ensure_ascii=False, sort_keys=True)
    parts: list[str] = []
    for kind, prefix in (("Dimensions", ""), ("Tags", "TAG:"), ("CostCategories", "COST_CATEGORY:")):
        spec = expression.get(kind)
        if isinstance(spec, dict):
            values = spec.get("Values")
            joined = "/".join(str(value) for value in values) if isinstance(values, list) else str(values)
            parts.append(f"{prefix}{spec.get('Key')}={joined}")
    for kind in ("And", "Or"):
        items = expression.get(kind)
        if isinstance(items, list):
            parts.extend(selector_label(item) for item in items)
    if expression.get("Not") is not None:
        parts.append("NOT " + selector_label(expression["Not"]))
    return ", ".join(parts) if parts else json.dumps(expression, ensure_ascii=False, sort_keys=True)


def _difference_order(row: dict[str, Any]) -> tuple[int, Decimal]:
    """Largest absolute difference first; values that are not finite numbers keep source order at the end."""
    value = row.get("difference")
    if value is None or isinstance(value, bool):
        return (1, Decimal(0))
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return (1, Decimal(0))
    if not number.is_finite():
        return (1, Decimal(0))
    return (0, -abs(number))


def total_rows(data: dict[str, Any]) -> list[dict[str, Any]]:
    """One row per metric from total_cost_and_usage; values untouched."""
    return [{"metric": name, **amounts} for name, amounts in metric_items(data.get("total_cost_and_usage"))]


def comparison_rows(data: dict[str, Any]) -> list[dict[str, Any]]:
    """One row per selector and metric, ordered by absolute difference; values untouched."""
    rows = [
        {"selector": selector_label(entry.get("cost_and_usage_selector")), "metric": name, **amounts}
        for entry in data.get("cost_and_usage_comparisons", [])
        for name, amounts in metric_items(entry.get("metrics"))
    ]
    return sorted(rows, key=_difference_order)


def driver_rows(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Selector totals followed by their cost drivers, in the order AWS returned them."""
    rows: list[dict[str, Any]] = []
    for entry in data.get("cost_comparison_drivers", []):
        selector = selector_label(entry.get("cost_selector"))
        for name, amounts in metric_items(entry.get("metrics")):
            rows.append({"selector": selector, "driver": "(selector 합계)", "type": None, "metric": name, **amounts})
        for driver in entry.get("cost_drivers", []):
            for name, amounts in metric_items(driver.get("metrics")):
                rows.append({
                    "selector": selector, "driver": driver.get("name"), "type": driver.get("type"),
                    "metric": name, **amounts,
                })
    return rows
