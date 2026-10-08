"""Read-only readiness checks for a live monthly review; nothing here changes AWS state.

Free checks run by default: caller identity, IAM policy simulation (when permitted),
Cost Optimization Hub enrollment and one recommendation page, Compute Optimizer enrollment,
Bedrock inference profile listing and model availability.
Opt-in checks that cost money: --ce (one Cost Explorer comparison request, about USD 0.01)
and --invoke (one tiny Bedrock Converse call, token pricing).
"""

import argparse
import os
import re
import sys
from typing import Any, Callable

import boto3
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError, ProfileNotFound

from models import ReviewPeriod

BILLING_REGION = "us-east-1"  # the Billing MCP server pins Cost Explorer and Cost Optimization Hub here
DEFAULT_MODEL_ID = "global.anthropic.claude-sonnet-4-6"  # Strands 1.56.0 default when MODEL_ID is unset
REQUIRED_ACTIONS = (
    "ce:GetCostAndUsageComparisons",
    "ce:GetCostComparisonDrivers",
    "cost-optimization-hub:ListRecommendations",
    "bedrock:InvokeModel",
    "bedrock:InvokeModelWithResponseStream",
)
PROFILE_PREFIXES = ("global.", "us.", "eu.", "apac.", "au.", "jp.", "us-gov.")
BOTO3_METHODS = (
    ("sts", "get_caller_identity"),
    ("iam", "simulate_principal_policy"),
    ("ce", "get_cost_and_usage_comparisons"),
    ("cost-optimization-hub", "list_enrollment_statuses"),
    ("cost-optimization-hub", "list_recommendations"),
    ("compute-optimizer", "get_enrollment_status"),
    ("bedrock", "list_inference_profiles"),
    ("bedrock", "get_foundation_model_availability"),
    ("bedrock", "get_use_case_for_model_access"),
    ("bedrock-runtime", "converse"),
    ("freetier", "get_account_plan_state"),
)


class CheckFailed(Exception):
    """A blocking problem; the live run would fail."""


class Warn(Exception):
    """Not blocking by itself, but worth confirming."""


class Skip(Exception):
    """The check could not run here."""


def mask_account(text: str) -> str:
    """Hide the middle of 12-digit account IDs in anything we print."""
    return re.sub(r"\d{12}", lambda match: match.group()[:4] + "****" + match.group()[-4:], text)


def principal_arn(identity_arn: str) -> str | None:
    """IAM user/role ARN usable by the policy simulator; None for root or unknown principals."""
    match = re.fullmatch(r"arn:aws[^:]*:sts::(\d{12}):assumed-role/([^/]+)/.+", identity_arn)
    if match:
        return f"arn:aws:iam::{match.group(1)}:role/{match.group(2)}"
    if re.fullmatch(r"arn:aws[^:]*:iam::\d{12}:(user|role)/.+", identity_arn):
        return identity_arn
    return None


def error_code(exc: ClientError) -> str:
    return exc.response.get("Error", {}).get("Code", "Unknown")


def error_message(exc: ClientError) -> str:
    return mask_account(exc.response.get("Error", {}).get("Message", ""))


def check_identity(session: boto3.Session) -> str:
    identity = session.client("sts").get_caller_identity()
    return mask_account(identity["Arn"])


def check_account_plan(session: boto3.Session) -> str:
    """New-experience accounts have a FREE or PAID plan; the free plan excludes cross-Region inference."""
    client = session.client("freetier", region_name=BILLING_REGION)
    try:
        state = client.get_account_plan_state()
    except ClientError as exc:
        code = error_code(exc)
        if code in ("AccessDenied", "AccessDeniedException"):
            raise Skip("freetier:GetAccountPlanState 권한이 없어 건너뜁니다. Billing 콘솔 홈에서 플랜을 확인하세요.") from exc
        raise Skip(f"플랜 조회 불가 ({code}). 기존 방식으로 만든 계정이면 해당 없음.") from exc
    credits = state.get("accountPlanRemainingCredits") or {}
    detail = (
        f"plan={state.get('accountPlanType')}, status={state.get('accountPlanStatus')}, "
        f"남은 크레딧={credits.get('amount')} {credits.get('unit', '')}".rstrip()
    )
    if state.get("accountPlanType") == "FREE":
        raise Warn(
            detail + ". 무료 플랜은 Bedrock의 global/geo cross-Region inference를 지원하지 않습니다. "
            "in-region 모델을 쓰거나 Billing 콘솔에서 Paid plan으로 전환하세요."
        )
    return detail


def check_policy_simulation(session: boto3.Session) -> str:
    arn = principal_arn(session.client("sts").get_caller_identity()["Arn"])
    if arn is None:
        raise Skip("root 또는 알 수 없는 principal이라 policy simulation을 건너뜁니다.")
    try:
        results = session.client("iam").simulate_principal_policy(
            PolicySourceArn=arn, ActionNames=list(REQUIRED_ACTIONS)
        )
    except ClientError as exc:
        if error_code(exc) in ("AccessDenied", "AccessDeniedException"):
            raise Skip("iam:SimulatePrincipalPolicy 권한이 없어 건너뜁니다.") from exc
        raise
    denied = [
        result["EvalActionName"] for result in results.get("EvaluationResults", [])
        if result.get("EvalDecision") != "allowed"
    ]
    if denied:
        raise CheckFailed("허용되지 않은 action: " + ", ".join(denied))
    return "필수 action 5개 모두 allowed (IAM/SCP 기준; 모델 구독 상태는 별도)"


def check_cost_explorer(session: boto3.Session, period: ReviewPeriod) -> str:
    dates = period.tool_dates()
    client = session.client("ce", region_name=BILLING_REGION)
    try:
        response = client.get_cost_and_usage_comparisons(
            BaselineTimePeriod={"Start": dates["baseline_start_date"], "End": dates["baseline_end_date"]},
            ComparisonTimePeriod={"Start": dates["comparison_start_date"], "End": dates["comparison_end_date"]},
            MetricForComparison="UnblendedCost",
            MaxResults=1,
        )
    except ClientError as exc:
        code = error_code(exc)
        if code == "AccessDeniedException" and "not enabled" in error_message(exc).lower():
            raise CheckFailed("Cost Explorer가 활성화되지 않았습니다. 콘솔에서 Launch Cost Explorer 후 최대 24시간 대기.") from exc
        if code == "DataUnavailableException":
            raise CheckFailed("요청한 월의 데이터가 없습니다 (최근 13개월, 활성화 후 24시간 이상 경과 필요).") from exc
        raise
    total = response.get("TotalCostAndUsage", {}).get("UnblendedCost", {})
    return (
        f"baseline={total.get('BaselineTimePeriodAmount')} comparison={total.get('ComparisonTimePeriodAmount')} "
        f"{total.get('Unit')} (Cost Explorer 요청 1건, 약 USD 0.01 과금)"
    )


def check_optimization_hub(session: boto3.Session) -> str:
    client = session.client("cost-optimization-hub", region_name=BILLING_REGION)
    items = client.list_enrollment_statuses(includeOrganizationInfo=False).get("items", [])
    status = items[0].get("status") if items else None
    if status != "Active":
        raise CheckFailed(f"enrollment status={status}. 콘솔 Cost Optimization Hub에서 Enable 하세요 (자동 활성화 안 함).")
    count = len(client.list_recommendations(maxResults=1).get("items", []))
    note = "" if count else " (권고 0건: 활성화 후 최대 24시간 소요이거나 권고가 없음)"
    return f"enrollment Active, 권고 샘플 {count}건{note}"


def check_compute_optimizer(session: boto3.Session) -> str:
    status = session.client("compute-optimizer", region_name=BILLING_REGION).get_enrollment_status().get("status")
    if status != "Active":
        raise Warn(f"status={status}. Compute Optimizer 기반 rightsizing 권고는 Hub에 수집되지 않습니다.")
    return "Active"


def _use_case_form_status(client: Any) -> str:
    """Anthropic models need a one-time use case form per account (PutUseCaseForModelAccess)."""
    try:
        submitted = bool(client.get_use_case_for_model_access().get("formData"))
    except ClientError as exc:
        code = error_code(exc)
        if code in ("AccessDenied", "AccessDeniedException"):
            return "use case form 상태 확인 불가 (bedrock:GetUseCaseForModelAccess 권한 없음)"
        if code != "ResourceNotFoundException":
            return f"use case form 상태 확인 불가 ({code})"
        submitted = False
    if not submitted:
        raise CheckFailed(
            "Anthropic use case form이 제출되지 않았습니다. Bedrock 콘솔(us-east-1) Model catalog에서 "
            "Claude 모델을 선택해 use case details를 제출하세요. 제출 직후 호출할 수 있습니다."
        )
    return "Anthropic use case form 제출됨"


def check_bedrock_model(session: boto3.Session, region: str, model_id: str) -> str:
    client = session.client("bedrock", region_name=region)
    details: list[str] = []
    base_model_id = model_id
    if model_id.startswith(PROFILE_PREFIXES):
        active: set[str] = set()
        token = None
        while True:
            kwargs: dict[str, Any] = {"typeEquals": "SYSTEM_DEFINED"}
            if token:
                kwargs["nextToken"] = token
            page = client.list_inference_profiles(**kwargs)
            active.update(
                summary["inferenceProfileId"] for summary in page.get("inferenceProfileSummaries", [])
                if summary.get("status") == "ACTIVE"
            )
            token = page.get("nextToken")
            if not token:
                break
        if model_id not in active:
            similar = sorted(item for item in active if "anthropic" in item)[:8]
            raise CheckFailed(
                f"{region}에서 inference profile {model_id}를 찾을 수 없습니다. "
                f"MODEL_ID 후보: {', '.join(similar) or '(없음)'}"
            )
        details.append(f"{region}에서 {model_id} ACTIVE")
        base_model_id = model_id.split(".", 1)[1]
    try:
        availability = client.get_foundation_model_availability(modelId=base_model_id)
    except ClientError as exc:
        details.append(f"{base_model_id} availability 조회 불가 ({error_code(exc)})")
        raise Warn("; ".join(details) + ". --invoke로 실제 호출을 확인하세요.") from exc
    agreement = availability.get("agreementAvailability", {}).get("status")
    details.append(
        f"{base_model_id}: agreement={agreement}, authorization={availability.get('authorizationStatus')}, "
        f"entitlement={availability.get('entitlementAvailability')}, region={availability.get('regionAvailability')}"
    )
    if availability.get("authorizationStatus") == "NOT_AUTHORIZED":
        raise CheckFailed("; ".join(details) + ". 모델 제공자 승인(authorization)이 없습니다. Bedrock 콘솔 Model access를 확인하세요.")
    if "anthropic" in base_model_id:
        details.append(_use_case_form_status(client))
    if agreement != "AVAILABLE":
        raise Warn(
            "; ".join(details)
            + ". 첫 호출 때 자동 구독을 시도합니다(aws-marketplace:Subscribe 권한 필요). --invoke로 확인하세요."
        )
    return "; ".join(details)


def check_invoke(session: boto3.Session, region: str, model_id: str) -> str:
    client = session.client("bedrock-runtime", region_name=region)
    try:
        response = client.converse(
            modelId=model_id,
            messages=[{"role": "user", "content": [{"text": "Reply with the single word OK."}]}],
            inferenceConfig={"maxTokens": 8},
        )
    except ClientError as exc:
        hints = {
            "AccessDeniedException": "모델 접근 거부: Marketplace 자동 구독 실패, 결제 수단 없음, 또는 bedrock:InvokeModel 권한 부족.",
            "ValidationException": "Anthropic use case form 미제출이거나 model id/region 불일치일 수 있습니다.",
            "ResourceNotFoundException": "해당 region에서 model id를 찾을 수 없습니다.",
        }
        hint = hints.get(error_code(exc), "")
        if "Error 002" in error_message(exc):
            hint = (
                "계정 수준 Bedrock 차단(Error 002). IAM·모델 접근 설정으로는 해결되지 않습니다. "
                "--model-id amazon.nova-pro-v1:0 --invoke 로 in-region 모델도 실패하면 "
                "AWS Support(Account and billing) 케이스를 여세요."
            )
        raise CheckFailed(
            f"{error_code(exc)}: {error_message(exc)}" + (f" | 힌트: {hint}" if hint else "")
        ) from exc
    usage = response.get("usage", {})
    return f"응답 수신 (inputTokens={usage.get('inputTokens')}, outputTokens={usage.get('outputTokens')})"


def run(name: str, func: Callable[[], str]) -> bool:
    """Print one line per check; only FAIL affects the exit code."""
    try:
        detail = func()
    except Skip as exc:
        print(f"SKIP  {name} | {exc}")
        return True
    except Warn as exc:
        print(f"WARN  {name} | {exc}")
        return True
    except CheckFailed as exc:
        print(f"FAIL  {name} | {exc}")
        return False
    except ClientError as exc:
        print(f"FAIL  {name} | {error_code(exc)}: {error_message(exc)}")
        return False
    except (NoCredentialsError, ProfileNotFound, BotoCoreError) as exc:
        print(f"FAIL  {name} | {type(exc).__name__}: {mask_account(str(exc))}")
        return False
    print(f"OK    {name} | {detail}")
    return True


def dry_run(session: boto3.Session, region: str) -> bool:
    """Verify the installed boto3 exposes every API this script uses; no network."""
    ok = True
    for service, method in BOTO3_METHODS:
        client = session.client(service, region_name=region)
        present = callable(getattr(client, method, None))
        ok = ok and present
        print(f"{'OK   ' if present else 'FAIL '} boto3 {service}.{method}")
    return ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only readiness checks for the live cost review")
    parser.add_argument("--profile", help="AWS profile (same as app.py --profile)")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION") or "us-east-1", help="Bedrock region")
    parser.add_argument("--model-id", default=os.environ.get("MODEL_ID") or DEFAULT_MODEL_ID)
    parser.add_argument("--previous", help="YYYY-MM, required with --ce")
    parser.add_argument("--current", help="YYYY-MM, required with --ce")
    parser.add_argument("--ce", action="store_true", help="Also send one Cost Explorer comparison request (about USD 0.01)")
    parser.add_argument("--invoke", action="store_true", help="Also send one tiny Bedrock Converse request (token pricing)")
    parser.add_argument("--dry-run", action="store_true", help="Only verify boto3 API availability; no AWS calls")
    args = parser.parse_args(argv)
    if args.ce and not (args.previous and args.current):
        parser.error("--ce에는 --previous와 --current가 필요합니다.")
    try:
        session = boto3.Session(profile_name=args.profile, region_name=args.region)
        period = ReviewPeriod(args.previous, args.current) if args.ce else None
    except (ProfileNotFound, BotoCoreError, ValueError) as exc:
        print(f"FAIL  설정 | {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    if args.dry_run:
        return 0 if dry_run(session, args.region) else 1

    results = [
        run("자격증명 (sts:GetCallerIdentity)", lambda: check_identity(session)),
        run("계정 플랜 (freetier:GetAccountPlanState)", lambda: check_account_plan(session)),
        run("IAM policy simulation", lambda: check_policy_simulation(session)),
        run("Cost Optimization Hub 등록·권고 조회", lambda: check_optimization_hub(session)),
        run("Compute Optimizer 등록", lambda: check_compute_optimizer(session)),
        run(f"Bedrock 모델 ({args.model_id}, {args.region})", lambda: check_bedrock_model(session, args.region, args.model_id)),
    ]
    if period is not None:
        results.append(run("Cost Explorer 비교 조회 (유료)", lambda: check_cost_explorer(session, period)))
    else:
        print("SKIP  Cost Explorer 비교 조회 | --ce --previous YYYY-MM --current YYYY-MM 로 실행하면 1건(약 USD 0.01) 호출합니다.")
    if args.invoke:
        results.append(run("Bedrock Converse 호출 (유료)", lambda: check_invoke(session, args.region, args.model_id)))
    else:
        print("SKIP  Bedrock Converse 호출 | --invoke 로 실행하면 짧은 요청 1건을 보냅니다.")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
