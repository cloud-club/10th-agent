"""Render source-owned comparison and recommendation tables plus narrative-only analysis."""

from pathlib import Path
from typing import Any

from comparison import (
    COMPARISONS_OPERATION, DRIVERS_OPERATION, comparison_data, comparison_rows, driver_rows, total_rows,
)
from hooks import SourceSnapshot
from models import ReviewAnalysis, ReviewPeriod

REPORT_SUFFIX = ".md"
COMPARISON_ROW_LIMIT = 20


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


def _table_or_empty(headers: list[str], rows: list[list[str]]) -> list[str]:
    return _table(headers, rows) if rows else ["- 데이터 없음"]


def _amount_cells(row: dict[str, Any]) -> list[str]:
    return [
        _cell(row.get("baseline_time_period_amount")),
        _cell(row.get("comparison_time_period_amount")),
        _cell(row.get("difference")),
        _cell(row.get("unit")),
    ]


def _comparison_section(previous: str, current: str, snapshot: SourceSnapshot) -> list[str]:
    """Python copies cost-comparison values from the source snapshot; nothing is computed."""
    dates = ReviewPeriod(previous, current).tool_dates()
    metrics = ", ".join(
        f"{request.get('metric_for_comparison')} ({request.get('operation')})"
        for request in snapshot.comparison_requests
    ) or "N/A"
    amount_headers = [f"{previous} (baseline)", f"{current} (comparison)", "Difference", "Unit"]
    lines = [
        "## 월간 비용 비교", "",
        "이 표는 cost-comparison source snapshot에서 Python이 값을 그대로 옮겨 생성했습니다. "
        "계산하거나 보정한 값은 없습니다.", "",
        f"- 비교 기간: baseline {dates['baseline_start_date']}~{dates['baseline_end_date']}, "
        f"comparison {dates['comparison_start_date']}~{dates['comparison_end_date']}",
        f"- metric_for_comparison: {metrics}",
        "", "### 총 비용", "",
    ]
    comparisons = snapshot.cost_comparison_raw.get(COMPARISONS_OPERATION)
    if comparisons is None:
        lines += [f"- {COMPARISONS_OPERATION} 데이터 없음"]
    else:
        data = comparison_data(COMPARISONS_OPERATION, comparisons)
        lines += _table_or_empty(
            ["Metric", *amount_headers],
            [[_cell(row["metric"]), *_amount_cells(row)] for row in total_rows(data)],
        )
        rows = comparison_rows(data)
        lines += ["", f"### 항목별 변화 (|difference| 내림차순, 상위 {COMPARISON_ROW_LIMIT}건)", ""]
        lines += _table_or_empty(
            ["Selector", "Metric", *amount_headers],
            [[_cell(row["selector"]), _cell(row["metric"]), *_amount_cells(row)]
             for row in rows[:COMPARISON_ROW_LIMIT]],
        )
        if len(rows) > COMPARISON_ROW_LIMIT:
            lines += ["", f"원본 {len(rows)}건 중 {COMPARISON_ROW_LIMIT}건만 표시했습니다."]
    lines += ["", "### 주요 cost driver (AWS 반환 순서)", ""]
    drivers = snapshot.cost_comparison_raw.get(DRIVERS_OPERATION)
    if drivers is None:
        lines += [f"- {DRIVERS_OPERATION} 데이터 없음"]
    else:
        lines += _table_or_empty(
            ["Selector", "Driver", "Type", "Metric", *amount_headers],
            [[_cell(row["selector"]), _cell(row["driver"]), _cell(row["type"]), _cell(row["metric"]),
              *_amount_cells(row)] for row in driver_rows(comparison_data(DRIVERS_OPERATION, drivers))],
        )
    return lines


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
        "- MOCK fixture: fixtures/cost_comparison_drivers.json\n"
        "- MOCK fixture: fixtures/recommendations.json"
        if mode == "MOCK" else
        "- AWS Cost Explorer / cost-comparison (getCostAndUsageComparisons, getCostComparisonDrivers)\n"
        "- AWS Cost Optimization Hub / cost-optimization (list_recommendations)"
    )
    limitations = analysis.limitations + (prerequisites or []) + snapshot.errors
    lines = [
        "# AWS Cost Review", "", "## 실행 정보", "",
        f"- 비교 월: {previous}", f"- 현재 월: {current}", f"- mode: {mode}",
        "", "## 비용 변화 요약", "", analysis.summary, "", analysis.cost_change_explanation,
        "", *_comparison_section(previous, current, snapshot),
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
