"""Strict exception rules, the review period, and narrative-only structured model output."""

import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ResourceRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resource_id: str = Field(min_length=1)
    action: Literal["exclude"]
    reason: str = Field(min_length=1)


class TagRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1)
    value: str
    action: Literal["exclude"]
    reason: str = Field(min_length=1)


class ExceptionContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resources: list[ResourceRule] = Field(default_factory=list)
    tag_rules: list[TagRule] = Field(default_factory=list)


@dataclass(frozen=True)
class ReviewPeriod:
    """Two calendar months; Cost Explorer comparisons need first-day-to-first-day intervals."""

    previous: str
    current: str

    def __post_init__(self) -> None:
        for month in (self.previous, self.current):
            if re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", month) is None:
                raise ValueError(f"월은 YYYY-MM 형식이어야 합니다: {month}")
        if self.previous >= self.current:
            raise ValueError("previous는 current보다 이전 월이어야 합니다.")

    @staticmethod
    def _bounds(month: str) -> tuple[str, str]:
        start = date.fromisoformat(f"{month}-01")
        end = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
        return start.isoformat(), end.isoformat()

    def tool_dates(self) -> dict[str, str]:
        """The only cost-comparison date arguments the hook accepts."""
        baseline_start, baseline_end = self._bounds(self.previous)
        comparison_start, comparison_end = self._bounds(self.current)
        return {
            "baseline_start_date": baseline_start,
            "baseline_end_date": baseline_end,
            "comparison_start_date": comparison_start,
            "comparison_end_date": comparison_end,
        }


# Month and date labels the narrative may mention: 2026-07, 2026/08/01, 2026년, 8월, 1일.
_DATE_LABEL = re.compile(
    r"(?<![0-9])(?:(?:19|20)[0-9]{2}[-/](?:0[1-9]|1[0-2])(?:[-/](?:0[1-9]|[12][0-9]|3[01]))?"
    r"|[0-9]{4}년|[0-9]{1,2}월|[0-9]{1,2}일)(?![0-9])"
)
# Currency amounts: a numeral with a currency marker, or an amount-like numeral
# (decimal fraction or thousands separators) that is not a percentage.
_CURRENCY_PREFIX = re.compile(r"(?:USD|KRW|EUR|[$₩€])\s?[0-9]")
_CURRENCY_SUFFIX = re.compile(r"[0-9]\s?(?:USD|KRW|EUR|달러|원|dollars?)(?![A-Za-z])")
_AMOUNT_LIKE = re.compile(r"(?<![A-Za-z0-9.,])(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+\.[0-9]+)(?![0-9%])")


def contains_currency_amount(text: str) -> bool:
    """True when prose carries a monetary amount; month labels are ignored first."""
    scrubbed = _DATE_LABEL.sub(" ", text)
    return any(pattern.search(scrubbed) for pattern in (_CURRENCY_PREFIX, _CURRENCY_SUFFIX, _AMOUNT_LIKE))


class ReviewAnalysis(BaseModel):
    """Only prose; monetary values and resource tables come from captured source data."""

    model_config = ConfigDict(extra="forbid")

    summary: str
    cost_change_explanation: str
    priority_explanation: str
    limitations: list[str]

    @field_validator("summary", "cost_change_explanation", "priority_explanation", "limitations")
    @classmethod
    def no_generated_money(cls, value: str | list[str]) -> str | list[str]:
        prose = [value] if isinstance(value, str) else value
        if any(contains_currency_amount(text) for text in prose):
            raise ValueError(
                "설명에 금액을 쓰지 마세요. 금액은 Python이 source table로만 표시합니다. "
                "월 표기(예: 2026-07, 8월)는 허용됩니다."
            )
        return value
