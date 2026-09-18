"""Read-only CLI entry point for one monthly Billing MCP review."""

import argparse
import json
import logging
import re
import sys
from datetime import date
from pathlib import Path

import yaml
from pydantic import ValidationError

from agent import ModelSetupError, make_agent, review_request
from billing_mcp import BillingMCPError, discover_billing_tools, make_billing_client, server_environment
from filters import load_exceptions
from hooks import ReadOnlyBillingHook
from models import ReviewAnalysis
from report import write_report


def parse_month(value: str) -> str:
    """Require a real calendar month in exact YYYY-MM form."""
    if re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", value) is None:
        raise argparse.ArgumentTypeError("월은 YYYY-MM 형식이어야 합니다.")
    try:
        date.fromisoformat(f"{value}-01")
    except ValueError as exc:
        raise argparse.ArgumentTypeError("유효하지 않은 월입니다.") from exc
    return value


def mock_analysis() -> ReviewAnalysis:
    """Fixture-backed prose, not an LLM or AWS response."""
    return ReviewAnalysis(
        summary="MOCK 예제 데이터에 대한 월간 비용 검토입니다. 실제 AWS 비용이 아닙니다.",
        cost_change_explanation="MOCK 비용 비교 fixture의 서비스별 원본 값을 참고하세요. "
        "실제 비용 변화나 원인은 이 실행에서 확인하지 않았습니다.",
        priority_explanation="MOCK 권고는 source의 estimatedMonthlySavings가 있는 항목을 "
        "내림차순으로 표시했습니다. 업무 예외 적용 여부는 Python의 정확 일치 규칙으로 결정했습니다.",
        limitations=["MOCK: AWS/MCP 및 Bedrock을 호출하지 않았습니다. 설명은 예제용 고정 문구입니다."],
    )


def run_mock(previous: str, current: str, hook: ReadOnlyBillingHook, root: Path) -> Path:
    comparison = json.loads((root / "fixtures/cost_comparison.json").read_text(encoding="utf-8"))
    optimization = json.loads((root / "fixtures/recommendations.json").read_text(encoding="utf-8"))
    if not comparison.get("synthetic") or not optimization.get("synthetic"):
        raise ValueError("MOCK fixture에 synthetic 표시가 없습니다.")
    if (comparison.get("previous_month"), comparison.get("current_month")) != (previous, current):
        raise ValueError("MOCK fixture의 비교 월이 CLI 입력과 다릅니다.")
    hook.record_cost_comparison(comparison)
    hook.process_optimization(optimization)
    return write_report(
        previous=previous, current=current, mode="MOCK", analysis=mock_analysis(),
        snapshot=hook.snapshot, directory=root / "reports",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AWS read-only monthly cost review")
    parser.add_argument("--previous", type=parse_month, required=True)
    parser.add_argument("--current", type=parse_month, required=True)
    parser.add_argument("--profile", help="AWS profile for the MCP server and Bedrock")
    parser.add_argument("--region", help="AWS region; defaults to AWS_REGION or us-east-1")
    parser.add_argument("--mock", action="store_true", help="Offline fixture mode; no AWS/MCP/model calls")
    parser.add_argument("--discover-only", action="store_true", help="Connect and verify tools without AWS API/model calls")
    args = parser.parse_args(argv)
    if args.previous >= args.current:
        parser.error("--previous는 --current보다 이전 월이어야 합니다.")
    if args.mock and args.discover_only:
        parser.error("--mock과 --discover-only는 함께 사용할 수 없습니다.")
    root = Path(__file__).resolve().parent
    try:
        hook = ReadOnlyBillingHook(load_exceptions(root / "context/exceptions.yaml"))
    except (OSError, yaml.YAMLError, ValidationError, TypeError, ValueError) as exc:
        print(f"업무 예외 Context를 읽을 수 없습니다 ({type(exc).__name__}): {exc}", file=sys.stderr)
        return 2
    if args.mock:
        try:
            path = run_mock(args.previous, args.current, hook, root)
        except (OSError, ValueError, TypeError) as exc:
            print(f"MOCK 실행 실패 ({type(exc).__name__}): {exc}", file=sys.stderr)
            return 2
        print(f"MOCK report: {path}")
        print(f"included={len(hook.snapshot.included_recommendations)}, "
              f"excluded={len(hook.snapshot.excluded_recommendations)}")
        return 0

    env = server_environment(args.profile, args.region)
    # Strands logs initialization ExceptionGroups with full tracebacks before
    # returning an error; the CLI emits a concise actionable error below.
    sdk_logger = logging.getLogger("strands.tools.mcp.mcp_client")
    previous_log_level = sdk_logger.level
    sdk_logger.setLevel(logging.CRITICAL)
    try:
        client = make_billing_client(args.profile, args.region)
        with client:
            discovered, tools = discover_billing_tools(client)
            print(f"발견된 Billing MCP tool names: {', '.join(discovered)}", file=sys.stderr)
            agent = make_agent(
                tools,
                profile=env.get("AWS_PROFILE"),
                region=env["AWS_REGION"],
                hook=hook,
                check_credentials=not args.discover_only,
            )
            print(f"Agent 노출 tool names: {', '.join(sorted(agent.tool_names))}", file=sys.stderr)
            if args.discover_only:
                return 0
            result = agent(
                review_request(args.previous, args.current), structured_output_model=ReviewAnalysis
            )
            if hook.snapshot.errors:
                raise ValueError("; ".join(hook.snapshot.errors))
            if not hook.snapshot.cost_comparison_raw or hook.snapshot.optimization_raw is None:
                raise ValueError("필수 cost-comparison/cost-optimization source가 모두 수집되지 않았습니다.")
            if not isinstance(result.structured_output, ReviewAnalysis):
                raise ValueError("LLM의 ReviewAnalysis structured output을 검증할 수 없습니다.")
            items = hook.snapshot.included_recommendations + hook.snapshot.excluded_recommendations
            missing_fields = [
                field for field in ("tags", "restartNeeded", "rollbackPossible", "source")
                if any(field not in item for item in items)
            ]
            prerequisites = (
                ["Billing MCP 응답에 없는 필드는 N/A로 표시했습니다: " + ", ".join(missing_fields)]
                if missing_fields else []
            )
            if hook.context.tag_rules and "tags" in missing_fields:
                prerequisites.append(
                    "Billing MCP가 tags를 전달하지 않은 권고의 태그 예외 적용 여부는 확인 불가입니다. "
                    "리소스 변경 전 사람이 별도로 확인하세요."
                )
            path = write_report(
                previous=args.previous, current=args.current, mode="LIVE",
                analysis=result.structured_output, snapshot=hook.snapshot,
                directory=root / "reports", prerequisites=prerequisites,
            )
            print(f"LIVE report: {path}")
            print(f"included={len(hook.snapshot.included_recommendations)}, "
                  f"excluded={len(hook.snapshot.excluded_recommendations)}")
            return 0
    except BillingMCPError as exc:
        print(f"Billing MCP 설정 오류: {exc}", file=sys.stderr)
    except ModelSetupError as exc:
        print(f"모델 설정 오류: {exc}", file=sys.stderr)
    except ValueError as exc:
        print(f"리뷰 데이터 검증 실패: {exc}", file=sys.stderr)
    except Exception as exc:
        print(
            f"Billing MCP 연결 또는 AWS 조회 실패 ({type(exc).__name__}). "
            "uvx, AWS 자격증명/권한, Bedrock 접근, Cost Explorer 및 "
            "Cost Optimization Hub 준비 상태를 확인하세요. 자동 활성화는 하지 않습니다. "
            "오프라인 검증은 --mock으로 실행할 수 있습니다.",
            file=sys.stderr,
        )
    finally:
        sdk_logger.setLevel(previous_log_level)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
