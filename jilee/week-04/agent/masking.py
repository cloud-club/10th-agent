"""마스킹: 외부 LLM으로 나가는 글에서 고객사명·인명을 자리표시자로 바꾸고, 돌아온 답에서 되돌린다.

LLM 호출 한 곳(MaskedLLM.chat)에서만 처리하므로 주 에이전트·녹취 구간 정리·SOP 문의가 모두 같은 규칙을 탄다.
파일에 저장되는 회의록과 상태 파일에는 실명이 그대로 남는다(마스킹은 전송 구간에만 적용).

사전(무엇을 가릴지)은 딜 폴더에서 만든다: 고객정보 제목의 회사명, 고객정보·녹취 참석 줄의 인명,
녹취의 화자 표기('이름:'), 딜 기억의 참석자. 사전에 없는 표기(STT가 잘못 들은 이름, '정팀장님' 같은 성+직함)는 가려지지 않는다.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

TITLE_WORDS = r"(팀장|과장|부장|차장|대리|사원|이사|상무|전무|대표|사장|공장장|본부장|실장|매니저|책임|선임|주임)"


def build_dictionary(deal_dir: Path, extra_names: list[str] | None = None) -> tuple[list[str], list[str]]:
    """(회사명 목록, 인명 목록)."""
    customer = (deal_dir / "customer.md").read_text(encoding="utf-8")
    companies = []
    m = re.search(r"^#\s*고객정보\s*[—-]\s*(.+)$", customer, re.M)
    if m:
        full = m.group(1).strip()
        companies = [full, re.sub(r"\((주|유|합)\)|㈜|주식회사", "", full).strip()]

    names = set(extra_names or [])
    texts = [customer] + [p.read_text(encoding="utf-8") for p in sorted(deal_dir.glob("transcript_*.txt"))]
    for t in texts:
        names |= set(re.findall(r"^([가-힣]{2,4})\s*:", t, re.M))  # 화자 표기
        for line in re.findall(r"(?:참석|당사 담당|고객 접점)[^:：]*[:：](.*)", t):
            for part in re.split(r"[,/·]", re.sub(r"\((고객|당사)\)", "", line)):
                m = re.match(r"\s*([가-힣]{2,4}?)(?=\s|\(|$|" + TITLE_WORDS + ")", part)
                if m:
                    names.add(m.group(1))
    company_words = {w for c in companies for w in re.findall(r"[가-힣]+", c)}
    not_names = {"참석", "일시", "장소", "메모", "고객", "당사", "안건", "기록", "비고"}
    names = {n for n in names if len(n) >= 2 and n not in company_words | not_names}
    return [c for c in dict.fromkeys(companies) if c], sorted(names)


class Masker:
    def __init__(self, companies: list[str], names: list[str]):
        self.forward: dict[str, str] = {}
        for i, c in enumerate(companies):
            self.forward[c] = "[고객사A]"  # 정식 명칭과 약칭은 같은 자리표시자
        for i, n in enumerate(names, start=1):
            self.forward[n] = f"[인물{i}]"
        # 긴 것부터 바꿔야 '한빛정밀(주)'가 '한빛정밀'보다 먼저 잡힌다
        self._order = sorted(self.forward, key=len, reverse=True)
        self.backward = {}
        for k in self._order:
            self.backward.setdefault(self.forward[k], k)  # 자리표시자 → 가장 긴 원문
        self.hits = 0

    def mask(self, s: str) -> str:
        for k in self._order:
            if k in s:
                self.hits += s.count(k)
                s = s.replace(k, self.forward[k])
        return s

    def unmask(self, s: str) -> str:
        for ph, orig in self.backward.items():
            s = s.replace(ph, orig)
        return s

    def summary(self) -> str:
        n_c = len({v for v in self.forward.values() if v.startswith("[고객사")})
        n_p = len({v for v in self.forward.values() if v.startswith("[인물")})
        return f"마스킹 사전: 고객사 {n_c} · 인물 {n_p}"


class MaskedLLM:
    """LLMClient를 감싸 보내기 전 마스킹, 받은 뒤 복원한다. chat() 서명은 LLMClient와 같다."""

    def __init__(self, llm, masker: Masker):
        self.llm, self.masker = llm, masker

    def __getattr__(self, name):  # model, switched_reason 등은 원래 클라이언트 것을 쓴다
        return getattr(self.llm, name)

    def chat(self, messages: list[dict], tools: list[dict] | None = None, **kw) -> dict:
        out = []
        for m in messages:
            m = dict(m)
            if isinstance(m.get("content"), str):
                m["content"] = self.masker.mask(m["content"])
            if m.get("tool_calls"):
                m["tool_calls"] = [self._map_call(c, self.masker.mask) for c in m["tool_calls"]]
            out.append(m)
        msg = dict(self.llm.chat(out, tools=tools, **kw))
        if isinstance(msg.get("content"), str):
            msg["content"] = self.masker.unmask(msg["content"])
        if msg.get("tool_calls"):
            msg["tool_calls"] = [self._map_call(c, self.masker.unmask) for c in msg["tool_calls"]]
        return msg

    @staticmethod
    def _map_call(call: dict, fn) -> dict:
        call = json.loads(json.dumps(call))
        f = call.get("function", {})
        if isinstance(f.get("arguments"), str):
            # 모델이 한글을 \uXXXX로 보낼 수 있어 문자열 치환 대신 JSON을 풀어 값마다 바꾼다
            try:
                f["arguments"] = json.dumps(_map_values(json.loads(f["arguments"] or "{}"), fn), ensure_ascii=False)
            except json.JSONDecodeError:
                f["arguments"] = fn(f["arguments"])
        return call


def _map_values(v, fn):
    if isinstance(v, str):
        return fn(v)
    if isinstance(v, list):
        return [_map_values(x, fn) for x in v]
    if isinstance(v, dict):
        return {k: _map_values(x, fn) for k, x in v.items()}
    return v
