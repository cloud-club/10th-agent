"""Render source-owned recommendation tables and narrative-only analysis."""

from pathlib import Path
from typing import Any

from hooks import SourceSnapshot
from models import ReviewAnalysis

REPORT_SUFFIX = ".md"


def _field(item: dict[str, Any], camel: str, snake: str) -> Any:
    return item.get(camel, item.get(snake))


def _cell(value: Any) -> str:
    if value is None or value == "":
        return "N/A"
    return str(value).replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")


def _savings(item: dict[str, Any]) -> str:
    amount = _field(item, "estimatedMonthlySavings", "estimated_monthly_savings")
    if amount is None:
        return "N/A"
    currency = _field(item, "currencyCode", "currency_code")
    return f"{_cell(currency)} {_cell(amount)}" if currency else _cell(amount)


def _table(headers: list[str], rows: list[list[str]]) -> list[str]:
    return [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
        *("| " + " | ".join(row) + " |" for row in rows),
    ]


def render_report(
    *, previous: str, current: str, mode: str,
    analysis: ReviewAnalysis, snapshot: SourceSnapshot,
    prerequisites: list[str] | None = None,
) -> str:
    """Never ask the model to reproduce source values in the tables."""
    if mode not in {"LIVE", "MOCK"}:
        raise ValueError("mode must be LIVE or MOCK")
    included = snapshot.included_recommendations
    excluded = snapshot.excluded_recommendations
    rows = [
        [
            _cell(_field(item, "resourceId", "resource_id")),
            _cell(item.get("region")),
            _cell(_field(item, "actionType", "action_type")),
            _savings(item),
            _cell(_field(item, "estimatedSavingsPercentage", "estimated_savings_percentage")),
            _cell(_field(item, "implementationEffort", "implementation_effort")),
            _cell(_field(item, "restartNeeded", "restart_needed")),
            _cell(_field(item, "rollbackPossible", "rollback_possible")),
            _cell(item.get("source")),
        ]
        for item in included
    ]
    excluded_rows = [
        [
            _cell(_field(item, "resourceId", "resource_id")),
            _cell(_field(item, "actionType", "action_type")),
            _savings(item),
            _cell(item.get("exclusion_reason")),
        ]
        for item in excluded
    ]
    source = (
        "- MOCK fixture: fixtures/cost_comparison.json\n"
        "- MOCK fixture: fixtures/recommendations.json"
        if mode == "MOCK" else
        "- AWS Cost Explorer / cost-comparison\n"
        "- AWS Cost Optimization Hub / cost-optimization"
    )
    limitations = analysis.limitations + (prerequisites or []) + snapshot.errors
    lines = [
        "# AWS Cost Review", "", "## 실행 정보", "",
        f"- 비교 월: {previous}", f"- 현재 월: {current}", f"- mode: {mode}",
        "", "## 비용 변화 요약", "", analysis.summary, "", analysis.cost_change_explanation,
        "", "## 검토할 비용 최적화 후보", "",
        "이 표는 source snapshot에서 Python이 직접 생성했습니다. 제안의 실제 적용 여부는 사람이 결정합니다.", "",
        *_table(
            ["Resource", "Region", "Action", "Estimated Monthly Savings", "Savings %",
             "Implementation Effort", "Restart Needed", "Rollback Possible", "Source"],
            rows,
        ),
        "", "## 업무 예외로 제외된 권고", "",
        *_table(["Resource", "Action", "Estimated Monthly Savings", "Exclusion Reason"], excluded_rows),
        "", "## 분석", "", analysis.priority_explanation,
        "", "## 한계 및 주의사항", "",
        *(f"- {item}" for item in limitations),
        "", "## 데이터 출처", "", source, "",
    ]
    return "\n".join(lines)


def write_report(
    *, previous: str, current: str, mode: str, analysis: ReviewAnalysis,
    snapshot: SourceSnapshot, directory: Path, prerequisites: list[str] | None = None,
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"cost_review_{current}{REPORT_SUFFIX}"
    path.write_text(
        render_report(
            previous=previous, current=current, mode=mode,
            analysis=analysis, snapshot=snapshot, prerequisites=prerequisites,
        ),
        encoding="utf-8",
    )
    return path
