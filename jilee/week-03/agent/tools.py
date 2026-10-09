"""에이전트가 호출하는 Tool 4종(조회 2·SOP 문의·저장)과 그 스키마.

모델은 판단만 하고, 파일을 읽고 쓰는 행동은 여기 있는 파이썬 함수가 한다.
"""

from __future__ import annotations

import re
from pathlib import Path

from .sop_agent import consult_sop

ROOT = Path(__file__).resolve().parent.parent
DEALS_DIR = ROOT / "deals"          # 딜별 고객정보·녹취·회의록이 한 폴더에 있다
OUTPUT_DIR = DEALS_DIR              # 회의록 저장 위치(비교 실험 때만 experiments/ 로 바꾼다)
KNOWLEDGE_DIR = ROOT / "knowledge"


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
    """고객정보와 직전 회의록(있으면)을 합쳐 돌려준다. 이것이 에이전트의 '기억'이다.

    meeting_no를 주면 그 회차보다 앞선 회의록 중 가장 최근 것만 직전 회의록으로 본다
    (1차를 다시 돌릴 때 2차 회의록을 '직전'으로 읽는 일을 막는다).
    """
    d = _deal_dir(deal_id)
    parts = ["## 고객정보\n" + (d / "customer.md").read_text(encoding="utf-8")]
    out = OUTPUT_DIR / deal_id
    prev = sorted(out.glob("minutes_*.md")) if out.exists() else []
    if meeting_no is not None:
        prev = [q for q in prev if int(q.stem.split("_")[1]) < int(meeting_no)]
    if prev:
        parts.append(f"## 직전 회의록 ({prev[-1].name})\n" + prev[-1].read_text(encoding="utf-8"))
    else:
        parts.append("## 직전 회의록\n(없음 — 이번이 첫 회의)")
    return "\n\n".join(parts)


def read_transcript(deal_id: str, meeting_no: int) -> str:
    """지정한 회차의 녹취/메모 원문을 돌려준다."""
    d = _deal_dir(deal_id)
    p = d / f"transcript_{int(meeting_no):02d}.txt"
    if not p.exists():
        raise FileNotFoundError(f"녹취 파일이 없습니다: {p.name}")
    return p.read_text(encoding="utf-8")


# ---- Tool ② 저장 ---------------------------------------------------------

def save_minutes(deal_id: str, meeting_no: int, markdown: str) -> str:
    """표준 양식 회의록을 저장한다. 저장된 파일은 다음 회의의 누적 자료가 된다."""
    _deal_dir(deal_id)
    out = OUTPUT_DIR / deal_id
    out.mkdir(parents=True, exist_ok=True)
    p = out / f"minutes_{int(meeting_no):02d}.md"
    p.write_text(markdown.strip() + "\n", encoding="utf-8")
    shown = p.relative_to(ROOT).as_posix() if p.is_relative_to(ROOT) else str(p)
    return f"저장 완료: {shown} ({len(markdown)}자)"


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
            "description": "딜의 고객정보와 직전 회의록을 읽는다. 회의록을 쓰기 전에 반드시 먼저 호출한다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "deal_id": {"type": "string", "description": "딜 코드 (예: D001)"},
                    "meeting_no": {"type": "integer", "description": "이번 회차 번호. 이보다 앞선 회의록만 직전 회의록으로 읽는다"},
                },
                "required": ["deal_id", "meeting_no"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_transcript",
            "description": "지정한 회차의 회의 녹취/메모 원문을 읽는다.",
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
            "description": "표준 양식으로 작성한 회의록 마크다운을 저장한다. 작성이 끝나면 반드시 호출한다.",
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
