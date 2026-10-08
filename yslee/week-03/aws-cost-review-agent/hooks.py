"""Strands tool guards and deterministic post-tool filtering."""

import json
import logging
from copy import deepcopy
from dataclasses import dataclass, field
from threading import Lock
from typing import Any

from strands.hooks import AfterToolCallEvent, BeforeToolCallEvent, HookRegistry
from strands.types.tools import ToolResult

from billing_mcp import ALLOWED_TOOLS
from comparison import COMPARISON_OPERATIONS, ComparisonPayloadError, comparison_data
from filters import FilterResult, FilteringError, filter_recommendations, recommendation_items
from models import ExceptionContext, ReviewPeriod

OPTIMIZATION_OPERATION = "list_recommendations"
logger = logging.getLogger(__name__)


@dataclass
class SourceSnapshot:
    """In-memory source data; never serialize credentials or account metadata."""

    # operation -> server payload ({"status", "data"}) exactly as decoded from the tool result
    cost_comparison_raw: dict[str, dict[str, Any]] = field(default_factory=dict)
    # accepted cost-comparison request parameters (operation, metric_for_comparison, dates)
    comparison_requests: list[dict[str, str]] = field(default_factory=list)
    optimization_raw: Any = None
    optimization_succeeded: bool = False
    included_recommendations: list[dict[str, Any]] = field(default_factory=list)
    excluded_recommendations: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def missing_sources(self) -> list[str]:
        """Required operations without a validated success result; the report needs all of them."""
        missing = [
            f"cost-comparison {operation}"
            for operation in sorted(COMPARISON_OPERATIONS)
            if operation not in self.cost_comparison_raw
        ]
        if not self.optimization_succeeded:
            missing.append(f"cost-optimization {OPTIMIZATION_OPERATION}")
        return missing


def decode_tool_payload(result: ToolResult) -> dict[str, Any]:
    """Read structured JSON, JSON content, or text containing one JSON object."""
    if result.get("status") != "success":
        raise FilteringError("MCP tool 실행이 성공하지 않았습니다.")
    candidates = [result.get("structuredContent")]
    for block in result.get("content", []):
        if isinstance(block, dict):
            candidates.extend((block.get("json"), block.get("text")))
    for candidate in candidates:
        if isinstance(candidate, dict):
            return candidate
        if isinstance(candidate, str):
            text = candidate.strip()
            if text.startswith("```json") and text.endswith("```"):
                text = text[7:-3].strip()
            try:
                decoded = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(decoded, dict):
                return decoded
    raise FilteringError("MCP ToolResult에서 JSON 객체를 파싱할 수 없습니다.")


class ReadOnlyBillingHook:
    """Cap calls, pin the review period, and forbid non-list Cost Optimization Hub operations."""

    def __init__(self, context: ExceptionContext | None = None, period: ReviewPeriod | None = None) -> None:
        self.context = context or ExceptionContext()
        self.period = period
        self.snapshot = SourceSnapshot()
        self._lock = Lock()
        self._comparison_operations: set[str] = set()
        self._optimization_calls = 0

    def register_hooks(self, registry: HookRegistry, **kwargs: object) -> None:
        registry.add_callback(BeforeToolCallEvent, self.before_tool_call)
        registry.add_callback(AfterToolCallEvent, self.after_tool_call)

    def accept_comparison_request(self, parameters: Any) -> str | None:
        """Return a cancel message, or record the accepted request. Shared by the live hook and mock."""
        if not isinstance(parameters, dict):
            return "tool 입력 형식이 올바르지 않습니다."
        operation = parameters.get("operation")
        if operation not in COMPARISON_OPERATIONS:
            return "허용되지 않은 cost-comparison operation입니다."
        if self.period is None:
            return "리뷰 기간이 설정되지 않아 cost-comparison을 호출할 수 없습니다."
        expected = self.period.tool_dates()
        if any(parameters.get(key) != value for key, value in expected.items()):
            wanted = ", ".join(f"{key}={value}" for key, value in expected.items())
            return f"cost-comparison 기간이 요청한 월과 다릅니다. 다음 값으로만 호출하세요: {wanted}"
        metric = parameters.get("metric_for_comparison")
        if not isinstance(metric, str) or not metric.strip():
            return "metric_for_comparison를 문자열로 지정하세요."
        with self._lock:
            if operation in self._comparison_operations or len(self._comparison_operations) >= 2:
                return "cost-comparison 중복 또는 초과 조회가 차단됐습니다."
            self._comparison_operations.add(operation)
            self.snapshot.comparison_requests.append(
                {"operation": operation, "metric_for_comparison": metric, **expected}
            )
        return None

    def process_comparison(self, operation: str, payload: Any) -> dict[str, Any]:
        """Shared live/mock path: validate the server shape, then store the payload unchanged."""
        comparison_data(operation, payload)
        self.snapshot.cost_comparison_raw[operation] = deepcopy(payload)
        return self.snapshot.cost_comparison_raw[operation]

    def process_optimization(self, payload: dict[str, Any]) -> FilterResult:
        """Shared live/mock filter path; store source before any transformation."""
        self.snapshot.optimization_raw = deepcopy(payload)
        result = filter_recommendations(payload, self.context)
        self.snapshot.included_recommendations = result.included
        self.snapshot.excluded_recommendations = result.excluded
        self.snapshot.optimization_succeeded = True
        return result

    def after_tool_call(self, event: AfterToolCallEvent) -> None:
        """Replace the optimization result before it is appended to model history."""
        if event.cancel_message:
            return
        name = event.tool_use["name"]
        if name not in ALLOWED_TOOLS:
            return
        original = deepcopy(event.result)
        if not isinstance(original, dict):
            message = f"{name} 결과가 ToolResult 객체가 아닙니다."
            logger.error("%s", message)
            self.snapshot.errors.append(message)
            if name == "cost-optimization":
                self.snapshot.optimization_raw = original
            event.result = ToolResult(
                toolUseId=event.tool_use.get("toolUseId", "unknown"),
                status="error", content=[{"text": message}],
            )
            return
        if name == "cost-comparison":
            parameters = event.tool_use.get("input", {})
            operation = parameters.get("operation") if isinstance(parameters, dict) else None
            try:
                if operation not in COMPARISON_OPERATIONS:
                    raise FilteringError("operation을 확인할 수 없습니다.")
                self.process_comparison(operation, decode_tool_payload(original))
            except (FilteringError, ComparisonPayloadError) as exc:
                self.snapshot.errors.append(f"cost-comparison {operation or '(unknown)'} 결과 확인 실패: {exc}")
            return
        # Capture even malformed results in memory. Never pass them through to the LLM.
        self.snapshot.optimization_raw = original
        try:
            payload = decode_tool_payload(original)
            recommendation_items(payload)
            result = self.process_optimization(payload)
            filtered = {
                "included_recommendations": result.included,
                "excluded_recommendations": result.excluded,
            }
            event.result = ToolResult(
                toolUseId=original.get("toolUseId", event.tool_use["toolUseId"]),
                status="success",
                content=[{"json": filtered}],
            )
        except (FilteringError, TypeError, ValueError) as exc:
            message = f"cost-optimization 결과 처리 실패: {exc}"
            logger.error("%s", message)
            self.snapshot.errors.append(message)
            self.snapshot.optimization_succeeded = False
            self.snapshot.included_recommendations = []
            self.snapshot.excluded_recommendations = []
            event.result = ToolResult(
                toolUseId=original.get("toolUseId", event.tool_use["toolUseId"]),
                status="error",
                content=[{"text": message + " 원본 데이터는 리포트에 사용하지 않습니다."}],
            )

    def before_tool_call(self, event: BeforeToolCallEvent) -> None:
        # Strands' Pydantic structured-output tool is internal, not an MCP tool.
        # It must remain usable for the final ReviewAnalysis response.
        if getattr(getattr(event, "selected_tool", None), "tool_type", None) == "structured_output":
            return
        name = event.tool_use["name"]
        if name not in ALLOWED_TOOLS:
            event.cancel_tool = "허용되지 않은 tool입니다."
            return

        parameters = event.tool_use.get("input", {})
        if not isinstance(parameters, dict):
            event.cancel_tool = "tool 입력 형식이 올바르지 않습니다."
            return
        if name == "cost-optimization":
            with self._lock:
                if parameters.get("operation") != OPTIMIZATION_OPERATION:
                    event.cancel_tool = f"cost-optimization은 {OPTIMIZATION_OPERATION}만 허용됩니다."
                elif self._optimization_calls >= 1:
                    event.cancel_tool = "한 번의 리뷰에서 최적화 권고는 한 번만 조회합니다."
                else:
                    self._optimization_calls += 1
            return
        refusal = self.accept_comparison_request(parameters)
        if refusal:
            event.cancel_tool = refusal
