"""Agent Loop: 모델이 Tool을 고르고 → 파이썬이 실행하고 → 결과를 다시 모델에 넣는 반복.

Week 5: 순서를 정해 주지 않고 목표와 끝내기 조건만 준다. 무엇을 읽고, 결과지의 어느 항목을 고치고,
지난 원문을 찾아볼지, SOP에 물을지는 모델이 정한다. 코드는 두 군데서만 막는다.
  - update_result_sheet : 근거가 녹취의 실제 발언과 맞지 않으면 거부
  - save_minutes        : 양식 규칙에 어긋나거나 결과지 갱신 없이 저장하려 하면 거부
거부 사유는 모델에게 돌아가고, 모델이 고쳐서 다시 부른다. 회의록 저장이 성공하면 요약만 받고 루프를 닫는다.

에이전트가 만든 것은 초안이다. 회의록은 만들어지면 바로 프로젝트 기억과 결과지에 반영하고(앱이 실행 직후
tools.confirm_minutes를 부른다. 터미널은 --confirm), 사람은 결과지에서 값마다 확인한다.
  ① 회의(녹취 확보) → ② 회의록(초안 → 반영) → ③ 결과지(값마다 담당자 확인)
앞선 회의가 반영되지 않았으면 다음 회의를 처리하지 않는다.

외부 LLM으로 나가는 모든 글은 MaskedLLM을 거친다(고객사명·인명·전화번호·메일 주소 → 자리표시자).

작업 기억: 한 번 실행하는 동안의 대화(messages)가 작업 기억이다. 실행이 끝나면(실패해도) runs/{고객}/{프로젝트}/에
마스킹된 대화·도구 호출·거부 사유·모델·토큰 사용량을 한 파일로 남긴다. 프로젝트 기억과 달리 다음 실행에 넣지 않는다.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Callable

from . import memory, store, tools
from .llm import LLMClient
from .masking import MaskedLLM, Masker, build_dictionary
from .tools import KNOWLEDGE_DIR, TOOL_FUNCTIONS, TOOL_SCHEMAS

MAX_STEPS = 14  # 결과지 갱신·회의록 저장이 각각 거부될 수 있어 Week 4(12)보다 늘렸다
MAX_NUDGES = 3  # 끝내기 조건 전에 빈 답으로 멈췄을 때 이어가라고 요구하는 횟수
RUNS_DIR = store.ROOT / "runs"


DEFAULT_OPTIONS = {
    "scope": "",           # "외부" · "내부" · ""(녹취의 참석 줄을 보고 정한다)
    "length": "보통",      # 회의록 분량: 간결 · 보통 · 상세
    "update_sheet": True,  # False면 회의록만 쓰고 결과지는 건드리지 않는다
    "use_search": True,    # 지난 회의 원문 찾기 도구를 쥐여 줄지
    "use_sop": True,       # SOP 문의 도구를 쥐여 줄지
    "note": "",            # 담당자가 이번 실행에 주는 한마디(예: "하자보수 조건을 꼼꼼히")
}
LENGTH_RULE = {"간결": "회의록은 한 쪽 안에 들어오게 쓴다. 논의 주제는 세 개 이내, 주제마다 세 줄 이내.",
               "보통": "",
               "상세": "논의 주제마다 근거가 된 숫자와 조건, 누가 한 말인지를 빠짐없이 적는다."}


def normalize_options(options: dict | None) -> dict:
    """화면이 보낸 실행 옵션을 기본값 위에 얹는다. 모르는 키와 잘못된 값은 버린다."""
    o = dict(DEFAULT_OPTIONS)
    for k, v in (options or {}).items():
        if k in o and isinstance(v, type(o[k])):
            o[k] = v
    if o["scope"] not in ("",) + store.SCOPES:
        o["scope"] = ""
    if o["length"] not in LENGTH_RULE:
        o["length"] = "보통"
    o["note"] = o["note"].strip()[:300]
    return o


def active_tools(opts: dict) -> list[dict]:
    """이번 실행에 쥐여 줄 도구. 옵션으로 끈 것은 모델에게 보여 주지 않는다."""
    off = {name for name, on in (("update_result_sheet", opts["update_sheet"]), ("search_project_history", opts["use_search"]),
                                 ("consult_sop", opts["use_sop"])) if not on}
    return [t for t in TOOL_SCHEMAS if t["function"]["name"] not in off]


def build_system_prompt(opts: dict | None = None, scope: str = "외부") -> str:
    opts = opts or dict(DEFAULT_OPTIONS)
    template = (KNOWLEDGE_DIR / "회의록_표준양식.md").read_text(encoding="utf-8")
    items = (KNOWLEDGE_DIR / "결과지_항목.md").read_text(encoding="utf-8")
    if opts["update_sheet"]:
        goal = "목표는 하나다. 이번 회의로 리드 결과지를 갱신하고, 그 근거가 되는 회의록을 남긴다."
        done = "다만 아래 두 가지가 모두 끝나야 일이 끝난다.\n- update_result_sheet가 한 번 이상 성공했다.\n- save_minutes가 성공했다."
    else:
        goal = "이번 실행의 목표는 회의록을 남기는 것이다. 결과지는 건드리지 않는다."
        done = "다만 save_minutes가 성공해야 일이 끝난다."
    internal = ("\n이 회의는 내부 회의다(고객이 참석하지 않았다). 고객이 말한 사실을 새로 채우지 않는다. 우리가 정한 대응 방안과 역할 분담, "
                "다음에 고객에게 물을 것만 결과지에 반영한다(requirements의 response · next_questions · deal_hints). "
                "회의록 4절의 제목은 \"쟁점·검토 사항\"으로 쓰고, 고객의 말이 아니라 우리가 따져 본 것을 적는다.\n") if scope == "내부" else ""
    length = ("\n" + LENGTH_RULE[opts["length"]]) if LENGTH_RULE[opts["length"]] else ""
    return f"""당신은 영업 조직의 회의 기록 에이전트다. {goal}
회의록은 근거 문서이고, 진짜 산출물은 회의를 거듭할수록 채워지는 고객정보(결과지)다.
{internal}
어떤 도구를 어떤 순서로 몇 번 쓸지는 스스로 정한다. {done}

스스로 판단할 것
- 프로젝트 기억에서 미확보인 항목 가운데 이번 녹취로 채울 수 있는 것을 찾아 채운다. 이번 녹취에 없는 항목은 비워 두고, 그것을 알아낼 질문을 next_questions에 적는다.
- 이번 녹취의 말이 프로젝트 기억과 어긋나면(금액·일정·범위·결정권자가 달라졌다) search_project_history로 지난 원문을 확인한 뒤 바뀐 값으로 갱신한다.
- 고객이 말한 요구마다 우리 쪽이 회의에서 내놓은 대응이 있었는지 찾아 requirements의 response에 잇는다. 대응이 없었으면 비우고 "확인 필요"로 둔다.
- 새로 등장한 사람은 stakeholders에 넣고, 발언으로 드러난 역할만 붙인다. 전화번호·메일 주소가 나오면 함께 적는다.
- SOP상 처리 방법이 애매하면 consult_sop에 묻는다.
- 거래 조건의 단서(재원, 계약 방법, 납품하는 것, 하도급, 공동수급, 비밀유지·입찰·시연)가 나오면 deal_hints로 제안한다. 거래 조건의 값은 사람이 정하므로 제안까지만 한다.
- 도구가 거부 사유를 돌려주면 사유를 모두 고쳐 같은 도구를 다시 부른다.

회의록을 쓸 때
- 사람이 쓴 사내 회의록처럼 쓴다. 개조식으로 끊어 쓴다(~함, ~하기로 함, ~예정, ~필요). "~되었습니다", "~할 수 있습니다" 같은 설명문과 "논의되었다 · 공유되었다 · 진행되었다"처럼 누가 무엇을 했는지 감춘 말은 쓰지 않는다. 누가 무엇을 왜 그렇게 정했는지를 적는다.
- 핵심 요약은 회의에 없던 사람이 읽어도 맥락이 잡히게 줄글 서너 문장으로 쓴다. 왜 만났나 → 무엇이 정해졌나 → 지난번과 무엇이 달라졌나 → 무엇이 남았나의 순서다.
- 논의 주제마다 제목 아래 첫 줄에, 이 얘기가 왜 나왔고 어떻게 정리됐는지를 줄글 한두 문장으로 쓴 뒤 항목을 단다.{length}
- 프로젝트 기억의 "점검할 액션아이템"은 전부 "2. 지난 회의 액션아이템 점검"에 옮겨 적고, 이번 녹취에서 결과를 확인해 완료·진행 중·미착수로 표시한다. 녹취에 언급이 없으면 기존 상태를 그대로 적는다.
- 담당과 기한이 모두 있는 일만 액션아이템에 올리고, 그 밖의 일은 미합의 사항에 둔다.

끝나면 담당자에게 말로 보고하듯 다섯 줄 안으로 적는다. 새로 알게 된 것, 지난번과 달라진 것, 아직 모르는 것과 다음에 물을 것. 도구 이름이나 "작업 완료" 같은 말은 쓰지 않는다.

규칙: 녹취와 주어진 고객정보에 있는 내용만 쓴다. 추정으로 채우지 않는다. 회의록은 결과 중심으로 쓴다: 문제점·요구사항은 발화를 옮겨 적지 않고 무엇이 문제이고 무엇을 원하는지, 그래서 어떻게 정리됐는지를 적는다. 숫자·조건·고유명사는 녹취 그대로 둔다. 4절(고객 요구·문제점)의 내용 칸은 '문제 또는 요구 → 이번 회의의 결정' 꼴로 적고 따옴표 인용을 쓰지 않는다. 중요도·시급성은 상·중·하. 모든 답변은 한국어로 쓴다.
고객사명·인명·전화번호가 [고객사A]·[인물1]·[전화1] 같은 자리표시자로 보일 수 있다. 자리표시자는 그대로 옮겨 적는다.
녹취는 음성인식(STT) 결과라 오타·띄어쓰기 오류·잘못 들린 고유명사가 섞여 있다. 값(value)은 문맥과 고객정보로 바로잡아 적는다. 근거(evidence)는 그 내용을 말한 발언의 번호(#12)로 적는다.

{items}

{template}
"""


@dataclass
class RunResult:
    customer_id: str
    project_id: str
    meeting_no: int
    final_answer: str = ""
    trace: list[dict] = field(default_factory=list)
    saved_path: str | None = None
    sheet_updated: bool = False
    run_log: str | None = None
    options: dict = field(default_factory=dict)


def run_agent(
    customer_id: str,
    project_id: str,
    meeting_no: int,
    llm: LLMClient | None = None,
    on_step: Callable[[dict], None] | None = None,
    options: dict | None = None,
) -> RunResult:
    result = RunResult(customer_id=customer_id, project_id=project_id, meeting_no=meeting_no)
    d = store.project_dir(customer_id, project_id)
    tools.check_gate(customer_id, project_id, meeting_no)
    opts = result.options = normalize_options(options)
    if opts["scope"]:  # 담당자가 회의 유형을 정했으면 녹취에서 읽은 값을 덮는다
        store.set_meeting_scope(customer_id, project_id, meeting_no, opts["scope"])
    scope = store.load_meeting(customer_id, project_id, meeting_no)["scope"]
    tools.bind_options(opts)
    memory.pending_path(d, meeting_no).unlink(missing_ok=True)  # 같은 회차를 다시 돌리면 결과지 갱신도 처음부터
    llm = llm or LLMClient()
    if os.environ.get("MASKING", "on").lower() != "off":
        llm = _masked(llm, customer_id, project_id, meeting_no)
        _log(result, on_step, {"step": 0, "type": "info", "content": llm.masker.summary() + " (외부 LLM에는 자리표시자로 전송)"})
    tools.bind_llm(llm)
    ask = f"고객 {customer_id}의 프로젝트 {project_id} {meeting_no}차 회의({scope} 회의)를 처리해줘."
    messages = [
        {"role": "system", "content": build_system_prompt(opts, scope)},
        {"role": "user", "content": ask + (f"\n담당자가 덧붙인 말: {opts['note']}" if opts["note"] else "")},
    ]
    _log(result, on_step, {"step": 0, "type": "info", "content": f"{scope} 회의 · 회의록 분량 {opts['length']}"
                           + ("" if opts["update_sheet"] else " · 결과지는 건드리지 않음")})

    started = time.time()
    try:
        _loop(result, llm, messages, on_step, active_tools(opts))
    except Exception as e:
        result.final_answer = f"(실행 오류: {e})"
        raise
    finally:
        result.run_log = save_run_log(result, llm, messages, started)
    return result


def _loop(result: RunResult, llm, messages: list[dict], on_step, schemas: list[dict] | None = None) -> None:
    nudges, schemas = 0, schemas or TOOL_SCHEMAS
    needs_sheet = any(t["function"]["name"] == "update_result_sheet" for t in schemas)
    for step in range(1, MAX_STEPS + 1):
        msg = llm.chat(messages, tools=schemas)
        _note_switch(llm, result, on_step)
        tool_calls = msg.get("tool_calls") or []

        if not tool_calls:
            content = (msg.get("content") or "").strip()
            # 끝내기 조건을 채우기 전에 빈 답이나 잡담으로 멈추면(일부 모델이 그렇다) 두 번까지 이어가라고 요구한다
            if not result.saved_path and nudges < MAX_NUDGES:
                nudges += 1
                todo = "회의록 저장(save_minutes)" if result.sheet_updated or not needs_sheet else "결과지 갱신(update_result_sheet)과 회의록 저장(save_minutes)"
                messages.append({"role": "assistant", "content": content or "(빈 응답)"})
                messages.append({"role": "user", "content": f"아직 일이 끝나지 않았다. 남은 것: {todo}. 도구를 호출해 마저 끝내라."})
                _log(result, on_step, {"step": step, "type": "nudge", "content": f"끝내기 조건 전에 멈춰 재요청 {nudges}/{MAX_NUDGES} (응답 {len(content)}자, 키 {sorted(msg.keys())})"})
                continue
            result.final_answer = content or ("" if result.saved_path else "(끝내기 조건을 채우지 못하고 멈췄습니다. 실행 과정의 거부 사유를 확인하세요)")
            _log(result, on_step, {"step": step, "type": "final", "content": result.final_answer})
            break

        assistant = {"role": "assistant", "content": msg.get("content") or "", "tool_calls": tool_calls}
        if msg.get("reasoning_details"):  # 추론형 모델(OpenRouter 규격)은 추론 기록을 되돌려줘야 다음 단계를 잇는다
            assistant["reasoning_details"] = msg["reasoning_details"]
        messages.append(assistant)
        for call in tool_calls:
            name = call["function"]["name"]
            try:
                args = json.loads(call["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            try:
                output = TOOL_FUNCTIONS[name](**args)
                ok = True
            except Exception as e:  # 도구 실패도 모델에게 돌려줘 스스로 고치게 한다
                output, ok = f"오류: {e}", False
            if name == "save_minutes" and ok:
                result.saved_path = output
            if name == "update_result_sheet" and ok:
                result.sheet_updated = True
            _log(result, on_step, {
                "step": step, "type": "tool", "name": name, "ok": ok,
                "args": {k: _brief(k, v) for k, v in args.items()},
                "output_preview": output[:500],
            })
            messages.append({"role": "tool", "tool_call_id": call["id"], "name": name, "content": output})

        # 정지조건: 회의록 저장이 끝났으면 도구 없이 보고만 받고 루프를 닫는다.
        # (도구를 계속 열어두면 모델이 save_minutes를 반복 호출하는 경우가 있었다)
        if result.saved_path:
            msg = llm.chat(messages, tools=None)
            _note_switch(llm, result, on_step)
            result.final_answer = (msg.get("content") or "").strip()
            _log(result, on_step, {"step": step + 1, "type": "final", "content": result.final_answer})
            break
    else:
        result.final_answer = "(최대 단계 수에 도달해 중단했습니다)"


def _brief(key: str, value):
    """화면·기록에 남길 인자 요약. 긴 글과 목록은 크기만 남긴다."""
    if key == "markdown":
        return f"(회의록 {len(value)}자)"
    if isinstance(value, list):
        return f"({len(value)}건)"
    if isinstance(value, str) and len(value) > 60:
        return value[:60] + "…"
    return value


def _masked(llm, customer_id: str, project_id: str, meeting_no: int) -> MaskedLLM:
    d = store.project_dir(customer_id, project_id)
    prev = memory.load_before(store.key(customer_id, project_id), d, meeting_no)
    names = store.known_names(customer_id, project_id) + [a["name"] for a in prev["attendees"]]
    texts = [p.read_text(encoding="utf-8") for p in sorted(d.glob("transcript_*.txt"))]
    return MaskedLLM(llm, Masker(*build_dictionary(store.load_customer(customer_id)["name"], texts, names)))


def _log(result: RunResult, on_step, entry: dict) -> None:
    result.trace.append(entry)
    if on_step:
        on_step(entry)


def _note_switch(llm, result: RunResult, on_step) -> None:
    """제공자가 폴백으로 바뀐 순간을 한 번만 기록한다."""
    reason = getattr(llm, "switched_reason", None)
    if reason and not any(e.get("type") == "route" for e in result.trace):
        _log(result, on_step, {"step": 0, "type": "route", "content": reason})


def save_run_log(result: RunResult, llm, messages: list[dict], started: float) -> str:
    """작업 기억을 파일로 남긴다. 외부로 나간 그대로(마스킹된 상태)를 저장해 기록 파일에 실명이 쌓이지 않게 한다."""
    masker = getattr(llm, "masker", None)
    hide = masker.mask if masker else (lambda x: x)

    def clean(v):
        if isinstance(v, str):
            return hide(v)
        if isinstance(v, list):
            return [clean(x) for x in v]
        if isinstance(v, dict):
            return {k: clean(x) for k, x in v.items()}
        return v

    calls = list(getattr(llm, "usage_log", None) or [])
    rejected = lambda name: sum(1 for t in result.trace if t.get("name") == name and not t.get("ok"))
    log = {
        "customer_id": result.customer_id, "project_id": result.project_id, "meeting_no": result.meeting_no,
        "started": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(started)),
        "elapsed_sec": round(time.time() - started, 1),
        "saved": bool(result.saved_path), "sheet_updated": result.sheet_updated, "options": result.options,
        "rejections": {"update_result_sheet": rejected("update_result_sheet"), "save_minutes": rejected("save_minutes")},
        "tool_sequence": [t["name"] for t in result.trace if t.get("type") == "tool"],  # 모델이 고른 순서(실행마다 다를 수 있다)
        "route": getattr(llm, "switched_reason", None),
        "masking": masker.summary() if masker else "off",
        "usage": {"calls": len(calls), "prompt_tokens": sum(c["prompt_tokens"] for c in calls),
                  "completion_tokens": sum(c["completion_tokens"] for c in calls), "by_call": calls},
        "final_answer": hide(result.final_answer),
        "trace": clean(result.trace),
        "messages": clean(messages),
    }
    d = RUNS_DIR / result.customer_id / result.project_id
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{result.meeting_no:02d}_{time.strftime('%Y%m%d-%H%M%S', time.localtime(started))}.json"
    p.write_text(json.dumps(log, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return str(p)
