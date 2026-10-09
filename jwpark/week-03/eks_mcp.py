"""
EKS Addon 관리 에이전트 (Llama-3.2-1B-Instruct + EKS MCP Server, 프레임워크 없이 Pydantic 기반)

구성:
    AgentConfig   : 에이전트 설정 (frozen)
    LocalLLM      : HF 로컬 모델, Llama 3.2 기본 tool-calling 템플릿 사용
    AddonTools    : MCP 서버에 없는 EKS Addon API(boto3) 도구
    Toolbox       : MCP 도구 + 로컬 도구를 ToolSpec으로 통합, 인자 검증/실행
    EksAddonAgent : LLM → ToolCall 검증 → (쓰기 작업은 승인) → 실행 → 관찰 루프

사용법:
    python eks_mcp.py --cluster my-prod-cluster "vpc-cni addon 상태 확인해줘"
    python eks_mcp.py --cluster my-prod-cluster --sensitive "aws-node 파드 이벤트 확인해줘"

필요 패키지:
    pip install transformers torch pydantic mcp "boto3[crt]"
"""

import argparse
import asyncio
import json
import os
from typing import Any, Awaitable, Callable, Dict, List, Literal, Optional, Tuple, Type

import boto3
import torch
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, ValidationError, create_model, model_validator
from transformers import AutoModelForCausalLM, AutoTokenizer


# ---------------------------------------------------------------------------
# 1. 설정 / 데이터 모델
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
    max_steps: int = Field(default=5, ge=1, le=10)
    observation_chars: int = Field(default=2000, description="LLM에 되돌려줄 도구 결과 최대 길이")

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
    content: str


class AgentStep(BaseModel):
    call: ToolCall
    observation: Observation


class AgentResult(BaseModel):
    question: str
    steps: List[AgentStep] = Field(default_factory=list)
    final_answer: str


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

# 도구 호출은 정확성이 중요하므로 Greedy. repetition_penalty는 JSON 따옴표/괄호를 깨뜨려 사용하지 않음
TOOL_CALL_GENERATION: Dict[str, Any] = {"do_sample": False}
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

    def _generate_sync(self, messages: List[Dict[str, Any]], tools: Optional[List[Dict]]) -> str:
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
            max_new_tokens=self.max_new_tokens,
            pad_token_id=self.tokenizer.eos_token_id,
            **(TOOL_CALL_GENERATION if tools else ANSWER_GENERATION),
        )
        return self.tokenizer.decode(
            outputs[0][inputs["input_ids"].shape[-1]:],
            skip_special_tokens=True,
            clean_up_tokenization_spaces=False,
        ).strip()

    async def generate(self, messages: List[Dict[str, Any]], tools: Optional[List[Dict]] = None) -> str:
        # generate()는 블로킹이므로 이벤트 루프(MCP stdio 통신)를 막지 않도록 스레드에서 실행
        return await asyncio.to_thread(self._generate_sync, messages, tools)


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
# 4. 로컬 도구: EKS Addon API (EKS MCP 서버에는 addon 도구가 없음)
# ---------------------------------------------------------------------------

class NoArgs(BaseModel):
    model_config = ConfigDict(extra="ignore")


class AddonNameArgs(BaseModel):
    addon_name: str = Field(description="Addon name, e.g. vpc-cni, coredns, kube-proxy, aws-ebs-csi-driver")
    model_config = ConfigDict(extra="ignore")


class UpdateAddonArgs(AddonNameArgs):
    addon_version: str = Field(description="Target version, e.g. v1.18.3-eksbuild.1")
    resolve_conflicts: Literal["NONE", "OVERWRITE", "PRESERVE"] = Field(
        default="PRESERVE", description="How to handle config conflicts"
    )


class AddonTools:
    def __init__(self, config: AgentConfig):
        self.cluster = config.cluster_name
        self.eks = boto3.client("eks", region_name=config.aws_region)

    def specs(self) -> List[Tuple[ToolSpec, Callable[[BaseModel], Dict[str, Any]]]]:
        return [
            (ToolSpec(name="list_addons", description="List all EKS addons installed in the cluster.",
                      args_model=NoArgs, source="local"), self.list_addons),
            (ToolSpec(name="describe_addon", description="Get status, version and health issues of one EKS addon.",
                      args_model=AddonNameArgs, source="local"), self.describe_addon),
            (ToolSpec(name="list_addon_versions",
                      description="List addon versions compatible with the cluster's Kubernetes version.",
                      args_model=AddonNameArgs, source="local"), self.list_addon_versions),
            (ToolSpec(name="update_addon", description="Update an EKS addon to a specific version. Modifies the cluster.",
                      args_model=UpdateAddonArgs, read_only=False, source="local"), self.update_addon),
        ]

    def list_addons(self, _: NoArgs) -> Dict[str, Any]:
        return {"addons": self.eks.list_addons(clusterName=self.cluster)["addons"]}

    def describe_addon(self, args: AddonNameArgs) -> Dict[str, Any]:
        addon = self.eks.describe_addon(clusterName=self.cluster, addonName=args.addon_name)["addon"]
        return {
            "name": addon["addonName"],
            "version": addon["addonVersion"],
            "status": addon["status"],
            "health_issues": addon.get("health", {}).get("issues", []),
            "service_account_role_arn": addon.get("serviceAccountRoleArn"),
        }

    def _compatible_versions(self, addon_name: str) -> List[Dict[str, Any]]:
        k8s_version = self.eks.describe_cluster(name=self.cluster)["cluster"]["version"]
        resp = self.eks.describe_addon_versions(addonName=addon_name, kubernetesVersion=k8s_version)
        versions = []
        for addon in resp["addons"]:
            for info in addon["addonVersions"]:
                default = any(c.get("defaultVersion") for c in info.get("compatibilities", []))
                versions.append({"version": info["addonVersion"], "default": default})
        return versions

    def list_addon_versions(self, args: AddonNameArgs) -> Dict[str, Any]:
        # 1B 모델의 문맥 절약을 위해 최신 5개만 반환 (API는 최신순으로 정렬해 돌려줌)
        return {"addon": args.addon_name, "compatible_versions": self._compatible_versions(args.addon_name)[:5]}

    def update_addon(self, args: UpdateAddonArgs) -> Dict[str, Any]:
        # LLM이 지어낸 버전으로 업데이트하지 않도록 호환 버전 목록과 대조
        compatible = {v["version"] for v in self._compatible_versions(args.addon_name)}
        if args.addon_version not in compatible:
            raise ValueError(f"{args.addon_version} is not a compatible version of {args.addon_name}")
        update = self.eks.update_addon(
            clusterName=self.cluster,
            addonName=args.addon_name,
            addonVersion=args.addon_version,
            resolveConflicts=args.resolve_conflicts,
        )["update"]
        return {"update_id": update["id"], "status": update["status"]}


# ---------------------------------------------------------------------------
# 5. Toolbox: MCP 도구 + 로컬 도구 통합
# ---------------------------------------------------------------------------

Executor = Callable[[BaseModel], Awaitable[Observation]]


class Toolbox:
    def __init__(self, config: AgentConfig, session: ClientSession, addon_tools: AddonTools):
        self.config = config
        self.session = session
        self.addon_tools = addon_tools
        self.specs: Dict[str, ToolSpec] = {}
        self._executors: Dict[str, Executor] = {}

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

        for spec, fn in self.addon_tools.specs():
            self._register(spec, self._local_executor(fn))

    def _register(self, spec: ToolSpec, executor: Executor) -> None:
        self.specs[spec.name] = spec
        self._executors[spec.name] = executor

    def llm_schemas(self) -> List[Dict[str, Any]]:
        return [spec.to_llm_schema() for spec in self.specs.values()]

    def validate(self, call: ToolCall) -> Tuple[ToolSpec, BaseModel]:
        """도구 이름과 인자를 검증. 실패 시 ValueError (메시지는 LLM에 그대로 피드백)."""
        spec = self.specs.get(call.name)
        if spec is None:
            raise ValueError(f"Unknown tool '{call.name}'. Available tools: {', '.join(self.specs)}")
        try:
            return spec, spec.args_model.model_validate(call.arguments)
        except ValidationError as e:
            errors = "; ".join(f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors())
            raise ValueError(f"Invalid arguments for '{call.name}': {errors}") from e

    async def execute(self, spec: ToolSpec, args: BaseModel) -> Observation:
        try:
            return await self._executors[spec.name](args)
        except Exception as e:  # 도구 실패도 관찰 결과로 LLM에 전달
            return Observation(ok=False, content=f"{type(e).__name__}: {e}")

    def _mcp_executor(self, spec: ToolSpec) -> Executor:
        async def run(args: BaseModel) -> Observation:
            arguments = args.model_dump(exclude_none=True)
            if spec.inject_cluster:
                arguments["cluster_name"] = self.config.cluster_name
            result = await self.session.call_tool(spec.name, arguments)
            text = "\n".join(c.text for c in getattr(result, "content", []) if hasattr(c, "text"))
            return Observation(ok=not getattr(result, "is_error", False), content=text)
        return run

    def _local_executor(self, fn: Callable[[BaseModel], Dict[str, Any]]) -> Executor:
        async def run(args: BaseModel) -> Observation:
            data = await asyncio.to_thread(fn, args)  # boto3 호출은 블로킹
            return Observation(ok=True, content=json.dumps(data, ensure_ascii=False, default=str))
        return run


# ---------------------------------------------------------------------------
# 6. 에이전트
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = (
    "You are an assistant that manages EKS addons (vpc-cni, coredns, kube-proxy, aws-ebs-csi-driver, ...). "
    "The cluster is already selected; never ask for or pass cluster_name. "
    "Call exactly one tool at a time. "
    "When you have enough information, answer in plain text without JSON.\n\n"
    # 1B 모델은 규칙 설명보다 예시를 더 잘 따르므로 의도별 예시를 제공
    "Examples:\n"
    'Q: vpc-cni addon status -> {"name": "describe_addon", "parameters": {"addon_name": "vpc-cni"}}\n'
    'Q: available/upgradable versions of coredns -> {"name": "list_addon_versions", "parameters": {"addon_name": "coredns"}}\n'
    'Q: aws-node pods in kube-system -> {"name": "list_k8s_resources", "parameters": '
    '{"kind": "Pod", "api_version": "v1", "namespace": "kube-system", "label_selector": "k8s-app=aws-node"}}\n'
    'Q: installed addons -> {"name": "list_addons", "parameters": {}}'
)

ANSWER_PROMPT = (
    "당신은 EKS 운영 어시스턴트입니다. 아래 도구 실행 결과에 있는 사실만 사용해 사용자 질문에 한국어로 간결하게 답하세요. "
    "결과에 없는 정보는 지어내지 마세요."
)


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

    async def _approve(self, spec: ToolSpec, args: BaseModel) -> bool:
        if spec.read_only or self.config.auto_approve:
            return True
        prompt = (
            f"\n[Approval] '{spec.name}' 작업이 클러스터 '{self.config.cluster_name}'를 변경합니다.\n"
            f"  args: {args.model_dump_json()}\n  실행할까요? [y/N] "
        )
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

                steps = await self._loop(question, toolbox)

        final_answer = await self._answer(question, steps)
        return AgentResult(question=question, steps=steps, final_answer=final_answer)

    async def _loop(self, question: str, toolbox: Toolbox) -> List[AgentStep]:
        """Plan(LLM) → Validate → Approve → Act(Tool) → Observe 반복"""
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": question},
        ]
        tools = toolbox.llm_schemas()
        steps: List[AgentStep] = []
        seen: set = set()

        for step_no in range(1, self.config.max_steps + 1):
            raw = await self.llm.generate(messages, tools=tools)
            print(f"\n[Step {step_no}] LLM: {raw}")

            call = parse_tool_call(raw)
            if call is None:
                break  # 도구 호출이 아니면 정보 수집 종료

            # 1B 모델은 같은 호출을 반복하는 경향이 있어 중복 호출 시 종료
            key = json.dumps(call.model_dump(), sort_keys=True, ensure_ascii=False)
            if key in seen:
                print("[Agent] 동일한 도구 호출 반복 → 수집 종료")
                break
            seen.add(key)

            try:
                spec, args = toolbox.validate(call)
                if await self._approve(spec, args):
                    print(f"[Agent] Executing {spec.name} {args.model_dump_json(exclude_none=True)}")
                    observation = await toolbox.execute(spec, args)
                else:
                    observation = Observation(ok=False, content="User rejected this operation. Do not retry it.")
            except ValueError as e:
                observation = Observation(ok=False, content=f"{e}. Fix the call and try again.")

            observation.content = observation.content[: self.config.observation_chars]
            print(f"[Observation ok={observation.ok}] {observation.content[:500]}")
            steps.append(AgentStep(call=call, observation=observation))

            messages.append({
                "role": "assistant",
                "tool_calls": [{"type": "function", "function": {"name": call.name, "arguments": call.arguments}}],
            })
            messages.append({"role": "tool", "content": observation.content})

        return steps

    async def _answer(self, question: str, steps: List[AgentStep]) -> str:
        """도구 없이 별도 호출로 최종 답변 생성 (1B 모델이 JSON 모드에서 벗어나도록)"""
        if not steps:
            return "도구를 호출하지 못해 답변할 수 없습니다. 질문을 더 구체적으로 작성해 주세요."

        succeeded = [s for s in steps if s.observation.ok]
        # 실패 내역은 1B 모델이 요약하면 환각이 생기므로 코드에서 그대로 보고
        failures = "\n".join(
            f"- {s.call.name} 실패: {s.observation.content[:300]}" for s in steps if not s.observation.ok
        )
        if not succeeded:
            return f"모든 도구 호출이 실패해 답변할 수 없습니다.\n{failures}"

        results = "\n\n".join(
            f"[{s.call.name} {json.dumps(s.call.arguments, ensure_ascii=False)}]\n{s.observation.content}"
            for s in succeeded
        )
        messages = [
            {"role": "system", "content": ANSWER_PROMPT},
            {"role": "user", "content": f"질문: {question}\n\n도구 실행 결과:\n{results}"},
        ]
        answer = await self.llm.generate(messages)
        return f"{answer}\n\n[실패한 도구]\n{failures}" if failures else answer


# ---------------------------------------------------------------------------
# 7. CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="EKS Addon 관리 에이전트")
    parser.add_argument("question", nargs="?", default="vpc-cni addon의 상태와 업그레이드 가능한 버전을 확인해줘.")
    parser.add_argument("--cluster", required=True, help="대상 EKS 클러스터 이름")
    parser.add_argument("--region", default="ap-northeast-2")
    parser.add_argument("--model", default="meta-llama/Llama-3.2-1B-Instruct")
    parser.add_argument("--max_steps", type=int, default=5)
    parser.add_argument("--sensitive", action="store_true", help="파드 로그/이벤트 조회 허용")
    parser.add_argument("--yes", action="store_true", help="쓰기 작업 자동 승인 (주의)")
    args = parser.parse_args()

    config = AgentConfig(
        cluster_name=args.cluster,
        aws_region=args.region,
        model_name=args.model,
        max_steps=args.max_steps,
        allow_sensitive_data_access=args.sensitive,
        auto_approve=args.yes,
    )
    result = asyncio.run(EksAddonAgent(config).run(args.question))

    print("\n=== 최종 답변 ===")
    print(result.final_answer)
    print(f"\n(도구 호출 수: {len(result.steps)})")


if __name__ == "__main__":
    main()
