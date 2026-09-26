"""딜 이력 검색 평가: 질문 → 정답 발화가 몇 위에 나오는가. 실행: python experiments/search_eval/eval_search.py

지표
  Recall@5 : 정답이 상위 5개 안에 든 질문의 비율
  MRR      : 정답 순위의 역수 평균(1위=1.0, 2위=0.5 …, 10위 밖=0)

비교
  word     : 띄어쓰기 단어 그대로 색인(조사가 붙으면 다른 단어가 됨)
  bigram   : 한글을 2글자 조각으로 잘라 색인(현재 방식)
  bigram+RRF(word) : 두 색인의 순위를 RRF로 합침 — 벡터 검색을 붙일 때와 같은 결합 경로
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from agent import retrieval  # noqa: E402

DEAL = ROOT / "deals" / "D001"
QUESTIONS = json.loads((Path(__file__).parent / "questions.json").read_text(encoding="utf-8"))


def word_tokenize(text: str) -> list[str]:
    return [w.lower() for w in re.findall(r"[가-힣]+|[A-Za-z]+|\d+", text)]


def evaluate(rank_fn) -> dict:
    hits5, rr, misses = 0, 0.0, []
    for item in QUESTIONS:
        ranked = rank_fn(item["q"])[:10]
        pos = next((i for i, c in enumerate(ranked, start=1)
                    if c["meeting"] == item["meeting"] and item["contains"] in c["text"]), None)
        hits5 += 1 if pos and pos <= 5 else 0
        rr += 1 / pos if pos else 0
        if not pos or pos > 5:
            misses.append(f"{item['q']} → {pos or '10위 밖'}")
    n = len(QUESTIONS)
    return {"Recall@5": round(hits5 / n, 3), "MRR": round(rr / n, 3), "놓친 질문": misses}


def main() -> None:
    chunks = [c for c in retrieval.build_chunks("D001", DEAL, DEAL) if c["kind"] == "발화"]
    by_id = {c["id"]: c for c in chunks}
    docs = {c["id"]: c["header"] + " " + c["text"] for c in chunks}
    bigram = retrieval.BM25(docs)
    orig = retrieval.tokenize
    retrieval.tokenize = word_tokenize
    word = retrieval.BM25(docs)
    retrieval.tokenize = orig

    def ranker(index, tok):
        def run(q):
            retrieval.tokenize = tok
            try:
                return index.rank(q)
            finally:
                retrieval.tokenize = orig
        return run

    rank_word, rank_bigram = ranker(word, word_tokenize), ranker(bigram, orig)
    variants = {
        "word": lambda q: [by_id[i] for i in rank_word(q)],
        "bigram": lambda q: [by_id[i] for i in rank_bigram(q)],
        "bigram+RRF(word)": lambda q: [by_id[i] for i in retrieval.rrf([rank_bigram(q), rank_word(q)])],
    }
    print(f"발화 조각 {len(chunks)}개 · 질문 {len(QUESTIONS)}개")
    results = {}
    for name, fn in variants.items():
        r = evaluate(fn)
        results[name] = r
        print(f"\n[{name}] Recall@5 {r['Recall@5']:.3f} · MRR {r['MRR']:.3f}")
        for m in r["놓친 질문"]:
            print("  놓침:", m)
    (Path(__file__).parent / "result.json").write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
