"""Single Strands Agent for read-only monthly cost review."""

import os

import boto3
from botocore.exceptions import BotoCoreError, NoCredentialsError, ProfileNotFound
from strands import Agent
from strands.models import BedrockModel

from billing_mcp import ALLOWED_TOOLS
from hooks import ReadOnlyBillingHook
from models import ReviewPeriod

SYSTEM_PROMPT = """You are a read-only AWS cost review assistant. Follow these rules:
1. For a monthly review call only the two supplied Billing MCP tools.
2. Use cost-comparison exactly twice: operation=getCostAndUsageComparisons for the month-to-month
   comparison and operation=getCostComparisonDrivers for the major cost drivers. Use exactly the
   baseline/comparison dates given in the request (first day of the month to the first day of the
   next month) and the metric_for_comparison named there. Python cancels calls with other dates;
   if a call is cancelled, retry once with the dates from the cancellation message.
3. Use cost-optimization only with operation=list_recommendations from Cost Optimization Hub.
4. Never calculate, infer, or invent costs or savings that AWS did not return.
   Do not write monetary amounts in your narrative; Python renders numeric tables from the source.
   You may name months such as 2026-07.
5. Python has already applied exact business exclusions to the cost-optimization result.
   Do not reclassify, add, or remove recommendations. Do not transform source fields.
6. Never claim an AWS resource or account setting was changed. Recommendations are proposals;
   a human decides whether to act on them.
7. If data is absent or inaccessible, say '데이터 없음/확인 불가' instead of guessing.
8. Avoid unnecessary Cost Explorer calls. Never repeat an API call for the same purpose
   within one monthly review. If a tool fails, report the failure; do not try another tool.
9. Do not enable Cost Explorer or Cost Optimization Hub, change IAM, modify resources,
   or purchase Savings Plans or Reserved Instances.
"""


class ModelSetupError(RuntimeError):
    """Bedrock model credentials or configuration are unavailable."""


def make_agent(
    tools: list[object], *, profile: str | None, region: str,
    hook: ReadOnlyBillingHook | None = None, check_credentials: bool = True,
) -> Agent:
    """Keep the default Bedrock provider; optionally select MODEL_ID."""
    try:
        session = boto3.Session(profile_name=profile, region_name=region)
        model_id = os.environ.get("MODEL_ID")
        model_kwargs = {"model_id": model_id} if model_id else {}
        model = BedrockModel(boto_session=session, **model_kwargs)
    except (NoCredentialsError, ProfileNotFound, BotoCoreError) as exc:
        raise ModelSetupError(
            "AWS 자격증명 또는 지정한 profile을 사용할 수 없습니다. "
            "읽기 권한이 있는 AWS profile/credential과 Bedrock 모델 접근 권한을 설정하세요."
        ) from exc

    agent = Agent(
        model=model,
        tools=tools,
        system_prompt=SYSTEM_PROMPT,
        hooks=[hook or ReadOnlyBillingHook()],
        load_tools_from_directory=False,
        callback_handler=None,
    )
    if set(agent.tool_names) != ALLOWED_TOOLS:
        raise ModelSetupError(f"Agent tool 목록이 허용 목록과 다릅니다: {agent.tool_names}")
    if check_credentials:
        try:
            if session.get_credentials() is None:
                raise NoCredentialsError()
        except (NoCredentialsError, BotoCoreError) as exc:
            raise ModelSetupError(
                "AWS 자격증명을 찾을 수 없습니다. 읽기 권한이 있는 AWS profile/credential과 "
                "Bedrock 모델 접근 권한을 설정하세요."
            ) from exc
    return agent


def review_request(period: ReviewPeriod) -> str:
    """Ask for one review; the hook enforces the period and a bounded read-only tool budget."""
    dates = ", ".join(f"{key}={value}" for key, value in period.tool_dates().items())
    return (
        f"{period.previous}와 {period.current}의 AWS 월간 비용을 비교하고 주요 cost driver와 "
        "Cost Optimization Hub의 list_recommendations 권고를 요약하세요. "
        f"cost-comparison은 {dates}, metric_for_comparison=UnblendedCost로 "
        "operation=getCostAndUsageComparisons(group_by='[{\"Type\": \"DIMENSION\", \"Key\": \"SERVICE\"}]')와 "
        "operation=getCostComparisonDrivers를 각각 정확히 한 번 호출하세요. "
        "Python이 적용한 업무 예외 결과를 재판단하지 마세요. 금액·리소스 표는 만들지 말고 "
        "비용 변화와 검토 우선순위, 한계만 서술하세요."
    )
