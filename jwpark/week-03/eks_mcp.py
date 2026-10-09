"""
EKS Addon 관리 에이전트 (Llama-3.2-1B-Instruct + EKS MCP Server, 프레임워크 없이 Pydantic 기반)

신뢰성 원칙:
    1B 모델은 "질문의 의도"만 고른다. 인자 결정, 사실 판단, 답변 작성은 코드가 한다.
    - 의도    : few-shot 예시 대화로 라벨 한 단어를 여러 번 생성 → 다수결 (self-consistency)
    - 인자    : addon 이름/버전은 질문에서 코드로 추출하고 실제 addon 목록과 대조
    - 대체    : 의도 득표율이 낮으면 JSON 도구 호출을 여러 번 생성 → 검증 통과 후보끼리 다수결
    - 판정    : 설치/업그레이드 가능 여부, 파드 정상 여부는 AWS/쿠버네티스 API 결과로 코드가 판정
    - 답변    : 구조를 아는 결과는 템플릿으로 출력. 그 외 MCP 결과만 모델이 요약하되 원본에 없는 값이 있으면 폐기

구성:
    AgentConfig     : 에이전트 설정 (frozen)
    LocalLLM        : HF 로컬 모델, Llama 3.2 기본 tool-calling 템플릿 사용
    AddonTools      : EKS Addon API(boto3) 도구 + 판정 로직 (MCP 서버에는 addon 도구가 없음)
    PodTools        : MCP로 파드 목록 + 파드별 status 조회 (read 작업만)
    Toolbox         : MCP 도구 + 로컬 도구를 ToolSpec으로 통합, 인자 검증/실행
    EksAddonAgent   : 의도 분류(다수결) → 계획 → 실행(쓰기는 승인) → 답변

사용법:
    python eks_mcp.py --cluster my-prod-cluster "coredns 업그레이드 가능해?"
    python eks_mcp.py --cluster my-prod-cluster --sensitive "aws-node 파드 이벤트 확인해줘"

필요 패키지:
    pip install transformers torch pydantic mcp "boto3[crt]"
"""

import argparse
import asyncio
import difflib
import json
import os
import re
from collections import Counter
from typing import Any, Awaitable, Callable, Dict, List, Literal, Optional, Tuple, Type

import boto3
import torch
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    create_model,
    field_validator,
    model_validator,
)
from transformers import AutoModelForCausalLM, AutoTokenizer


# ---------------------------------------------------------------------------
# 1. 설정 / 공통 데이터 모델
# ---------------------------------------------------------------------------

# 1B 모델이 헷갈리지 않도록 Addon 관리에 필요한 MCP 도구만 노출
DEFAULT_MCP_TOOLS = (
    "list_k8s_resources",
    "get_k8s_events",
    "get_pod_logs",
    "get_eks_insights",
    "search_eks_troubleshoot_guide",
)
# MCP 서버를 --allow-sensitive-data-access 로 띄워야 동작하는 도구
SENSITIVE_MCP_TOOLS = {"get_k8s_events", "get_pod_logs"}
# LLM에 숨기는 인자: cluster_name은 코드가 주입, 나머지는 1B 모델이 설명 속 예시값을 복사해 오용함
HIDDEN_MCP_ARGS = ("cluster_name", "field_selector", "next_token")


class AgentConfig(BaseModel):
    cluster_name: str = Field(min_length=1, description="대상 EKS 클러스터 (LLM이 아닌 코드가 주입)")
    aws_region: str = Field(default="ap-northeast-2")

    model_name: str = Field(default="meta-llama/Llama-3.2-1B-Instruct")
    max_new_tokens: int = Field(default=512)
    observation_chars: int = Field(default=2000, description="요약용으로 LLM에 넘길 도구 결과 최대 길이")

    # 신뢰성 설정: 모델을 여러 번 호출해 다수결/검증
    route_samples: int = Field(default=5, ge=0, le=20, description="greedy 1회 외에 추가로 샘플링할 후보 수 (다수결)")
    min_intent_confidence: float = Field(default=0.5, ge=0, le=1, description="의도 분류 채택 최소 득표율 (미달 시 도구 호출 방식)")
    min_write_confidence: float = Field(default=0.5, ge=0, le=1, description="update 의도 실행 최소 득표율 (미달 시 조회로 강등)")
    summary_candidates: int = Field(default=3, ge=1, le=10, description="MCP 결과 요약 후보 수 (검증 통과 1개 채택)")

    # uvx에 boto3[crt] 의존성 주입하여 AWS SSO 에러 방지, 버전 고정
    mcp_command: str = Field(default="uvx")
    mcp_package: str = Field(default="awslabs.eks-mcp-server==0.2.1")
    mcp_tools: Tuple[str, ...] = Field(default=DEFAULT_MCP_TOOLS)
    allow_sensitive_data_access: bool = Field(default=False, description="파드 로그/이벤트 조회 허용")
    auto_approve: bool = Field(default=False, description="쓰기 작업을 승인 없이 실행 (주의)")

    model_config = ConfigDict(frozen=True)


class ToolSpec(BaseModel):
    """LLM에 노출되는 도구 하나. MCP/로컬 도구를 같은 형태로 다룬다."""
    name: str
    description: str
    args_model: Type[BaseModel]
    read_only: bool = True
    source: Literal["mcp", "local"]
    inject_cluster: bool = False  # 실행 시 cluster_name을 코드에서 채워 넣을지 여부

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    def to_llm_schema(self) -> Dict[str, Any]:
        """1B 모델용 축약 스키마 (title/anyOf 등 군더더기 제거)"""
        properties, required = {}, []
        for field_name, field in self.args_model.model_fields.items():
            prop: Dict[str, Any] = {"type": _JSON_TYPE_NAMES.get(_unwrap_optional(field.annotation), "string")}
            if field.description:
                prop["description"] = field.description
            properties[field_name] = prop
            if field.is_required():
                required.append(field_name)
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {"type": "object", "properties": properties, "required": required},
            },
        }


class ToolCall(BaseModel):
    name: str
    # Llama 3.2는 "parameters", OpenAI 형식은 "arguments"를 쓰므로 둘 다 허용
    arguments: Dict[str, Any] = Field(
        default_factory=dict, validation_alias=AliasChoices("parameters", "arguments")
    )

    @model_validator(mode="before")
    @classmethod
    def unwrap_function(cls, data: Any) -> Any:
        # 1B 모델이 스키마 형태를 흉내 내는 경우 처리
        if not isinstance(data, dict):
            return data
        function = data.get("function")
        if isinstance(function, dict):  # {"type": "function", "function": {"name": ..., "parameters": ...}}
            return function
        if isinstance(function, str) and "name" not in data:  # {"function": "tool_name", "parameters": ...}
            return {**data, "name": function}
        return data


class Observation(BaseModel):
    ok: bool
    content: str                    # 원본 결과 (MCP 텍스트 또는 로컬 결과 JSON)
    rendered: Optional[str] = None  # 코드가 만든 사람용 답변 (있으면 LLM 요약 생략)


class RouteDecision(BaseModel):
    """다수결 라우팅 결과"""
    method: Literal["intent", "tool_call"]
    label: Optional[str] = None  # intent 방식일 때 분류된 의도
    note: Optional[str] = None   # 답변 앞에 붙일 안내 (강등 사유 등)
    tool: Optional[str] = None
    arguments: Dict[str, Any] = Field(default_factory=dict)
    votes: int = 0
    total: int = 0
    tally: Dict[str, int] = Field(default_factory=dict)  # 후보별 득표 (디버깅용)

    @property
    def confidence(self) -> float:
        return self.votes / self.total if self.total else 0.0


class AgentResult(BaseModel):
    question: str
    route: RouteDecision
    observation: Optional[Observation] = None
    answer: str


# ---------------------------------------------------------------------------
# 2. JSON Schema → Pydantic 모델 변환
# ---------------------------------------------------------------------------

_JSON_TYPES: Dict[str, type] = {
    "string": str, "integer": int, "number": float, "boolean": bool, "array": list, "object": dict,
}
_JSON_TYPE_NAMES = {v: k for k, v in _JSON_TYPES.items()}


def _unwrap_optional(annotation: Any) -> Any:
    args = [a for a in getattr(annotation, "__args__", ()) if a is not type(None)]
    return args[0] if args else annotation


def _resolve_type(spec: Dict[str, Any]) -> type:
    if "type" in spec:
        return _JSON_TYPES.get(spec["type"], str)
    for option in spec.get("anyOf", []):  # Optional[str] → anyOf: [{string}, {null}]
        if option.get("type") not in (None, "null"):
            return _JSON_TYPES.get(option["type"], str)
    return str


def model_from_json_schema(name: str, schema: Dict[str, Any], drop: Tuple[str, ...] = ()) -> Type[BaseModel]:
    """MCP inputSchema로 검증용 Pydantic 모델 생성. 모르는 키는 무시(1B 모델의 잡음 제거)."""
    required = set(schema.get("required", []))
    fields: Dict[str, Any] = {}
    for prop, spec in schema.get("properties", {}).items():
        if prop in drop:
            continue
        py_type = _resolve_type(spec)
        description = (spec.get("description") or "").split("\n")[0].strip() or None
        if prop in required:
            fields[prop] = (py_type, Field(description=description))
        else:
            fields[prop] = (Optional[py_type], Field(default=spec.get("default"), description=description))
    return create_model(f"{name}_args", __config__=ConfigDict(extra="ignore"), **fields)


def _short_description(text: Optional[str], limit: int = 200) -> str:
    """MCP 도구 설명은 매우 길어서 첫 문단만 사용"""
    first = (text or "").strip().split("\n\n")[0]
    return " ".join(first.split())[:limit]


# ---------------------------------------------------------------------------
# 3. 로컬 LLM
# ---------------------------------------------------------------------------

# 도구 호출 1순위 후보는 Greedy. repetition_penalty는 JSON 따옴표/괄호를 깨뜨려 사용하지 않음
TOOL_CALL_GREEDY: Dict[str, Any] = {"do_sample": False}
# 다수결용 추가 후보: 다양한 후보를 뽑아야 투표가 의미 있으므로 온도를 높임
TOOL_CALL_SAMPLING: Dict[str, Any] = {"do_sample": True, "temperature": 0.7, "top_p": 0.95}
# 자연어 답변은 Greedy 시 1B 모델이 같은 문장을 무한 반복하므로 반복 억제 + 낮은 온도 샘플링
ANSWER_GENERATION: Dict[str, Any] = {
    "do_sample": True,
    "temperature": 0.2,
    "top_p": 0.9,
    "repetition_penalty": 1.2,
    "no_repeat_ngram_size": 4,
}


class LocalLLM:
    def __init__(self, model_name: str, max_new_tokens: int):
        self.max_new_tokens = max_new_tokens
        self.tokenizer = AutoTokenizer.from_pretrained(model_name)
        self.model = AutoModelForCausalLM.from_pretrained(
            model_name,
            device_map="auto",
            dtype=torch.bfloat16,
        )

    def _generate_sync(
        self,
        messages: List[Dict[str, Any]],
        generation: Dict[str, Any],
        tools: Optional[List[Dict]],
        n: int,
        max_new_tokens: Optional[int],
    ) -> List[str]:
        inputs = self.tokenizer.apply_chat_template(
            messages,
            tools=tools,  # Llama 3.2 기본 tool-calling 프롬프트 사용
            add_generation_prompt=True,
            tokenize=True,
            return_dict=True,
            return_tensors="pt",
        ).to(self.model.device)

        outputs = self.model.generate(
            **inputs,
            max_new_tokens=max_new_tokens or self.max_new_tokens,
            pad_token_id=self.tokenizer.eos_token_id,
            num_return_sequences=n,  # 프롬프트를 한 번만 인코딩하고 후보 n개를 배치로 생성
            **generation,
        )
        prompt_len = inputs["input_ids"].shape[-1]
        return [
            self.tokenizer.decode(out[prompt_len:], skip_special_tokens=True, clean_up_tokenization_spaces=False).strip()
            for out in outputs
        ]

    async def generate(
        self,
        messages: List[Dict[str, Any]],
        generation: Dict[str, Any],
        tools: Optional[List[Dict]] = None,
        n: int = 1,
        max_new_tokens: Optional[int] = None,
    ) -> List[str]:
        # generate()는 블로킹이므로 이벤트 루프(MCP stdio 통신)를 막지 않도록 스레드에서 실행
        return await asyncio.to_thread(self._generate_sync, messages, generation, tools, n, max_new_tokens)


def parse_tool_call(text: str) -> Optional[ToolCall]:
    """LLM 출력에서 첫 JSON 객체를 ToolCall로 파싱. 도구 호출이 아니면 None."""
    clean = text.replace("```json", "").replace("```", "")
    start = clean.find("{")
    if start == -1:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(clean[start:])
        return ToolCall.model_validate(obj)
    except (json.JSONDecodeError, ValidationError):
        return None


# ---------------------------------------------------------------------------
# 4. EKS Addon 판정 로직 (모델이 아닌 코드가 판단)
# ---------------------------------------------------------------------------

_VERSION_RE = re.compile(r"v?(\d+)\.(\d+)\.(\d+)(?:-eksbuild\.(\d+))?")


def version_key(version: str) -> Tuple[int, ...]:
    """'v1.14.7-eksbuild.11' → (1, 14, 7, 11). 문자열 비교 시 eksbuild.11 < eksbuild.3 이 되는 문제 방지."""
    m = _VERSION_RE.match(version)
    if not m:
        return (-1,)
    return tuple(int(g or 0) for g in m.groups())


# 클러스터가 작업 중인 상태에서는 update_addon이 거부됨
BUSY_STATUSES = {"CREATING", "UPDATING", "DELETING"}

Verdict = Literal["upgradable", "up_to_date", "installable", "not_available", "blocked"]

VERDICT_LABELS: Dict[str, str] = {
    "upgradable": "업그레이드 가능",
    "up_to_date": "최신 버전 사용 중",
    "installable": "설치 가능 (미설치)",
    "not_available": "이 클러스터 버전에서 사용 불가",
    "blocked": "작업 진행 중이라 변경 불가",
}

# 사용자가 흔히 쓰는 이름 → 실제 addon 이름
ADDON_ALIASES = {
    "aws-node": "vpc-cni",
    "cni": "vpc-cni",
    "amazon-vpc-cni": "vpc-cni",
    "dns": "coredns",
    "ebs": "aws-ebs-csi-driver",
    "ebs-csi": "aws-ebs-csi-driver",
    "ebs-csi-driver": "aws-ebs-csi-driver",
    "efs": "aws-efs-csi-driver",
    "efs-csi-driver": "aws-efs-csi-driver",
    "pod-identity": "eks-pod-identity-agent",
    "pod-identity-agent": "eks-pod-identity-agent",
}

# addon → kube-system 파드 label selector (확실한 것만. 없으면 kube-system 전체 파드 조회)
POD_SELECTORS = {
    "vpc-cni": "k8s-app=aws-node",
    "coredns": "k8s-app=kube-dns",
    "kube-proxy": "k8s-app=kube-proxy",
    "eks-pod-identity-agent": "app.kubernetes.io/name=eks-pod-identity-agent",
}


def _compact(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def find_addon(question: str, known: List[str]) -> Optional[str]:
    """질문에서 addon 이름을 코드로 찾는다. 'ebs csi driver', 'pod identity agent', 'aws-node' 같은 표기도 처리.

    구분자를 모두 지운 문자열끼리 비교하고, 여러 개가 걸리면 가장 긴 이름을 채택 ('dns' < 'coredns').
    """
    keys: Dict[str, str] = {}
    for alias, target in ADDON_ALIASES.items():
        if target in known:
            keys[_compact(alias)] = target
    for name in known:
        keys[_compact(name)] = name
        for prefix in ("aws-", "eks-", "amazon-"):
            if name.startswith(prefix):
                keys.setdefault(_compact(name[len(prefix):]), name)
    q = _compact(question)
    matches = [k for k in keys if len(k) >= 3 and k in q]
    return keys[max(matches, key=len)] if matches else None


def extract_version(question: str) -> Optional[str]:
    m = re.search(r"v\d+\.\d+\.\d+(?:-eksbuild\.\d+)?", question)
    return m.group(0) if m else None


class ToolResult(BaseModel):
    """로컬 도구 결과의 공통 형태: 원본 JSON + 사람용 렌더링"""
    ok: bool = True

    def render(self) -> str:
        raise NotImplementedError


class AddonVersion(BaseModel):
    version: str
    default: bool = False  # EKS가 이 클러스터 버전에 기본으로 설치하는 버전
    requires_iam_permissions: bool = False
    requires_configuration: bool = False


class AddonAssessment(ToolResult):
    addon_name: str
    cluster_version: str
    installed: bool
    current_version: Optional[str] = None
    status: Optional[str] = None
    health_issues: List[str] = Field(default_factory=list)
    compatible_versions: List[AddonVersion] = Field(default_factory=list)  # 최신순
    newer_versions: List[str] = Field(default_factory=list)
    verdict: Verdict
    recommended_version: Optional[str] = None
    reasons: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)

    @property
    def default_version(self) -> Optional[str]:
        return next((v.version for v in self.compatible_versions if v.default), None)

    def render(self) -> str:
        lines = [f"[{self.addon_name}] {VERDICT_LABELS[self.verdict]}"]
        lines.append(f"- 클러스터 Kubernetes 버전: {self.cluster_version}")
        if self.installed:
            lines.append(f"- 현재 버전: {self.current_version} (상태: {self.status})")
        if self.compatible_versions:
            lines.append(
                f"- 최신 호환 버전: {self.compatible_versions[0].version}"
                f" / EKS 기본 버전: {self.default_version or '없음'}"
            )
        if self.recommended_version:
            lines.append(f"- 권장 버전: {self.recommended_version}")
        lines += [f"- 근거: {r}" for r in self.reasons]
        lines += [f"- 주의: {w}" for w in self.warnings]
        if self.compatible_versions:
            top = ", ".join(v.version for v in self.compatible_versions[:5])
            lines.append(f"- 호환 버전 (최신순 {min(5, len(self.compatible_versions))}개): {top}")
        return "\n".join(lines)


def judge_addon(
    addon_name: str,
    cluster_version: str,
    compatible: List[AddonVersion],
    installed: Optional[Dict[str, Any]],
) -> AddonAssessment:
    """AWS API 결과만으로 설치/업그레이드 가능 여부를 판정 (순수 함수 → 단독 테스트 가능)"""
    compatible = sorted(compatible, key=lambda v: version_key(v.version), reverse=True)
    base: Dict[str, Any] = {
        "addon_name": addon_name,
        "cluster_version": cluster_version,
        "installed": installed is not None,
        "compatible_versions": compatible,
    }

    if installed is None:
        if not compatible:
            return AddonAssessment(
                **base, verdict="not_available",
                reasons=[f"Kubernetes {cluster_version}과 호환되는 {addon_name} 버전이 없습니다."],
            )
        target = next((v for v in compatible if v.default), compatible[0])
        warnings = []
        if target.requires_iam_permissions:
            warnings.append("IAM 권한이 필요한 addon입니다. 설치 전 IRSA 또는 Pod Identity 역할을 준비하세요.")
        if target.requires_configuration:
            warnings.append("설치 시 추가 설정(configuration values)이 필요합니다.")
        return AddonAssessment(
            **base, verdict="installable", recommended_version=target.version, warnings=warnings,
            reasons=[
                f"EKS 관리형 addon으로 설치되어 있지 않으며, Kubernetes {cluster_version} 호환 버전이 {len(compatible)}개 있습니다.",
                "self-managed(Helm/매니페스트)로 이미 설치되어 있을 수 있으니 설치 전 kube-system을 확인하세요.",
            ],
        )

    current = installed["addonVersion"]
    status = installed["status"]
    health = [f"{i.get('code')}: {i.get('message')}" for i in installed.get("health", {}).get("issues", [])]
    base.update(current_version=current, status=status, health_issues=health)

    warnings: List[str] = []
    if compatible and current not in {v.version for v in compatible}:
        warnings.append(f"현재 버전 {current}은 Kubernetes {cluster_version} 호환 목록에 없습니다. 업그레이드가 필요합니다.")
    if health:
        warnings += [f"health 이슈 - {h}" for h in health]
        warnings.append("업그레이드로 해결되지 않을 수 있으니 health 이슈의 원인을 먼저 확인하세요.")

    if status in BUSY_STATUSES:
        return AddonAssessment(
            **base, verdict="blocked", warnings=warnings,
            reasons=[f"현재 {status} 상태입니다. 진행 중인 작업이 끝난 뒤 다시 확인하세요."],
        )

    newer = [v.version for v in compatible if version_key(v.version) > version_key(current)]
    if newer:
        return AddonAssessment(
            **base, verdict="upgradable", newer_versions=newer, recommended_version=newer[0], warnings=warnings,
            reasons=[f"현재 {current}보다 새로운 호환 버전이 {len(newer)}개 있습니다 (최신 {newer[0]})."],
        )
    return AddonAssessment(
        **base, verdict="up_to_date", warnings=warnings,
        reasons=[f"현재 {current}이 Kubernetes {cluster_version} 호환 버전 중 가장 최신입니다."],
    )


class AddonList(ToolResult):
    addons: List[AddonAssessment]

    def render(self) -> str:
        if not self.addons:
            return "클러스터에 설치된 EKS addon이 없습니다."
        lines = [f"설치된 EKS addon {len(self.addons)}개 (Kubernetes {self.addons[0].cluster_version})"]
        for a in self.addons:
            line = f"- {a.addon_name}: {a.current_version} [{a.status}] → {VERDICT_LABELS[a.verdict]}"
            if a.verdict == "upgradable":
                line += f" (최신 {a.recommended_version})"
            lines.append(line)
            lines += [f"    주의: {h}" for h in a.health_issues]
        return "\n".join(lines)


class UpdatePlan(ToolResult):
    assessment: AddonAssessment
    target_version: Optional[str] = None
    resolve_conflicts: str = "PRESERVE"
    allowed: bool
    reason: str
    rejected_by_user: bool = False

    def render(self) -> str:
        a = self.assessment
        if self.rejected_by_user:
            head = f"[{a.addon_name}] 사용자가 업데이트를 거부했습니다."
        elif not self.allowed:
            head = f"[{a.addon_name}] 업데이트를 진행할 수 없습니다: {self.reason}"
        else:
            head = (
                f"[{a.addon_name}] 업데이트 계획: {a.current_version} → {self.target_version}"
                f" (resolveConflicts={self.resolve_conflicts})"
            )
        return f"{head}\n\n{a.render()}"


class UpdateResult(ToolResult):
    addon_name: str
    from_version: str
    to_version: str
    update_id: str
    status: str

    def render(self) -> str:
        return (
            f"[{self.addon_name}] 업데이트 요청 완료: {self.from_version} → {self.to_version}\n"
            f"- update id: {self.update_id} (상태: {self.status})\n"
            f"- 진행 상황은 'aws eks describe-update' 또는 assess_addon으로 다시 확인하세요."
        )


# ---------------------------------------------------------------------------
# 5. 로컬 도구: EKS Addon API
# ---------------------------------------------------------------------------

Approver = Callable[[str], Awaitable[bool]]


class NoArgs(BaseModel):
    model_config = ConfigDict(extra="ignore")


def _normalize_addon_name(v: str, info: ValidationInfo) -> str:
    name = ADDON_ALIASES.get(v.strip().lower(), v.strip().lower())
    # 실제 존재하는 addon 이름과 대조 → 모델이 지어낸 이름은 다수결 후보에서 탈락
    known = (info.context or {}).get("known_addons")
    if known and name not in known:
        hints = difflib.get_close_matches(name, known, n=3)
        raise ValueError(f"unknown addon '{v}'" + (f", did you mean: {', '.join(hints)}" if hints else ""))
    return name


class AddonNameArgs(BaseModel):
    addon_name: str = Field(description="Addon name, e.g. vpc-cni, coredns, kube-proxy, aws-ebs-csi-driver")
    model_config = ConfigDict(extra="ignore")

    @field_validator("addon_name")
    @classmethod
    def normalize_addon_name(cls, v: str, info: ValidationInfo) -> str:
        return _normalize_addon_name(v, info)


class PodArgs(BaseModel):
    addon_name: Optional[str] = Field(default=None, description="Addon whose pods to check. Omit for all kube-system pods")
    model_config = ConfigDict(extra="ignore")

    @field_validator("addon_name")
    @classmethod
    def normalize_addon_name(cls, v: Optional[str], info: ValidationInfo) -> Optional[str]:
        return _normalize_addon_name(v, info) if v else None


class UpdateAddonArgs(AddonNameArgs):
    addon_version: Optional[str] = Field(
        default=None, description="Target version. Omit to use the latest compatible version"
    )
    resolve_conflicts: Literal["NONE", "OVERWRITE", "PRESERVE"] = Field(
        default="PRESERVE", description="How to handle config conflicts"
    )


LocalHandler = Callable[[BaseModel, Approver], Awaitable[ToolResult]]


class AddonTools:
    def __init__(self, config: AgentConfig):
        self.cluster = config.cluster_name
        self.eks = boto3.client("eks", region_name=config.aws_region)
        self._cluster_version: Optional[str] = None

    @property
    def cluster_version(self) -> str:
        if self._cluster_version is None:
            self._cluster_version = self.eks.describe_cluster(name=self.cluster)["cluster"]["version"]
        return self._cluster_version

    def specs(self) -> List[Tuple[ToolSpec, LocalHandler]]:
        return [
            (ToolSpec(name="list_addons",
                      description="List installed EKS addons with version, status and whether an upgrade is available.",
                      args_model=NoArgs, source="local"), self._list_addons),
            (ToolSpec(name="assess_addon",
                      description="Check one addon: status, health, compatible versions, and whether it can be "
                                  "installed or upgraded.",
                      args_model=AddonNameArgs, source="local"), self._assess_addon),
            (ToolSpec(name="update_addon",
                      description="Upgrade an installed EKS addon. Modifies the cluster.",
                      args_model=UpdateAddonArgs, read_only=False, source="local"), self._update_addon),
        ]

    # --- boto3 호출 (블로킹) ---

    def available_addons(self) -> List[str]:
        """이 클러스터 버전에서 사용 가능한 모든 addon 이름 (이름 검증용)"""
        names = set()
        paginator = self.eks.get_paginator("describe_addon_versions")
        for page in paginator.paginate(kubernetesVersion=self.cluster_version):
            names.update(a["addonName"] for a in page["addons"])
        return sorted(names)

    def assess(self, addon_name: str) -> AddonAssessment:
        k8s = self.cluster_version
        resp = self.eks.describe_addon_versions(addonName=addon_name, kubernetesVersion=k8s)
        compatible = []
        for addon in resp["addons"]:
            for info in addon["addonVersions"]:
                compat = [c for c in info.get("compatibilities", []) if c.get("clusterVersion") == k8s]
                if not compat:
                    continue
                compatible.append(AddonVersion(
                    version=info["addonVersion"],
                    default=any(c.get("defaultVersion") for c in compat),
                    requires_iam_permissions=info.get("requiresIamPermissions", False),
                    requires_configuration=info.get("requiresConfiguration", False),
                ))
        try:
            installed = self.eks.describe_addon(clusterName=self.cluster, addonName=addon_name)["addon"]
        except self.eks.exceptions.ResourceNotFoundException:
            installed = None
        return judge_addon(addon_name, k8s, compatible, installed)

    def plan_update(self, args: UpdateAddonArgs) -> UpdatePlan:
        a = self.assess(args.addon_name)
        common = {"assessment": a, "resolve_conflicts": args.resolve_conflicts}
        if a.verdict != "upgradable":
            return UpdatePlan(**common, ok=False, allowed=False, reason=VERDICT_LABELS[a.verdict])
        target = args.addon_version or a.recommended_version
        # 모델이 지어낸 버전 / 다운그레이드 / 미호환 버전 차단
        if target not in a.newer_versions:
            return UpdatePlan(
                **common, ok=False, allowed=False, target_version=target,
                reason=f"{target}은 현재 버전보다 새로운 호환 버전이 아닙니다. 가능한 버전: {', '.join(a.newer_versions[:5])}",
            )
        return UpdatePlan(**common, allowed=True, target_version=target, reason="")

    def apply_update(self, plan: UpdatePlan) -> UpdateResult:
        update = self.eks.update_addon(
            clusterName=self.cluster,
            addonName=plan.assessment.addon_name,
            addonVersion=plan.target_version,
            resolveConflicts=plan.resolve_conflicts,
        )["update"]
        return UpdateResult(
            addon_name=plan.assessment.addon_name,
            from_version=plan.assessment.current_version or "",
            to_version=plan.target_version or "",
            update_id=update["id"],
            status=update["status"],
        )

    # --- 도구 핸들러 (async) ---

    async def _list_addons(self, args: NoArgs, approve: Approver) -> AddonList:
        names = await asyncio.to_thread(lambda: self.eks.list_addons(clusterName=self.cluster)["addons"])
        assessments = await asyncio.gather(*(asyncio.to_thread(self.assess, n) for n in names))
        return AddonList(addons=list(assessments))

    async def _assess_addon(self, args: AddonNameArgs, approve: Approver) -> AddonAssessment:
        return await asyncio.to_thread(self.assess, args.addon_name)

    async def _update_addon(self, args: UpdateAddonArgs, approve: Approver) -> ToolResult:
        plan = await asyncio.to_thread(self.plan_update, args)
        if not plan.allowed:
            return plan
        if not await approve(plan.render()):
            return plan.model_copy(update={"ok": False, "rejected_by_user": True})
        return await asyncio.to_thread(self.apply_update, plan)


# ---------------------------------------------------------------------------
# 6. Toolbox: MCP 도구 + 로컬 도구 통합
# ---------------------------------------------------------------------------

Executor = Callable[[BaseModel, Approver], Awaitable[Observation]]


def _summarize_error(text: str) -> str:
    """MCP 에러는 traceback이 길어서 첫 줄과 쿠버네티스 응답 message만 추출"""
    first = text.strip().split("\n")[0]
    m = re.search(r'"message"\s*:\s*"([^"]+)"', text)
    return f"{first} ({m.group(1)})" if m else first


def _mcp_text(result: Any) -> str:
    return "\n".join(c.text for c in getattr(result, "content", []) if hasattr(c, "text"))


def _mcp_json(result: Any) -> Optional[Dict[str, Any]]:
    """MCP 결과는 '요약 한 줄' + 'JSON' 텍스트 블록으로 오므로 첫 JSON 블록을 파싱"""
    for c in getattr(result, "content", []):
        try:
            data = json.loads(getattr(c, "text", ""))
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return None


class PodStatus(BaseModel):
    name: str
    phase: str
    ready: str          # "1/1"
    restarts: int = 0
    node: Optional[str] = None
    reason: Optional[str] = None

    @property
    def healthy(self) -> bool:
        if self.phase == "Succeeded":
            return True
        done, total = self.ready.split("/")
        return self.phase == "Running" and done == total


class PodReport(ToolResult):
    addon_name: Optional[str] = None
    selector: Optional[str] = None
    pods: List[PodStatus] = Field(default_factory=list)
    total_found: int = 0

    def render(self) -> str:
        target = self.addon_name or "kube-system 전체"
        if not self.pods:
            hint = " addon이 설치되어 있지 않거나 노드가 없을 수 있습니다." if self.addon_name else ""
            return f"[{target}] kube-system에서 파드를 찾지 못했습니다 (selector: {self.selector or '없음'}).{hint}"
        healthy = sum(p.healthy for p in self.pods)
        lines = [f"[{target}] 파드 {len(self.pods)}개 중 정상 {healthy}개"]
        if self.total_found > len(self.pods):
            lines[0] += f" (전체 {self.total_found}개 중 {len(self.pods)}개만 확인)"
        for p in self.pods:
            mark = "정상" if p.healthy else "이상"
            line = f"- {p.name}: {p.phase}, ready {p.ready}, 재시작 {p.restarts}회 [{mark}]"
            if p.node:
                line += f" (node: {p.node})"
            lines.append(line)
            if p.reason:
                lines.append(f"    사유: {p.reason}")
        return "\n".join(lines)


def parse_pod_status(name: str, resource: Dict[str, Any]) -> PodStatus:
    status = resource.get("status") or {}
    containers = status.get("containerStatuses") or []
    reason = status.get("reason")
    for c in containers:  # CrashLoopBackOff, ImagePullBackOff 등
        waiting = (c.get("state") or {}).get("waiting")
        if waiting and not reason:
            reason = f"{c.get('name')}: {waiting.get('reason')} {waiting.get('message') or ''}".strip()
    for cond in status.get("conditions") or []:  # Pending 파드의 Unschedulable 등
        if cond.get("status") == "False" and cond.get("message") and not reason:
            reason = f"{cond.get('type')}: {cond.get('message')}"
    return PodStatus(
        name=name,
        phase=status.get("phase", "Unknown"),
        ready=f"{sum(bool(c.get('ready')) for c in containers)}/{len(containers)}",
        restarts=sum(c.get("restartCount", 0) for c in containers),
        node=(resource.get("spec") or {}).get("nodeName"),
        reason=reason,
    )


def render_insights(data: Dict[str, Any]) -> str:
    items = data.get("insights") or []
    if not items:
        return "EKS 인사이트가 없습니다."
    passing = sum((i.get("insight_status") or {}).get("status") == "PASSING" for i in items)
    lines = [f"EKS 인사이트 {len(items)}개 중 PASSING {passing}개 (문제 항목 먼저 표시)"]
    for i in sorted(items, key=lambda i: (i.get("insight_status") or {}).get("status") == "PASSING"):
        st = i.get("insight_status") or {}
        lines.append(f"- [{st.get('status')}] {i.get('name')} ({i.get('category')}, Kubernetes {i.get('kubernetes_version')})")
        if st.get("reason"):
            lines.append(f"    사유: {st['reason']}")
        if i.get("recommendation"):
            lines.append(f"    권장: {i['recommendation']}")
    return "\n".join(lines)


# MCP 도구 결과 중 구조가 확인된 것은 코드가 렌더링 (LLM 요약 생략)
MCP_RENDERERS: Dict[str, Callable[[Dict[str, Any]], str]] = {"get_eks_insights": render_insights}


class PodTools:
    """파드 상태 확인. list_k8s_resources는 메타데이터만 주므로 파드마다 manage_k8s_resource(read)로 status 조회.
    manage_k8s_resource는 LLM에 노출하지 않고 코드에서 read 작업으로만 호출한다."""

    MAX_PODS = 20

    def __init__(self, config: AgentConfig, session: ClientSession):
        self.cluster = config.cluster_name
        self.session = session

    def specs(self) -> List[Tuple[ToolSpec, LocalHandler]]:
        return [(ToolSpec(name="check_pods",
                          description="Check whether the pods of an addon in kube-system are running and ready.",
                          args_model=PodArgs, source="local"), self._check_pods)]

    async def _call(self, tool: str, arguments: Dict[str, Any]) -> Any:
        result = await self.session.call_tool(tool, {"cluster_name": self.cluster, **arguments})
        if getattr(result, "is_error", False):
            raise RuntimeError(_summarize_error(_mcp_text(result)))
        return result

    async def _check_pods(self, args: PodArgs, approve: Approver) -> PodReport:
        selector = POD_SELECTORS.get(args.addon_name) if args.addon_name else None
        query = {"kind": "Pod", "api_version": "v1", "namespace": "kube-system"}
        if selector:
            query["label_selector"] = selector
        names = [item["name"] for item in (_mcp_json(await self._call("list_k8s_resources", query)) or {}).get("items", [])]
        if args.addon_name and not selector:  # selector를 모르는 addon은 파드 이름으로 거름
            names = [n for n in names if _compact(args.addon_name) in _compact(n)]

        pods = []
        for name in names[: self.MAX_PODS]:
            data = _mcp_json(await self._call("manage_k8s_resource", {
                "operation": "read", "kind": "Pod", "api_version": "v1", "name": name, "namespace": "kube-system",
            })) or {}
            pods.append(parse_pod_status(name, data.get("resource") or {}))
        return PodReport(addon_name=args.addon_name, selector=selector, pods=pods, total_found=len(names))


class Toolbox:
    def __init__(self, config: AgentConfig, session: ClientSession, addon_tools: AddonTools):
        self.config = config
        self.session = session
        self.addon_tools = addon_tools
        self.specs: Dict[str, ToolSpec] = {}
        self._executors: Dict[str, Executor] = {}
        self._validation_context: Dict[str, Any] = {}
        self.known_addons: List[str] = []

    async def load(self) -> None:
        allowed = set(self.config.mcp_tools)
        if not self.config.allow_sensitive_data_access:
            allowed -= SENSITIVE_MCP_TOOLS

        for tool in (await self.session.list_tools()).tools:
            if tool.name not in allowed:
                continue
            schema = tool.input_schema or {}
            spec = ToolSpec(
                name=tool.name,
                description=_short_description(tool.description),
                args_model=model_from_json_schema(tool.name, schema, drop=HIDDEN_MCP_ARGS),
                source="mcp",
                inject_cluster="cluster_name" in schema.get("properties", {}),
            )
            self._register(spec, self._mcp_executor(spec))

        for spec, handler in self.addon_tools.specs() + PodTools(self.config, self.session).specs():
            self._register(spec, self._local_executor(handler))

        self.known_addons = await asyncio.to_thread(self.addon_tools.available_addons)
        self._validation_context = {"known_addons": set(self.known_addons)}

    def _register(self, spec: ToolSpec, executor: Executor) -> None:
        self.specs[spec.name] = spec
        self._executors[spec.name] = executor

    def llm_schemas(self) -> List[Dict[str, Any]]:
        return [spec.to_llm_schema() for spec in self.specs.values()]

    def validate(self, call: ToolCall) -> Tuple[ToolSpec, BaseModel]:
        """도구 이름과 인자를 검증. 실패 시 ValueError."""
        spec = self.specs.get(call.name)
        if spec is None:
            raise ValueError(f"Unknown tool '{call.name}'")
        try:
            return spec, spec.args_model.model_validate(call.arguments, context=self._validation_context)
        except ValidationError as e:
            errors = "; ".join(f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors())
            raise ValueError(f"Invalid arguments for '{call.name}': {errors}") from e

    async def execute(self, spec: ToolSpec, args: BaseModel, approve: Approver) -> Observation:
        try:
            return await self._executors[spec.name](args, approve)
        except Exception as e:
            return Observation(ok=False, content=f"{type(e).__name__}: {e}")

    def _mcp_executor(self, spec: ToolSpec) -> Executor:
        async def run(args: BaseModel, approve: Approver) -> Observation:
            arguments = args.model_dump(exclude_none=True)
            if spec.inject_cluster:
                arguments["cluster_name"] = self.config.cluster_name
            result = await self.session.call_tool(spec.name, arguments)
            text = _mcp_text(result)
            if getattr(result, "is_error", False):
                return Observation(ok=False, content=_summarize_error(text))
            renderer, data = MCP_RENDERERS.get(spec.name), _mcp_json(result)
            return Observation(ok=True, content=text, rendered=renderer(data) if renderer and data else None)
        return run

    def _local_executor(self, handler: LocalHandler) -> Executor:
        async def run(args: BaseModel, approve: Approver) -> Observation:
            result = await handler(args, approve)
            return Observation(ok=result.ok, content=result.model_dump_json(), rendered=result.render())
        return run


# ---------------------------------------------------------------------------
# 7. 에이전트
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = (
    "You are an assistant that manages EKS addons (vpc-cni, coredns, kube-proxy, aws-ebs-csi-driver, ...). "
    "The cluster is already selected; never ask for or pass cluster_name. "
    "Call exactly one tool. Questions about pods use check_pods. "
    "Requests to update/upgrade an addon use update_addon. "
    "Questions asking whether an addon is ok, upgradable or installable use assess_addon.\n\n"
    # 1B 모델은 규칙 설명보다 예시를 더 잘 따르므로 의도별 예시를 제공 (사용자 질문과 같은 한국어로)
    "Examples:\n"
    'Q: 설치된 addon 목록 -> {"name": "list_addons", "parameters": {}}\n'
    'Q: vpc-cni 상태 어때? -> {"name": "assess_addon", "parameters": {"addon_name": "vpc-cni"}}\n'
    'Q: coredns 업그레이드 할 수 있어? -> {"name": "assess_addon", "parameters": {"addon_name": "coredns"}}\n'
    'Q: aws-ebs-csi-driver 설치 가능해? -> {"name": "assess_addon", "parameters": {"addon_name": "aws-ebs-csi-driver"}}\n'
    'Q: kube-proxy 업데이트해줘 -> {"name": "update_addon", "parameters": {"addon_name": "kube-proxy"}}\n'
    'Q: aws-node 파드 확인 -> {"name": "check_pods", "parameters": {"addon_name": "vpc-cni"}}\n'
    'Q: 클러스터 업그레이드 준비 상태 -> {"name": "get_eks_insights", "parameters": {}}'
)

ANSWER_PROMPT = (
    "당신은 EKS 운영 어시스턴트입니다. 아래 도구 실행 결과에 있는 사실만 사용해 사용자 질문에 한국어로 간결하게 답하세요. "
    "버전, 이름, 숫자는 결과에 있는 값을 그대로 옮기고, 결과에 없는 정보는 절대 지어내지 마세요."
)

# 1단계 의도 분류: 1B 모델은 JSON 생성보다 "예시 대화를 따라 라벨 한 단어 답하기"를 훨씬 잘함
INTENT_LABELS = ("list", "check", "update", "pods", "insights")
INTENT_PROMPT = (
    "Classify the user's request about EKS addons into one label: "
    + ", ".join(INTENT_LABELS) + ". Answer with the label only."
)
INTENT_SHOTS = (
    ("설치된 addon 목록 보여줘", "list"),
    ("애드온 리스트", "list"),
    ("vpc-cni 상태 어때?", "check"),
    ("coredns 업그레이드 할 수 있어?", "check"),
    ("aws-efs-csi-driver 설치 가능해?", "check"),
    ("kube-proxy 지금 몇 버전이야?", "check"),
    ("kube-proxy 업데이트해줘", "update"),
    ("vpc-cni 최신으로 업그레이드 진행해", "update"),
    ("coredns 버전 올려줘", "update"),
    ("aws-node 파드 확인", "pods"),
    ("kube-proxy 파드 목록 보여줘", "pods"),
    ("클러스터 업그레이드 준비 상태", "insights"),
    ("EKS 인사이트 보여줘", "insights"),
)


def normalize_label(text: str) -> Optional[str]:
    """'List.', 'lists' 같은 출력도 라벨로 인정"""
    m = re.search(r"[a-z]+", text.lower())
    if not m:
        return None
    return next((label for label in INTENT_LABELS if m.group(0).startswith(label)), None)


HELP_MESSAGE = (
    "질문에 맞는 도구를 고르지 못했습니다. 예시처럼 addon 이름을 넣어 다시 질문해 주세요.\n"
    "- 클러스터에 설치된 addon 목록 보여줘\n"
    "- coredns 업그레이드 가능해?\n"
    "- aws-ebs-csi-driver 설치 가능해?\n"
    "- kube-system의 aws-node 파드 상태 확인해줘"
)

# 원본과 대조할 "사실 토큰": 숫자가 들어간 영문/숫자 토큰 (버전, 파드 이름, IP 등)
_FACT_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._\-/:]*\d[A-Za-z0-9._\-/:]*")


def ungrounded_tokens(answer: str, source: str) -> List[str]:
    """답변에 있지만 원본 결과에는 없는 사실 토큰 목록 (한두 자리 숫자는 개수 표현일 수 있어 제외)"""
    tokens = {t.rstrip(".:,") for t in _FACT_TOKEN.findall(answer)}
    return sorted(t for t in tokens if not (t.isdigit() and len(t) <= 2) and t not in source)


def _error_hint(content: str) -> str:
    if "403" in content or "Forbidden" in content:
        return ("\n→ IAM 사용자에게 쿠버네티스 권한이 없습니다. 클러스터 Access Entry에 등록하고 "
                "AmazonEKSViewPolicy 등 액세스 정책을 연결하세요.")
    if "LoginRefreshRequired" in content or "ExpiredToken" in content:
        return "\n→ AWS 세션이 만료되었습니다. 'aws login'을 다시 실행하세요."
    return ""


class EksAddonAgent:
    def __init__(self, config: AgentConfig):
        self.config = config
        self.llm = LocalLLM(config.model_name, config.max_new_tokens)
        self.addon_tools = AddonTools(config)

        env = os.environ.copy()
        env["AWS_REGION"] = config.aws_region
        # Addon 쓰기는 로컬 도구(승인 게이트)로만 하므로 MCP 서버에는 --allow-write를 주지 않음
        args = ["--with", "boto3[crt]", config.mcp_package]
        if config.allow_sensitive_data_access:
            args.append("--allow-sensitive-data-access")
        self.server_params = StdioServerParameters(command=config.mcp_command, args=args, env=env)

    async def _approve(self, summary: str) -> bool:
        if self.config.auto_approve:
            return True
        prompt = f"\n[Approval] 클러스터 '{self.config.cluster_name}'를 변경합니다.\n{summary}\n\n실행할까요? [y/N] "
        answer = await asyncio.to_thread(input, prompt)
        return answer.strip().lower() in ("y", "yes")

    async def run(self, question: str) -> AgentResult:
        print(f"[*] Starting EKS Agent: cluster={self.config.cluster_name}, region={self.config.aws_region}")

        async with stdio_client(self.server_params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                toolbox = Toolbox(self.config, session, self.addon_tools)
                await toolbox.load()
                print(f"[*] Tools: {', '.join(toolbox.specs)}")

                # 1단계: 의도 분류(다수결) → 인자는 코드가 결정. 득표율이 낮으면 도구 호출 다수결로 대체
                route = await self._classify_intent(question)
                if route.label and route.confidence >= self.config.min_intent_confidence:
                    route = self._plan_from_intent(route, question, toolbox)
                else:
                    route = await self._route(question, toolbox)
                if route.tool is None:
                    return AgentResult(question=question, route=route, answer=route.note or HELP_MESSAGE)

                try:
                    spec, args = toolbox.validate(ToolCall(name=route.tool, arguments=route.arguments))
                except ValueError as e:
                    return AgentResult(question=question, route=route, answer=f"실행할 수 없습니다: {e}")
                if route.method == "tool_call" and not spec.read_only and route.confidence < self.config.min_write_confidence:
                    answer = (f"'{spec.name}'는 클러스터를 변경하는 작업인데 도구 선택 득표율이 "
                              f"{route.confidence:.0%}로 낮아 실행하지 않았습니다. 질문을 더 명확히 해 주세요.")
                    return AgentResult(question=question, route=route, answer=answer)

                print(f"[Agent] Executing {spec.name} {args.model_dump_json(exclude_none=True)}")
                observation = await toolbox.execute(spec, args, self._approve)
                print(f"[Observation ok={observation.ok}] {observation.content[:300]}")

        answer = await self._answer(question, route, observation)
        if route.note:
            answer = f"{route.note}\n\n{answer}"
        return AgentResult(question=question, route=route, observation=observation, answer=answer)

    async def _classify_intent(self, question: str) -> RouteDecision:
        """예시 대화(few-shot) + greedy 1회 + 샘플링 N회 → 라벨 다수결"""
        messages: List[Dict[str, Any]] = [{"role": "system", "content": INTENT_PROMPT}]
        for shot_q, shot_label in INTENT_SHOTS:
            messages += [{"role": "user", "content": shot_q}, {"role": "assistant", "content": shot_label}]
        messages.append({"role": "user", "content": question})

        raws = await self.llm.generate(messages, TOOL_CALL_GREEDY, max_new_tokens=4)
        if self.config.route_samples:
            raws += await self.llm.generate(
                messages, TOOL_CALL_SAMPLING, n=self.config.route_samples, max_new_tokens=4
            )
        tally = Counter(label for label in map(normalize_label, raws) if label)

        decision = RouteDecision(method="intent", total=len(raws), tally=dict(tally))
        if tally:
            decision.label, decision.votes = tally.most_common(1)[0]
        print(f"[Intent] {decision.label} 득표 {decision.votes}/{decision.total} {dict(tally)}")
        return decision

    def _plan_from_intent(self, route: RouteDecision, question: str, toolbox: Toolbox) -> RouteDecision:
        """의도 → 도구/인자 결정. 인자는 모델이 아닌 코드가 질문에서 추출"""
        label, note = route.label, None
        if label == "update" and route.confidence < self.config.min_write_confidence:
            label = "check"
            note = (f"업데이트 요청인지 확신이 낮아(득표 {route.votes}/{route.total}) 조회만 수행했습니다. "
                    "업데이트하려면 'coredns 업데이트해줘'처럼 요청해 주세요.")

        addon = find_addon(question, toolbox.known_addons)
        if label == "list":
            tool, arguments = "list_addons", {}
        elif label == "insights":
            tool, arguments = "get_eks_insights", {}
        elif label == "pods":
            tool, arguments = "check_pods", ({"addon_name": addon} if addon else {})
        elif addon is None:
            return route.model_copy(update={
                "label": label,
                "note": "질문에서 addon 이름을 찾지 못했습니다. 'coredns', 'vpc-cni', 'kube-proxy'처럼 addon 이름을 넣어 주세요.",
            })
        elif label == "update":
            tool, arguments = "update_addon", {"addon_name": addon}
            if version := extract_version(question):
                arguments["addon_version"] = version
        else:
            tool, arguments = "assess_addon", {"addon_name": addon}

        print(f"[Plan] {label} → {tool} {arguments}")
        return route.model_copy(update={"label": label, "tool": tool, "arguments": arguments, "note": note})

    async def _route(self, question: str, toolbox: Toolbox) -> RouteDecision:
        """greedy 1회 + 샘플링 N회로 후보를 만들고, 검증을 통과한 후보끼리 다수결"""
        messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": question}]
        tools = toolbox.llm_schemas()
        raws = await self.llm.generate(messages, TOOL_CALL_GREEDY, tools=tools)
        if self.config.route_samples:
            raws += await self.llm.generate(messages, TOOL_CALL_SAMPLING, tools=tools, n=self.config.route_samples)

        tally: Counter = Counter()
        valid: Dict[str, Tuple[str, Dict[str, Any]]] = {}
        for raw in raws:
            call = parse_tool_call(raw)
            if call is None:
                continue
            try:
                spec, args = toolbox.validate(call)
            except ValueError:
                continue  # 검증 실패 후보는 투표권 없음 (지어낸 도구/addon 이름 등)
            normalized = args.model_dump(exclude_none=True)
            key = json.dumps({"name": spec.name, "args": normalized}, sort_keys=True, ensure_ascii=False)
            tally[key] += 1
            valid[key] = (spec.name, normalized)

        # Counter.most_common은 동률일 때 먼저 들어온 후보(greedy)를 우선
        decision = RouteDecision(method="tool_call", total=len(raws), tally=dict(tally))
        if tally:
            key, votes = tally.most_common(1)[0]
            decision.tool, decision.arguments = valid[key]
            decision.votes = votes
        print(f"[Route] {decision.tool} {decision.arguments} 득표 {decision.votes}/{decision.total}")
        for key, votes in tally.most_common():
            print(f"        {votes}표 {key}")
        return decision

    async def _answer(self, question: str, route: RouteDecision, observation: Observation) -> str:
        if observation.rendered:  # Addon 도구: 코드가 만든 답변 그대로 사용 (LLM 미사용)
            return observation.rendered
        if not observation.ok:
            return f"'{route.tool}' 실행 실패: {observation.content}{_error_hint(observation.content)}"

        # MCP 결과: LLM 요약 후보 중 원본에 없는 값이 없는 것만 채택
        source = observation.content[: self.config.observation_chars]
        messages = [
            {"role": "system", "content": ANSWER_PROMPT},
            {"role": "user", "content": f"질문: {question}\n\n도구 실행 결과:\n{source}"},
        ]
        candidates = await self.llm.generate(messages, ANSWER_GENERATION, n=self.config.summary_candidates)
        for candidate in candidates:
            bad = ungrounded_tokens(candidate, observation.content)
            if not bad:
                return candidate
            print(f"[Verify] 요약 후보 폐기 (원본에 없는 값: {', '.join(bad[:5])})")
        return f"모델 요약이 원본 결과와 일치하지 않아 원본을 그대로 표시합니다.\n\n{source}"


# ---------------------------------------------------------------------------
# 8. CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="EKS Addon 관리 에이전트")
    parser.add_argument("question", nargs="?", default="클러스터에 설치된 addon 목록 보여줘")
    parser.add_argument("--cluster", required=True, help="대상 EKS 클러스터 이름")
    parser.add_argument("--region", default="ap-northeast-2")
    parser.add_argument("--model", default="meta-llama/Llama-3.2-1B-Instruct")
    parser.add_argument("--samples", type=int, default=5, help="다수결용 추가 라우팅 샘플 수 (0이면 greedy만)")
    parser.add_argument("--sensitive", action="store_true", help="파드 로그/이벤트 조회 허용")
    parser.add_argument("--yes", action="store_true", help="쓰기 작업 자동 승인 (주의)")
    args = parser.parse_args()

    config = AgentConfig(
        cluster_name=args.cluster,
        aws_region=args.region,
        model_name=args.model,
        route_samples=args.samples,
        allow_sensitive_data_access=args.sensitive,
        auto_approve=args.yes,
    )
    result = asyncio.run(EksAddonAgent(config).run(args.question))

    print("\n=== 답변 ===")
    print(result.answer)


if __name__ == "__main__":
    main()
