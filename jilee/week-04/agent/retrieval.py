"""딜 이력 검색: 녹취 발화와 회의록 항목을 맥락 꼬리표가 붙은 조각으로 저장하고, 하이브리드로 찾는다.

검색 순서
  1. 메타데이터 필터   : 딜·회차 범위·조각 종류로 후보를 먼저 좁힌다(다른 딜이 섞이지 않는다)
  2. 검색기 여러 개    : BM25(키워드, 한글 2글자 조각) — 벡터 검색은 VECTOR_RETRIEVER 자리에 끼운다
  3. RRF              : 검색기마다 다른 점수 대신 순위만 합친다. 1/(60+순위)의 합
  4. 앞뒤 발화 붙이기  : 찾은 발화 앞뒤 WINDOW개를 함께 돌려준다("이십이일까지"가 무엇에 대한 답인지 보이게)
  5. 최신성 표시       : 조각의 회차가 딜 기억의 최신 회차보다 앞서면 "현재 사실은 딜 기억 기준"을 붙인다

색인은 딜 폴더 파일의 수정 시각이 바뀔 때만 다시 만든다(질문마다 전체를 다시 훑지 않는다).
운영 환경(AWS)에서는 1·2·3을 OpenSearch(하이브리드 검색) 또는 PostgreSQL+pgvector로 옮긴다.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from pathlib import Path
from typing import Callable

from . import memory

RRF_K = 60
WINDOW = 2
VECTOR_RETRIEVER: Callable[[str, list[dict]], list[str]] | None = None  # (질문, 후보 조각) → 조각 id 순위. 임베딩 붙일 자리

_UTTER = re.compile(r"^([가-힣]{2,4})\s*:\s*(.+)$")
_HEAD_DATE = re.compile(r"^\[(\d{4}-\d{2}-\d{2})")


# ---- 조각 만들기 -------------------------------------------------------------

def build_chunks(deal_id: str, deal_dir: Path, minutes_dir: Path) -> list[dict]:
    chunks: list[dict] = []
    for tp in sorted(deal_dir.glob("transcript_*.txt")):
        n = int(tp.stem.split("_")[1])
        lines = tp.read_text(encoding="utf-8").splitlines()
        m = _HEAD_DATE.match(lines[0]) if lines else None
        date = m.group(1) if m else ""
        pos = 0
        for line in lines:
            u = _UTTER.match(line.strip().lstrip("- "))
            if not u or u.group(1) == "참석":
                continue
            pos += 1
            chunks.append({"id": f"{deal_id}-{n:02d}-u{pos:03d}", "deal": deal_id, "meeting": n, "date": date,
                           "kind": "발화", "speaker": u.group(1), "pos": pos, "text": u.group(2).strip()})
    section_kind = {3: "논의", 5: "합의", 6: "미합의", 7: "액션아이템"}
    for mp in sorted(minutes_dir.glob("minutes_*.md")):
        n = int(mp.stem.split("_")[1])
        md = mp.read_text(encoding="utf-8")
        m = re.search(r"일시:\s*(\d{4}-\d{2}-\d{2})", md)
        date = m.group(1) if m else ""
        secs = memory.split_sections(md)
        for no, kind in section_kind.items():
            body = secs.get(no, "")
            items = memory.bullets(body) or [" / ".join(r) for r in memory.parse_table(body)[1]]
            for i, t in enumerate(items, start=1):
                chunks.append({"id": f"{deal_id}-{n:02d}-m{no}-{i:02d}", "deal": deal_id, "meeting": n, "date": date,
                               "kind": f"회의록·{kind}", "speaker": "", "pos": i, "text": t})
    for c in chunks:  # 맥락 꼬리표: 조각만 떼어 봐도 언제·누가·무엇인지 알 수 있게
        who = f" · {c['speaker']}" if c["speaker"] else ""
        c["header"] = f"[{c['deal']} {c['meeting']}차 · {c['date']} · {c['kind']}{who}]"
    return chunks


# ---- BM25 (한글은 2글자 조각으로 잘라 조사 차이를 흡수) -----------------------

def tokenize(text: str) -> list[str]:
    toks = []
    for w in re.findall(r"[가-힣]+|[A-Za-z]+|\d+", text):
        if re.fullmatch(r"[가-힣]+", w):
            toks += [w] if len(w) == 1 else [w[i:i + 2] for i in range(len(w) - 1)]
        else:
            toks.append(w.lower())
    return toks


class BM25:
    def __init__(self, docs: dict[str, str], k1: float = 1.2, b: float = 0.75):
        self.k1, self.b = k1, b
        self.tf = {i: Counter(tokenize(t)) for i, t in docs.items()}
        self.len = {i: sum(c.values()) for i, c in self.tf.items()}
        self.avg = (sum(self.len.values()) / len(self.len)) if self.len else 1
        df = Counter(t for c in self.tf.values() for t in c)
        n = len(self.tf)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}
        self.postings: dict[str, list[str]] = {}
        for i, c in self.tf.items():
            for t in c:
                self.postings.setdefault(t, []).append(i)

    def rank(self, query: str, allowed: set[str] | None = None) -> list[str]:
        scores: Counter = Counter()
        for t in set(tokenize(query)):
            for i in self.postings.get(t, []):  # 역색인: 단어가 든 조각만 계산한다
                if allowed is not None and i not in allowed:
                    continue
                f = self.tf[i][t]
                scores[i] += self.idf[t] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * self.len[i] / self.avg))
        return [i for i, _ in scores.most_common()]


def rrf(rankings: list[list[str]], k: int = RRF_K) -> list[str]:
    score: Counter = Counter()
    for ranking in rankings:
        for r, i in enumerate(ranking, start=1):
            score[i] += 1 / (k + r)
    return [i for i, _ in score.most_common()]


# ---- 색인 캐시 ---------------------------------------------------------------

_cache: dict[str, tuple[tuple, list[dict], BM25]] = {}


def _index(deal_id: str, deal_dir: Path, minutes_dir: Path) -> tuple[list[dict], BM25]:
    files = sorted(list(deal_dir.glob("transcript_*.txt")) + list(minutes_dir.glob("minutes_*.md")))
    sig = tuple((str(p), p.stat().st_mtime_ns) for p in files)
    hit = _cache.get(deal_id)
    if hit and hit[0] == sig:
        return hit[1], hit[2]
    chunks = build_chunks(deal_id, deal_dir, minutes_dir)
    bm = BM25({c["id"]: c["header"] + " " + c["text"] for c in chunks})
    _cache[deal_id] = (sig, chunks, bm)
    return chunks, bm


# ---- 검색 -------------------------------------------------------------------

def search(deal_id: str, deal_dir: Path, minutes_dir: Path, query: str, meeting_from: int | None = None,
           meeting_to: int | None = None, kind: str | None = None, top_k: int = 5, window: int = WINDOW,
           latest_meeting: int | None = None, speakers: list[str] | None = None) -> list[dict]:
    chunks, bm = _index(deal_id, deal_dir, minutes_dir)
    cand = [c for c in chunks
            if (meeting_from is None or c["meeting"] >= meeting_from)
            and (meeting_to is None or c["meeting"] <= meeting_to)
            and (kind is None or c["kind"].startswith(kind))]
    allowed = {c["id"] for c in cand}
    rankings = [bm.rank(query, allowed)]
    if speakers:  # 질문에 인물이 있으면 그 사람의 발화만 따로 순위를 매겨 RRF로 합친다
        mine = {c["id"] for c in cand if c["speaker"] in speakers}
        rankings.append([i for i in rankings[0] if i in mine])
    if VECTOR_RETRIEVER is not None:
        rankings.append([i for i in VECTOR_RETRIEVER(query, cand) if i in allowed])
    by_id = {c["id"]: c for c in chunks}
    out = []
    for i in rrf(rankings)[:top_k]:
        c = dict(by_id[i])
        if c["kind"] == "발화" and window:
            same = [x for x in chunks if x["kind"] == "발화" and x["meeting"] == c["meeting"]
                    and abs(x["pos"] - c["pos"]) <= window and x["id"] != c["id"]]
            c["before"] = [f"{x['speaker']}: {x['text']}" for x in same if x["pos"] < c["pos"]]
            c["after"] = [f"{x['speaker']}: {x['text']}" for x in same if x["pos"] > c["pos"]]
        if latest_meeting and c["meeting"] < latest_meeting:
            c["freshness"] = f"{c['meeting']}차 기록. 이후 {latest_meeting}차까지 회의가 있었으므로 현재 유효한 사실은 딜 기억 기준으로 확인"
        out.append(c)
    return out


def format_results(results: list[dict], max_side: int = 160) -> str:
    if not results:
        return "검색 결과 없음. 다른 표현(고객이 쓴 말, 금액·날짜 숫자)으로 다시 찾거나 회차 범위를 넓힌다."
    parts = []
    for r in results:
        lines = [r["header"]]
        lines += [f"  (앞) {s[:max_side]}" for s in r.get("before", [])]
        lines.append(f"  ▶ {r['speaker'] + ': ' if r['speaker'] else ''}{r['text']}")
        lines += [f"  (뒤) {s[:max_side]}" for s in r.get("after", [])]
        if r.get("freshness"):
            lines.append(f"  ※ {r['freshness']}")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)
