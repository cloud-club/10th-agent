"""Exact-match business exclusions and source-preserving recommendation order."""

from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml

from models import ExceptionContext


class FilteringError(ValueError):
    """Input cannot be filtered reliably; do not pass it to the LLM."""


@dataclass
class FilterResult:
    included: list[dict[str, Any]]
    excluded: list[dict[str, Any]]


def load_exceptions(path: Path) -> ExceptionContext:
    """Reject unsupported rules instead of guessing how they should apply."""
    with path.open(encoding="utf-8") as source:
        return ExceptionContext.model_validate(yaml.safe_load(source))


def recommendation_items(payload: Any) -> list[dict[str, Any]]:
    """Accept AWS items or the Billing server's data.recommendations wrapper."""
    if not isinstance(payload, dict) or payload.get("status") not in (None, "success"):
        raise FilteringError("Cost Optimization Hub 응답이 JSON 객체의 성공 결과가 아닙니다.")
    data = payload.get("data", payload)
    if not isinstance(data, dict):
        raise FilteringError("Cost Optimization Hub data가 JSON 객체가 아닙니다.")
    items = data.get("items", data.get("recommendations"))
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise FilteringError("Cost Optimization Hub items/recommendations 목록을 확인할 수 없습니다.")
    return items


def _resource_id(item: dict[str, Any]) -> Any:
    return item.get("resourceId", item.get("resource_id"))


def _tags(item: dict[str, Any]) -> list[tuple[Any, Any]]:
    tags = item.get("tags")
    if tags is None:
        return []
    if isinstance(tags, dict):
        return list(tags.items())
    if isinstance(tags, list) and all(isinstance(tag, dict) for tag in tags):
        return [(tag.get("key"), tag.get("value")) for tag in tags]
    raise FilteringError("recommendation tags의 구조가 예상과 다릅니다.")


def _savings(item: dict[str, Any]) -> Decimal | None:
    value = item.get("estimatedMonthlySavings", item.get("estimated_monthly_savings"))
    if value is None:
        return None
    if isinstance(value, bool):
        raise FilteringError("estimatedMonthlySavings에 boolean 값이 있습니다.")
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise FilteringError("estimatedMonthlySavings를 숫자로 정렬할 수 없습니다.") from exc
    if not number.is_finite():
        raise FilteringError("estimatedMonthlySavings가 유한한 숫자가 아닙니다.")
    return number


def filter_recommendations(payload: Any, context: ExceptionContext) -> FilterResult:
    """Resource exact match wins over tag exact match; never mutate the source."""
    included: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for original in recommendation_items(payload):
        item = deepcopy(original)
        resource = _resource_id(item)
        reason = next((rule.reason for rule in context.resources if resource == rule.resource_id), None)
        if reason is None:
            tags = _tags(item)
            reason = next(
                (rule.reason for rule in context.tag_rules if (rule.key, rule.value) in tags), None
            )
        if reason is not None:
            item["exclusion_reason"] = reason
            excluded.append(item)
        else:
            included.append(item)

    # Validate every savings value, including excluded records, before returning.
    for item in included + excluded:
        _savings(item)
    valued = [item for item in included if _savings(item) is not None]
    unknown = [item for item in included if _savings(item) is None]
    valued.sort(key=lambda item: _savings(item), reverse=True)
    return FilterResult(included=valued + unknown, excluded=excluded)
