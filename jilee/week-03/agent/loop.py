"""Agent Loop: 모델이 Tool을 고르고 → 파이썬이 실행하고 → 결과를 다시 모델에 넣는 반복.

흐름(정상 경로)
  1. read_deal_context  : 고객정보 + 직전 회의록(기억)
  2. read_transcript    : 이번 회의 녹취
  (선택) consult_sop    : SOP 서브에이전트에 규칙 확인
  3. save_minutes       : 표준 양식 회의록 저장
  4. 최종 답변(요약)     → 루프 종료
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable

from .llm import LLMClient
from .tools import KNOWLEDGE_DIR, TOOL_FUNCTIONS, TOOL_SCHEMAS

MAX_STEPS = 8


def build_system_prompt(use_knowledge: bool = True) -> str:
    question_list = (KNOWLEDGE_DIR / "표준질문리스트.md").read_text(encoding="utf-8")
    template = (KNOWLEDGE_DIR / "회의록_표준양식.md").read_text(encoding="utf-8")
    prompt = f"""당신은 영업 조직의 회의록 담당자다. 회의 녹취/메모를 표준 양식 회의록으로 정리하고 액션아이템을 뽑는다.

작업 순서를 반드시 지킨다.
1. read_deal_context(deal_id, meeting_no)로 고객정보와 직전 회의록을 읽는다.
2. read_transcript로 이번 회의 녹취를 읽는다.
3. 아래 표준 양식에 맞춰 회의록을 작성하고 save_minutes로 저장한다.
   - 직전 회의록의 액션아이템은 "2. 지난 회의 액션아이템 점검"에 옮겨 적고, 이번 녹취에서 결과를 확인해 완료·진행 중·미착수로 표시한다.
   - "8. 확보된 표준 질문 항목"은 아래 표준 질문 리스트의 Q코드로 적는다. 직전 회의록에서 이미 확보된 항목과 이번에 새로 확보된 항목을 합쳐 미확보 항목을 남긴다.
4. 저장이 끝나면 한국어로 3줄 이내 요약(확보율 %, 새 액션아이템 수, 미확보 항목)을 답하고 끝낸다.
(선택) 회의록을 쓰기 전에 SOP상 처리 방법이 애매한 점이 있으면 consult_sop로 한 번만 묻고, 답을 회의록에 반영한다. 예: 기한 없는 일의 처리, 참석자 기록 범위, 예산·의사결정 조직 기록 항목.

규칙: 녹취와 고객정보에 있는 내용만으로 쓴다. 고객의 말은 고객 표현 그대로 옮긴다. 확인되지 않은 항목은 "확인 불가"로 적는다. 담당과 기한이 모두 있는 일만 액션아이템에 올리고, 그 밖의 일은 미합의 사항에 둔다. 모든 답변은 한국어로 쓴다.
녹취는 음성인식(STT) 결과라 오타·띄어쓰기 오류·잘못 들린 고유명사가 섞여 있다. 문맥과 고객정보로 바로잡아 적고, 확신이 서지 않는 표기는 원문을 괄호로 함께 남긴다.

{template}

{question_list}
"""
    if not use_knowledge:
        # 비교 실험용: 양식·질문 리스트 없이 "회의록을 써라"만 준다
        return """당신은 영업 조직의 회의록 담당자다. read_deal_context(deal_id, meeting_no)로 고객정보와 직전 회의록을 읽고,
read_transcript로 이번 회의 녹취를 읽은 뒤, 회의록을 마크다운으로 작성해 save_minutes로 저장한다.
저장이 끝나면 한국어로 3줄 이내 요약을 답하고 끝낸다. 답변은 모두 한국어로 쓴다."""
    return prompt


@dataclass
class RunResult:
    deal_id: str
    meeting_no: int
    final_answer: str = ""
    trace: list[dict] = field(default_factory=list)
    saved_path: str | None = None


def run_agent(
    deal_id: str,
    meeting_no: int,
    llm: LLMClient | None = None,
    on_step: Callable[[dict], None] | None = None,
    use_knowledge: bool = True,
) -> RunResult:
    llm = llm or LLMClient()
    result = RunResult(deal_id=deal_id, meeting_no=meeting_no)
    messages = [
        {"role": "system", "content": build_system_prompt(use_knowledge)},
        {"role": "user", "content": f"딜 {deal_id}의 {meeting_no}차 회의 녹취를 회의록으로 정리해 저장해줘."},
    ]

    nudges = 0
    for step in range(1, MAX_STEPS + 1):
        msg = llm.chat(messages, tools=TOOL_SCHEMAS)
        _note_switch(llm, result, on_step)
        tool_calls = msg.get("tool_calls") or []

        if not tool_calls:
            content = (msg.get("content") or "").strip()
            # 저장 전에 빈 답이나 잡담으로 멈추면(일부 모델이 그렇다) 두 번까지 이어가라고 요구한다
            if not result.saved_path and nudges < 2:
                nudges += 1
                messages.append({"role": "assistant", "content": content or "(빈 응답)"})
                messages.append({"role": "user", "content": "아직 회의록이 저장되지 않았다. 작업 순서대로 도구를 호출해 회의록을 작성하고 save_minutes로 저장하라."})
                _log(result, on_step, {"step": step, "type": "nudge", "content": f"저장 없이 멈춰 재요청 {nudges}/2 (응답 {len(content)}자, 키 {sorted(msg.keys())})"})
                continue
            result.final_answer = content
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
            _log(result, on_step, {
                "step": step, "type": "tool", "name": name, "ok": ok,
                "args": {k: (v if k != "markdown" else f"(회의록 {len(v)}자)") for k, v in args.items()},
                "output_preview": output[:300],
            })
            messages.append({"role": "tool", "tool_call_id": call["id"], "name": name, "content": output})

        # 정지조건: 저장이 끝났으면 도구 없이 요약만 받고 루프를 닫는다.
        # (도구를 계속 열어두면 모델이 save_minutes를 반복 호출하는 경우가 있었다)
        if result.saved_path:
            msg = llm.chat(messages, tools=None)
            _note_switch(llm, result, on_step)
            result.final_answer = (msg.get("content") or "").strip()
            _log(result, on_step, {"step": step + 1, "type": "final", "content": result.final_answer})
            break
    else:
        result.final_answer = "(최대 단계 수에 도달해 중단했습니다)"

    return result


def _log(result: RunResult, on_step, entry: dict) -> None:
    result.trace.append(entry)
    if on_step:
        on_step(entry)


def _note_switch(llm, result: RunResult, on_step) -> None:
    """제공자가 폴백으로 바뀐 순간을 한 번만 기록한다."""
    reason = getattr(llm, "switched_reason", None)
    if reason and not any(e.get("type") == "route" for e in result.trace):
        _log(result, on_step, {"step": 0, "type": "route", "content": reason})
