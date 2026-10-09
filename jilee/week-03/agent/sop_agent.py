"""SOP 조회 서브에이전트.

회의록 에이전트(주 에이전트)가 "SOP상 무엇을 확인·기록해야 하나"를 물으면,
knowledge/sop/ 발췌본에서 관련 문단을 찾아(키워드 검색) 그 문단만 근거로 답한다.
한 도구를 통해서만 SOP를 참조하므로 회사 규칙을 따르는 맥락이 실행마다 같게 유지된다.

흐름: 질문 → 문단 검색(파이썬, 결정론적) → 발췌 ≤ 2,500자 → 서브에이전트 LLM 호출 → 답 + 근거 절
"""

from __future__ import annotations

import math
import re
from pathlib import Path

from .llm import LLMClient

SOP_DIR = Path(__file__).resolve().parent.parent / "knowledge" / "sop"
MAX_CONTEXT_CHARS = 2500  # 무료 요금제 분당 토큰 한도를 고려한 발췌 상한

SUB_SYSTEM_PROMPT = """당신은 영업 SOP 담당자다. 아래 SOP 발췌만 근거로 질문에 답한다.
- 발췌에 있는 내용만으로 답한다. 발췌에 근거가 없으면 "SOP 발췌에 해당 규정 없음"이라고 쓴다.
- 답은 한국어 5줄 이내. 확인 항목·기록 규칙·최소 완료 조건을 그대로 인용하고, 끝에 근거 절 번호(예: 2.3.2, 10.2.3)를 적는다.
"""


def _sections(path: Path) -> list[dict]:
    """제목(#·##·###·'**n.n.n**') 단위로 자르고, 긴 절은 표 행 또는 문단으로 더 쪼갠다.
    SOP 정본은 문서마다 형식이 달라(표 위주 / 제목 위주) 두 방식을 함께 쓴다. mermaid 블록은 뺀다."""
    text = re.sub(r"```mermaid.*?```", "", path.read_text(encoding="utf-8"), flags=re.S)
    raw, title, buf = [], path.stem, []
    for line in text.splitlines():
        m = re.match(r"^(#{1,4} .+|\*\*\d+(\.\d+){1,2} .+\*\*)$", line.strip())
        if m:
            if buf:
                raw.append((title, "\n".join(buf).strip()))
            title, buf = m.group(1).strip("#* "), [line]
        else:
            buf.append(line)
    if buf:
        raw.append((title, "\n".join(buf).strip()))

    chunks = []
    for t, body in raw:
        if len(body) <= 2000:
            chunks.append({"file": path.name, "title": t, "text": body})
            continue
        rows = [l for l in body.splitlines() if l.startswith("| `") or l.startswith("| IX")]
        if rows:  # 표 위주 문서: 행 하나가 태스크 하나
            for r in rows:
                cells = [c.strip(" `") for c in r.strip("|").split("|")]
                chunks.append({"file": path.name, "title": f"{t} · " + " / ".join(cells[1:5]), "text": r})
        else:  # 제목 위주 문서: 빈 줄 기준 문단을 1,500자 안팎으로 묶음
            para, acc = [], 0
            for blk in re.split(r"\n\s*\n", body):
                if acc + len(blk) > 1500 and para:
                    chunks.append({"file": path.name, "title": t, "text": "\n\n".join(para)})
                    para, acc = [], 0
                para.append(blk)
                acc += len(blk)
            if para:
                chunks.append({"file": path.name, "title": t, "text": "\n\n".join(para)})
    return [c for c in chunks if len(c["text"]) > 40]


def _terms(q: str) -> list[str]:
    return [t for t in re.findall(r"[가-힣A-Za-z0-9]{2,}", q) if t not in {"무엇", "어떻게", "해야", "하나", "합니까", "있나"}]


def search_sop(question: str, limit_chars: int = MAX_CONTEXT_CHARS) -> list[dict]:
    """질문의 단어가 많이 겹치는 문단부터 고른다. LLM 없이 동작하므로 테스트가 쉽다."""
    terms = _terms(question)
    chunks = [c for path in sorted(SOP_DIR.rglob("*.md")) for c in _sections(path)]
    # 흔한 단어("결과"·"기록")가 점수를 지배하지 않도록, 드문 단어일수록 가중한다(IDF)
    n = len(chunks) or 1
    weight = {t: math.log(n / (1 + sum(1 for c in chunks if t in c["text"] or t in c["title"]))) + 0.1 for t in terms}
    scored = []
    for c in chunks:
        score = sum(weight[t] * (c["text"].count(t) + 3 * c["title"].count(t)) for t in terms)
        if score > 0:
            scored.append((round(score, 2), c))
    scored.sort(key=lambda x: -x[0])
    picked, used = [], 0
    for score, c in scored:
        if used + len(c["text"]) > limit_chars:
            continue
        picked.append({**c, "score": score})
        used += len(c["text"])
    return picked


def consult_sop(question: str, llm: LLMClient | None = None) -> str:
    """주 에이전트가 호출하는 도구 본체. 검색 → 서브에이전트 답변."""
    hits = search_sop(question)
    if not hits:
        return "SOP 발췌에서 관련 문단을 찾지 못했습니다. 질문에 SOP 용어(예: 예산, 의사결정 조직, 미합의, 참석자)를 넣어 다시 물어보세요."
    excerpt = "\n\n---\n\n".join(f"[{h['title']}]\n{h['text']}" for h in hits)
    llm = llm or LLMClient()
    msg = llm.chat([
        {"role": "system", "content": SUB_SYSTEM_PROMPT + "\n\n# SOP 발췌\n" + excerpt},
        {"role": "user", "content": question},
    ])
    answer = (msg.get("content") or "").strip()
    return answer + "\n\n(참조 절: " + ", ".join(h["title"] for h in hits) + ")"
