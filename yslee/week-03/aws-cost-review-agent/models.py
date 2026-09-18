"""Strict exception rules and narrative-only structured model output."""

import re
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
        # Service names such as EC2/S3 remain possible, but standalone numerals
        # (including unlabeled amounts) are owned by Python source tables.
        numeral = re.compile(r"(?<![A-Za-z])\d+(?:\.\d+)?(?![A-Za-z])")
        if any(numeral.search(text) for text in prose):
            raise ValueError("설명에 숫자를 생성하지 마세요. 수치는 source table에서만 표시합니다.")
        return value
