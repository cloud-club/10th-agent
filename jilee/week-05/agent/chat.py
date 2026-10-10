"""전역 에이전트 채팅: 화면 어디서든 고객·프로젝트·회의에 대해 묻고 답을 받는다.

"이 고객 예산 어떻게 됐지?", "최민호가 맡은 일은?", "다음 회의에서 뭘 물어야 해?" 같은 질문에,
지금 보고 있는 고객·프로젝트를 문맥으로 삼아 읽기 전용 도구를 골라 쓰고 근거와 함께 답한다.

쓰기는 하지 않는다. 결과지와 회의록은 세 개의 문(회의 → 회의록 → 결과지)을 거쳐야 바뀌므로,
채팅에는 update_result_sheet · save_minutes 같은 쓰기 도구를 아예 주지 않는다. 바꿔 달라는 요청에는
어디서 어떻게 하면 되는지를 안내한다.

외부 LLM으로 나가는 글은 회의 처리와 똑같이 MaskedLLM을 거친다. 채팅은 고객을 가로질러 물을 수 있어
이름 사전을 등록된 모든 고객·프로젝트에서 만들고, 고객사마다 다른 자리표시자([고객사A] · [고객사B] …)를 준다.
"""

from __future__ import annotations

import json
import os
from typing import Callable

from . import memory, store, tools
from .llm import LLMClient
from .masking import MaskedLLM, Masker, build_dictionary
from .sop_agent import consult_sop

MAX_STEPS = 5           # 도구를 고르는 횟수의 상한. 넘기면 그때까지 읽은 것으로 답하게 한다
HISTORY = 6             # 화면이 보낸 대화 가운데 뒤에서 이만큼만 쓴다(분당 입력 토큰 한도)
MAX_TOOL_CHARS = 4000   # 도구 결과 하나를 모델에 넘길 때의 상한
EMPTY_ANSWER = "(답을 만들지 못했습니다. 질문을 조금 더 구체적으로 적어 주세요.)"


# ---- 읽기 전용 도구 -----------------------------------------------------------

def list_projects() -> str:
    """등록된 고객 → 프로젝트 → 회의를 한눈에. 회의마다 세 개의 문 가운데 어디에 서 있는지 붙인다."""
    lines = []
    for c in store.list_tree():
        lines.append(f"## {c['name']} ({c['customer_id']})")
        for p in c["projects"]:
            lines.append(f"- 프로젝트 {p['project_id']} {p['name']} · 단계 {p['stage'] or '미정'} · 회의 {len(p['meetings'])}건")
            for n in p["meetings"]:
                m = tools.meeting_view(c["customer_id"], p["project_id"], n)
                todo = f" — 할 일: {m['gate']['todo']}" if m["gate"]["todo"] else ""
                lines.append(f"  - {n}차 {m['date'] or '일시 미정'} {m['type']} · {m['gate']['state']}{todo}".rstrip())
        if not c["projects"]:
            lines.append("- 프로젝트 없음")
    return "\n".join(lines) or "등록된 고객이 없습니다."


def read_project_context(customer_id: str, project_id: str) -> str:
    """고객·프로젝트·담당자와 확정된 최신 프로젝트 기억(합의 · 액션아이템 · 결과지 현황). 연락처는 들어 있지 않다."""
    return tools.read_project_context(customer_id, project_id)


def read_minutes(customer_id: str, project_id: str, meeting_no: int) -> str:
    """한 회차의 회의록 전문. 담당자가 확정하기 전이면 초안임을 머리에 적어 준다."""
    p = store.minutes_path(customer_id, project_id, meeting_no)
    if not p.exists():
        raise FileNotFoundError(f"{int(meeting_no)}차 회의록이 없습니다. 에이전트가 아직 이 회의를 처리하지 않았습니다.")
    state = tools.gate_of(customer_id, project_id, meeting_no)["state"]
    head = "(회의록 초안 — 담당자가 아직 확정하지 않았다. 프로젝트 기억과 결과지에는 반영되지 않은 내용이다)\n\n" if state == "회의록 검토" else ""
    return head + p.read_text(encoding="utf-8")


def _fn(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {"name": name, "description": description,
                                             "parameters": {"type": "object", "properties": properties, "required": required}}}


_IDS = {"customer_id": {"type": "string", "description": "고객코드 (예: KR0001)"},
        "project_id": {"type": "string", "description": "프로젝트코드 (예: P001)"}}

CHAT_TOOLS = [
    _fn("list_projects", "등록된 고객 · 프로젝트 · 회의 목록과 회의마다의 진행 상태(회의 → 회의록 → 결과지), 남은 할 일을 본다. 어느 고객·프로젝트인지 모를 때 먼저 부른다.",
        {}, []),
    _fn("read_project_context", "한 프로젝트의 고객정보 · 담당자 · 확정된 최신 기억(합의, 미합의, 액션아이템, 결과지 항목별 값과 상태, 다음 회의 질문)을 읽는다.",
        dict(_IDS), ["customer_id", "project_id"]),
    _fn("search_project_history", "지난 회의의 녹취 발화 · 회의록 항목에서 원문을 찾는다. 정확한 발언 · 숫자 · 날짜나 누가 말했는지가 필요할 때 부른다.",
        {**_IDS, "query": {"type": "string", "description": "찾을 내용. 고객이 쓴 말 · 금액 · 날짜 · 인물 이름"},
         "meeting_from": {"type": "integer"}, "meeting_to": {"type": "integer"},
         "kind": {"type": "string", "enum": ["발화", "회의록"]}},
        ["customer_id", "project_id", "query"]),
    _fn("read_minutes", "한 회차의 회의록 전문을 읽는다.",
        {**_IDS, "meeting_no": {"type": "integer", "description": "회차 번호"}}, ["customer_id", "project_id", "meeting_no"]),
    _fn("consult_sop", "영업 SOP 담당자에게 묻는다. 무엇을 확인 · 기록해야 하는지 같은 규정 질문에 쓴다.",
        {"question": {"type": "string", "description": "SOP 용어를 넣은 한 문장 질문"}}, ["question"]),
]
NEEDS_IDS = {"read_project_context", "search_project_history", "read_minutes"}


def _source(name: str, args: dict) -> str:
    """답의 출처로 화면에 보여줄 한 줄."""
    pid = args.get("project_id", "")
    return {"list_projects": "고객 · 프로젝트 목록",
            "read_project_context": f"{pid} 프로젝트 기억",
            "search_project_history": f"이력 검색: {str(args.get('query', ''))[:30]}",
            "read_minutes": f"{pid} {args.get('meeting_no', '')}차 회의록",
            "consult_sop": f"SOP: {str(args.get('question', ''))[:30]}"}[name]


# ---- 마스킹 -------------------------------------------------------------------

def build_masker() -> Masker:
    """등록된 모든 고객에서 이름 사전을 만든다. 채팅은 목록 조회로 다른 고객의 이름도 읽을 수 있기 때문이다.

    Masker는 고객사 하나를 전제로 [고객사A] 하나만 쓰므로, 고객사마다 다른 자리표시자를 주도록 사전을 직접 채운다.
    """
    names = {m["name"] for m in store.load_org().get("members", []) if m.get("name")}
    companies: list[list[str]] = []
    for c in store.list_tree():
        cid, texts, extra = c["customer_id"], [], [x["name"] for x in store.load_customer(c["customer_id"])["contacts"] if x.get("name")]
        for p in c["projects"]:
            d = store.project_dir(cid, p["project_id"])
            texts += [f.read_text(encoding="utf-8") for f in sorted(d.glob("transcript_*.txt"))]
            extra += [a["name"] for a in memory.latest(store.key(cid, p["project_id"]), d)["attendees"]]
            extra += [t["name"] for t in store.team(cid, p["project_id"]) if t.get("name")]
        found, people = build_dictionary(c["name"], texts, extra)
        companies.append(found)
        names |= set(people)
    words = {w for group in companies for w in group}
    masker = Masker([], sorted(n for n in names if n not in words))
    for i, group in enumerate(companies):
        for name in group:
            masker.forward[name] = f"[고객사{chr(ord('A') + i % 26)}{i // 26 or ''}]"
    masker._order = sorted(masker.forward, key=len, reverse=True)  # 긴 이름부터 바꿔야 '한빛정밀(주)'가 먼저 잡힌다
    masker.backward = {}
    for k in masker._order:
        masker.backward.setdefault(masker.forward[k], k)
    return masker


# ---- 대화 ---------------------------------------------------------------------

def build_system_prompt(customer_id: str | None, project_id: str | None, meeting_no: int | None) -> str:
    where = "지정되지 않았다. 어느 고객·프로젝트인지 모르면 list_projects로 찾는다."
    if customer_id:
        try:
            where = f"고객 {store.load_customer(customer_id)['name']}({customer_id})"
            if project_id:
                where += f" · 프로젝트 {store.load_project(customer_id, project_id)['name']}({project_id})"
            if meeting_no:
                where += f" · {int(meeting_no)}차 회의"
        except (FileNotFoundError, ValueError):
            where = f"고객코드 {customer_id} · 프로젝트코드 {project_id or '없음'} (등록된 것과 맞지 않는다. list_projects로 확인한다)"
    return f"""당신은 영업 조직의 회의 기록을 읽어 질문에 답하는 도우미다. 지금 사용자가 보고 있는 화면: {where}
질문이 "이 고객", "이 프로젝트"처럼 가리키면 위 화면의 것을 뜻한다.

- 읽기 전용이다. 도구로 기록을 읽고 답한다. 결과지·회의록·거래 조건을 바꿀 수는 없다. 바꿔 달라는 요청에는 방법을 안내한다:
  회의 내용은 "회의 상세"에서 녹취를 넣고 에이전트를 실행 → 회의록을 읽고 "회의록 확정" → 결과지에서 값마다 "확인". 거래 조건은 결과지 화면에서 사람이 고른다.
- 기록에 있는 것만 말한다. 기록에 없으면 모른다고 하고 추정하지 않는다. 필요하면 다음 회의에서 물을 질문으로 제안한다.
- 근거를 밝힌다. 회의에서 나온 사실은 "N차 · 화자"를 붙인다.
- 결과지의 값은 상태를 구분해 말한다. "초안"은 AI가 채우고 사람이 아직 확인하지 않은 것, "확인"은 사람이 확정한 것이다.
- [고객사A]·[인물1] 같은 자리표시자는 그대로 옮겨 적는다.
- 한국어로 짧게 답한다. 묻는 것부터 답하고, 목록이 필요하면 다섯 줄 안팎으로 적는다."""


def _brief(value):
    if isinstance(value, str) and len(value) > 60:
        return value[:60] + "…"
    return value


def answer(messages: list[dict], customer_id: str | None = None, project_id: str | None = None, meeting_no: int | None = None,
           llm=None, on_step: Callable[[dict], None] | None = None) -> dict:
    """대화 이력의 마지막 질문에 답한다. 돌려주는 값: {"answer": 답, "trace": 도구 호출 과정, "sources": 답에 쓴 기록}."""
    history = [{"role": m["role"], "content": str(m.get("content") or "")} for m in messages
               if m.get("role") in ("user", "assistant") and str(m.get("content") or "").strip()][-HISTORY:]
    if not history or history[-1]["role"] != "user":
        raise ValueError("질문이 비어 있습니다.")
    llm = llm or LLMClient()
    if os.environ.get("MASKING", "on").lower() != "off":
        llm = MaskedLLM(llm, build_masker())
    functions = {"list_projects": list_projects, "read_project_context": read_project_context, "read_minutes": read_minutes,
                 "search_project_history": tools.search_project_history,
                 "consult_sop": lambda question: consult_sop(question, llm)}  # SOP 문의도 같은(마스킹된) 클라이언트로
    convo = [{"role": "system", "content": build_system_prompt(customer_id, project_id, meeting_no)}] + history
    trace: list[dict] = []
    sources: list[str] = []

    def done(msg: dict) -> dict:
        return {"answer": (msg.get("content") or "").strip() or EMPTY_ANSWER, "trace": trace, "sources": sources}

    for step in range(1, MAX_STEPS + 1):
        msg = llm.chat(convo, tools=CHAT_TOOLS)
        calls = msg.get("tool_calls") or []
        if not calls:
            return done(msg)
        assistant = {"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls}
        if msg.get("reasoning_details"):  # 추론형 모델(OpenRouter 규격)은 추론 기록을 되돌려줘야 다음 단계를 잇는다
            assistant["reasoning_details"] = msg["reasoning_details"]
        convo.append(assistant)
        for call in calls:
            name = call["function"]["name"]
            try:
                args = json.loads(call["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            if name in NEEDS_IDS:  # 모델이 코드를 빼먹으면 화면에서 보고 있는 고객·프로젝트로 채운다
                for key, value in (("customer_id", customer_id), ("project_id", project_id)):
                    if value and not args.get(key):
                        args[key] = value
            try:
                if name not in functions:
                    raise ValueError(f"'{name}'은(는) 채팅에서 쓸 수 없는 도구다. 채팅은 읽기만 한다.")
                output, ok = functions[name](**args), True
            except Exception as e:  # 도구 실패도 모델에게 돌려줘 다른 길을 찾게 한다
                output, ok = f"오류: {e}", False
            if len(output) > MAX_TOOL_CHARS:
                output = output[:MAX_TOOL_CHARS] + f"\n…(길어서 {MAX_TOOL_CHARS:,}자에서 잘랐다. 더 필요하면 search_project_history로 좁혀 찾는다)"
            if ok and _source(name, args) not in sources:
                sources.append(_source(name, args))
            entry = {"step": step, "type": "tool", "name": name, "ok": ok,
                     "args": {k: _brief(v) for k, v in args.items()}, "output_preview": output[:300]}
            trace.append(entry)
            if on_step:
                on_step(entry)
            convo.append({"role": "tool", "tool_call_id": call["id"], "name": name, "content": output})

    # 상한에 닿았다. 더 읽지 않고 지금까지 읽은 것으로 답하게 한다
    convo.append({"role": "user", "content": "도구는 더 쓰지 않는다. 지금까지 읽은 내용만으로 답하라. 확인하지 못한 것은 모른다고 적는다."})
    return done(llm.chat(convo, tools=None))
