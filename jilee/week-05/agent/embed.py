"""임베딩 검색기: 이력 검색(retrieval)의 두 번째 검색기. 뜻이 같은데 낱말이 다른 질문("얼마 드나" ↔ "도입 금액")을 잡는다.

흐름
  1. 조각(맥락 꼬리표 + 본문)과 질문에서 고객사명 · 인명 · 전화번호 · 메일 주소를 가린다(외부로 나가는 글이라 LLM과 같은 규칙)
  2. 임베딩 API로 벡터를 받는다. 조각은 RETRIEVAL_DOCUMENT, 질문은 RETRIEVAL_QUERY로 따로 넣는다(비대칭 검색)
  3. 벡터는 길이 1로 맞춰 .cache/embeddings/{프로젝트}.json에 둔다. 열쇠는 (모델 · 차원 · 가린 글)의 해시라 글이 바뀐 조각만 다시 받는다
  4. 코사인 유사도 순위를 돌려준다. 키워드 순위와는 retrieval.search가 RRF로 합친다

벡터 저장소는 따로 두지 않았다. 프로젝트 하나의 조각이 수백 개라 전수 비교(brute force)로 충분하다.
조각이 수만 개가 되면 3 · 4를 pgvector(HNSW)나 OpenSearch k-NN으로 옮긴다. 바꿀 곳은 _load / _save / rank 셋이다.
키가 없거나 호출이 실패하면 빈 순위를 돌려주고, 검색은 키워드만으로 계속된다.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import urllib.error
import urllib.request
from pathlib import Path

from . import masking
from .llm import load_env

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / ".cache" / "embeddings"
BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_MODEL = "gemini-embedding-001"
DEFAULT_DIM = 768   # 모델 기본은 3072. 줄여 받아(MRL) 파일 크기와 비교 시간을 줄인다
BATCH = 100         # 한 요청에 넣는 글 수(API 상한)
TIMEOUT = 30

last_error = ""     # 가장 최근 실패 사유(화면 · 실험에서 보여 줄 때 쓴다)
_mem: dict[str, dict[str, list[float]]] = {}


def _conf() -> tuple[str, str, int]:
    load_env()
    return (os.environ.get("EMBED_API_KEY", ""), os.environ.get("EMBED_MODEL") or DEFAULT_MODEL,
            int(os.environ.get("EMBED_DIM") or DEFAULT_DIM))


def enabled() -> bool:
    return bool(_conf()[0])


def _unit(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [round(x / n, 5) for x in v]


def _call(texts: list[str], task: str) -> list[list[float]]:
    key, model, dim = _conf()
    out: list[list[float]] = []
    for i in range(0, len(texts), BATCH):
        body = {"requests": [{"model": f"models/{model}", "content": {"parts": [{"text": t}]},
                              "taskType": task, "outputDimensionality": dim} for t in texts[i:i + BATCH]]}
        req = urllib.request.Request(f"{BASE_URL}/models/{model}:batchEmbedContents", data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json", "x-goog-api-key": key}, method="POST")
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            out += [_unit(e["values"]) for e in json.loads(r.read().decode("utf-8"))["embeddings"]]
    return out


def _key(text: str) -> str:
    _, model, dim = _conf()
    return hashlib.sha1(f"{model}|{dim}|{text}".encode("utf-8")).hexdigest()


def _path(name: str) -> Path:
    return CACHE_DIR / (hashlib.sha1(name.encode("utf-8")).hexdigest()[:8] + "_" + "".join(c for c in name if c.isalnum() or c in "-_") + ".json")


def _load(name: str) -> dict[str, list[float]]:
    if name not in _mem:
        p = _path(name)
        _mem[name] = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    return _mem[name]


def _save(name: str) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _path(name).write_text(json.dumps(_mem[name], separators=(",", ":")), encoding="utf-8")


def vectors(name: str, texts: list[str]) -> list[list[float]]:
    """글마다 벡터. 받아 둔 것은 다시 받지 않는다."""
    store, keys = _load(name), [_key(t) for t in texts]
    todo = {k: t for k, t in zip(keys, texts) if k not in store}
    if todo:
        store.update(zip(todo, _call(list(todo.values()), "RETRIEVAL_DOCUMENT")))
        _save(name)
    return [store[k] for k in keys]


def masker_for(chunks: list[dict]) -> masking.Masker:
    """조각이 속한 고객의 회사명과 담당자 · 우리 조직 · 화자 이름으로 만든 사전."""
    company, names = "", {c["speaker"] for c in chunks if c.get("speaker")}
    try:
        from . import store
        cust = store.load_customer(chunks[0]["deal"].split("-")[0])
        company = cust.get("name", "")
        names |= {c["name"] for c in cust.get("contacts", [])} | {m["name"] for m in store.load_org().get("members", [])}
    except Exception:  # 실험처럼 고객 정보 없이 조각만 넘어올 때는 화자 이름만 가린다
        pass
    return masking.Masker(*masking.build_dictionary(company, [], sorted(names)))


def rank(query: str, chunks: list[dict]) -> list[str]:
    """질문과 가까운 순서의 조각 id. retrieval.VECTOR_RETRIEVER와 같은 모양이다."""
    global last_error
    if not chunks or not enabled():
        return []
    try:
        m = masker_for(chunks)
        docs = vectors(chunks[0]["deal"], [m.mask(c["header"] + " " + c["text"]) for c in chunks])
        q = _call([m.mask(query)], "RETRIEVAL_QUERY")[0]
    except (urllib.error.URLError, TimeoutError, KeyError, ValueError, OSError) as e:
        detail = e.read().decode("utf-8", "replace")[:200] if isinstance(e, urllib.error.HTTPError) else str(e)
        last_error = f"{type(e).__name__}: {detail}"
        return []
    last_error = ""
    scored = sorted(((sum(a * b for a, b in zip(q, d)), c["id"]) for c, d in zip(chunks, docs)), reverse=True)
    return [i for _, i in scored]
