"""에이전트가 호출하는 Tool 4종(조회 2·SOP 문의·저장)과 그 스키마.

모델은 판단만 하고, 파일을 읽고 쓰는 행동은 여기 있는 파이썬 함수가 한다.
Week 4: 조회는 딜 기억(상태 파일)을 읽고, 긴 녹취는 구간별로 정리해 넘기며, 저장은 규칙 검사를 통과해야 한다.
"""

from __future__ import annotations

import re
from pathlib import Path

from . import memory
from .sop_agent import consult_sop

ROOT = Path(__file__).resolve().parent.parent
DEALS_DIR = ROOT / "deals"          # 딜별 고객정보·녹취·회의록이 한 폴더에 있다
OUTPUT_DIR = DEALS_DIR              # 회의록 저장 위치(비교 실험 때만 experiments/ 로 바꾼다)
KNOWLEDGE_DIR = ROOT / "knowledge"

MEMORY_MODE = "state"     # "state": 딜 기억(상태 파일) · "last": Week 3 방식(직전 회의록 전문 1건). 비교 실험용
VALIDATE = True           # 저장 전 규칙 검사. 지식베이스 없는 비교 실험에서는 끈다
LONG_TRANSCRIPT = 4000    # 이보다 긴 녹취는 구간별로 정리해서 넘긴다(분당 토큰 한도·출력 상한 대응)
CHUNK_CHARS = 2500

_run_llm = None           # 실행 중인 LLM(마스킹 포함). 녹취 구간 정리에 같은 클라이언트를 쓴다


def bind_llm(llm) -> None:
    global _run_llm
    _run_llm = llm


def _deal_dir(deal_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", deal_id):
        raise ValueError(f"잘못된 딜 코드: {deal_id}")
    d = DEALS_DIR / deal_id
    if not d.is_dir():
        raise FileNotFoundError(f"딜 폴더가 없습니다: {deal_id}")
    return d


def list_deals() -> list[dict]:
    deals = []
    for d in sorted(DEALS_DIR.iterdir()):
        if not d.is_dir():
            continue
        transcripts = sorted(p.name for p in d.glob("transcript_*.txt"))
        minutes = sorted(p.name for p in (OUTPUT_DIR / d.name).glob("minutes_*.md")) if (OUTPUT_DIR / d.name).exists() else []
        deals.append({"deal_id": d.name, "transcripts": transcripts, "minutes": minutes})
    return deals


# ---- Tool ① 조회 ---------------------------------------------------------

def read_deal_context(deal_id: str, meeting_no: int | None = None) -> str:
    """고객정보와 딜 기억을 합쳐 돌려준다. 이것이 에이전트의 '기억'이다.

    state 모드: 1~(N-1)차 회의에서 쌓인 참석자·합의·미합의·액션아이템·Q코드 현황(상태 파일)을 준다.
    last 모드: Week 3 방식. meeting_no보다 앞선 회의록 중 가장 최근 것 1건의 전문을 준다.
    """
    d = _deal_dir(deal_id)
    parts = ["## 고객정보\n" + (d / "customer.md").read_text(encoding="utf-8")]
    out = OUTPUT_DIR / deal_id
    if MEMORY_MODE == "state":
        n = int(meeting_no) if meeting_no is not None else 1 + max([int(q.stem.split("_")[1]) for q in out.glob("minutes_*.md")] or [0])
        st = memory.load_before(deal_id, out, out, n)
        parts.append(f"## 딜 기억 ({n - 1}차까지 누적)\n" + memory.render_state(st))
        return "\n\n".join(parts)
    prev = sorted(out.glob("minutes_*.md")) if out.exists() else []
    if meeting_no is not None:
        prev = [q for q in prev if int(q.stem.split("_")[1]) < int(meeting_no)]
    if prev:
        parts.append(f"## 직전 회의록 ({prev[-1].name})\n" + prev[-1].read_text(encoding="utf-8"))
    else:
        parts.append("## 직전 회의록\n(없음 — 이번이 첫 회의)")
    return "\n\n".join(parts)


def read_transcript(deal_id: str, meeting_no: int) -> str:
    """지정한 회차의 녹취/메모 원문을 돌려준다. 길면 구간별 정리본을 돌려준다."""
    d = _deal_dir(deal_id)
    p = d / f"transcript_{int(meeting_no):02d}.txt"
    if not p.exists():
        raise FileNotFoundError(f"녹취 파일이 없습니다: {p.name}")
    text = p.read_text(encoding="utf-8")
    if len(text) <= LONG_TRANSCRIPT or _run_llm is None:
        return text
    return summarize_long_transcript(text, _run_llm)


CHUNK_PROMPT = """당신은 영업 회의 녹취 정리 담당자다. 아래는 긴 회의 녹취의 한 구간이다. 이 구간만 보고 다음을 한국어로 정리한다.
녹취는 음성인식 결과라 오타·잘못 들린 이름이 섞여 있다. 머리말의 참석자 명단으로 바로잡고, 확신이 없으면 원문을 괄호로 남긴다.

### 논의 요지
- 화자: 요지 (중요한 고객 발언은 "원문 그대로" 인용)
### 결정·합의
- 결정 내용 (이 구간 안에서 앞서 한 말을 뒤집었으면 "[번복] 처음 말 → 바뀐 말"로 표시)
### 약속·할 일
- 할 일 / 담당 / 기한(말한 그대로)
### 숫자
- 금액·수량·날짜·비율 (말한 그대로와 숫자 표기를 함께)
### 미결
- 정하지 못한 것

이 구간에 없는 내용은 쓰지 않는다. 해당 없는 항목은 "- 없음"."""


def split_transcript(text: str, size: int = CHUNK_CHARS) -> tuple[str, list[str]]:
    """머리말(일시·참석 줄)과 발화 단위 구간 목록. 발화 한 줄은 자르지 않는다."""
    lines = text.splitlines()
    head = [l for l in lines[:3] if l.startswith("[") or l.startswith("참석")]
    body = lines[len(head):]
    chunks, buf = [], []
    for line in body:
        if buf and sum(len(b) for b in buf) + len(line) > size:
            chunks.append("\n".join(buf))
            buf = []
        buf.append(line)
    if buf:
        chunks.append("\n".join(buf))
    return "\n".join(head), [c for c in chunks if c.strip()]


def summarize_long_transcript(text: str, llm) -> str:
    """map 단계: 구간마다 정리본을 만든다. reduce(회의록 작성)는 주 에이전트가 한다."""
    head, chunks = split_transcript(text)
    parts = [f"(녹취가 {len(text):,}자로 길어 {len(chunks)}개 구간으로 나눠 정리했다. 구간 순서가 시간 순서다. "
             "뒤 구간의 결정이 앞 구간과 다르면 뒤의 것이 최종이며, [번복] 표시를 회의록 3절·5절에 반영한다.)", head]
    for i, c in enumerate(chunks, start=1):
        msg = llm.chat([{"role": "system", "content": CHUNK_PROMPT},
                        {"role": "user", "content": f"{head}\n\n[구간 {i}/{len(chunks)}]\n{c}"}])
        parts.append(f"## 구간 {i}/{len(chunks)}\n" + (msg.get("content") or "(정리 실패)").strip())
    return "\n\n".join(parts)


# ---- Tool ② 저장 ---------------------------------------------------------

def save_minutes(deal_id: str, meeting_no: int, markdown: str) -> str:
    """규칙 검사를 통과한 회의록만 저장하고, 딜 기억(상태 파일)을 갱신한다."""
    d = _deal_dir(deal_id)
    out = OUTPUT_DIR / deal_id
    meeting_no = int(meeting_no)
    prev = memory.load_before(deal_id, out, out, meeting_no)
    if VALIDATE:
        known = (d / "customer.md").read_text(encoding="utf-8") + " ".join(a["name"] for a in prev["attendees"])
        tp = d / f"transcript_{meeting_no:02d}.txt"
        known += tp.read_text(encoding="utf-8") if tp.exists() else ""
        errs = memory.validate(markdown, known, prev)
        if errs:
            raise ValueError("저장 거부 — 아래를 고친 회의록 전문으로 save_minutes를 다시 호출하라.\n"
                             + "\n".join(f"{i}. {e}" for i, e in enumerate(errs, start=1)))
    out.mkdir(parents=True, exist_ok=True)
    p = out / f"minutes_{meeting_no:02d}.md"
    p.write_text(markdown.strip() + "\n", encoding="utf-8")
    st = memory.update_state(prev, markdown, meeting_no)
    memory.save_snapshot(out, st)
    k = memory.kpi(st)
    shown = p.relative_to(ROOT).as_posix() if p.is_relative_to(ROOT) else str(p)
    return f"저장 완료: {shown} ({len(markdown)}자) · 딜 기억 갱신 · 확보율 {k['확보율']} · 지난 액션 이행률 {k['이행률']}"


# ---- 스키마 (모델에게 알려주는 도구 설명) --------------------------------

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "consult_sop",
            "description": "영업 SOP 담당자(서브에이전트)에게 묻는다. 어떤 항목을 확인·기록해야 하는지, 미합의·참석자·예산 같은 내용을 SOP상 어떻게 다뤄야 하는지 애매할 때 한 번 호출한다. 예: '예산 확인 시 무엇을 기록해야 하나'",
            "parameters": {
                "type": "object",
                "properties": {"question": {"type": "string", "description": "SOP 용어를 넣은 한 문장 질문"}},
                "required": ["question"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_deal_context",
            "description": "딜의 고객정보와 딜 기억(지난 회의들의 참석자·합의·미합의·액션아이템·표준 질문 확보 현황)을 읽는다. 회의록을 쓰기 전에 반드시 먼저 호출한다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "deal_id": {"type": "string", "description": "딜 코드 (예: D001)"},
                    "meeting_no": {"type": "integer", "description": "이번 회차 번호. 이보다 앞선 회의만 기억으로 읽는다"},
                },
                "required": ["deal_id", "meeting_no"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_transcript",
            "description": "지정한 회차의 회의 녹취/메모를 읽는다. 긴 녹취는 구간별 정리본으로 돌려준다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "deal_id": {"type": "string"},
                    "meeting_no": {"type": "integer", "description": "회차 번호"},
                },
                "required": ["deal_id", "meeting_no"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_minutes",
            "description": "표준 양식으로 작성한 회의록 마크다운을 저장한다. 작성이 끝나면 반드시 호출한다. 규칙 검사에 걸리면 저장 거부 사유가 돌아오니 고쳐서 다시 호출한다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "deal_id": {"type": "string"},
                    "meeting_no": {"type": "integer"},
                    "markdown": {"type": "string", "description": "회의록 전문(마크다운)"},
                },
                "required": ["deal_id", "meeting_no", "markdown"],
            },
        },
    },
]

TOOL_FUNCTIONS = {
    "consult_sop": consult_sop,
    "read_deal_context": read_deal_context,
    "read_transcript": read_transcript,
    "save_minutes": save_minutes,
}
