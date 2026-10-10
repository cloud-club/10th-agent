"""임베딩 검색기: API를 부르지 않고(가짜 _call) 캐시 · 마스킹 · 실패 시 동작 · 순위 결합을 본다."""
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from agent import embed, retrieval

CHUNKS = [
    {"id": "a", "deal": "T0001-P001", "meeting": 1, "kind": "발화", "speaker": "정수연", "pos": 1, "header": "[1차 · 정수연]", "text": "도입 금액은 일억 원 안쪽입니다 010-1234-5678"},
    {"id": "b", "deal": "T0001-P001", "meeting": 1, "kind": "발화", "speaker": "김도현", "pos": 2, "header": "[1차 · 김도현]", "text": "검사 속도는 초당 두 개입니다"},
]


def fake_call(sent):
    """'금액'이 든 글은 [1, 0], 아니면 [0, 1]. 보낸 글을 sent에 모은다."""
    def call(texts, task):
        sent.append((task, list(texts)))
        return [[1.0, 0.0] if ("금액" in t or "얼마" in t) else [0.0, 1.0] for t in texts]
    return call


class EmbedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for p in (mock.patch.object(embed, "CACHE_DIR", Path(self.tmp.name)), mock.patch.dict(embed._mem, {}, clear=True),
                  mock.patch.dict(os.environ, {"EMBED_API_KEY": "test-key"})):
            p.start()
            self.addCleanup(p.stop)

    def test_off_without_key(self):
        with mock.patch.dict(os.environ, {"EMBED_API_KEY": ""}):
            self.assertFalse(embed.enabled())
            self.assertEqual(embed.rank("얼마", CHUNKS), [])

    def test_rank_and_cache(self):
        sent = []
        with mock.patch.object(embed, "_call", fake_call(sent)):
            self.assertEqual(embed.rank("얼마 드나", CHUNKS), ["a", "b"])
            self.assertEqual(embed.rank("얼마 드나", CHUNKS), ["a", "b"])
        docs = [s for s in sent if s[0] == "RETRIEVAL_DOCUMENT"]
        self.assertEqual(len(docs), 1)                       # 조각은 한 번만 받는다
        self.assertEqual(len(list(Path(self.tmp.name).glob("*.json"))), 1)
        embed._mem.clear()                                   # 다시 켜도 파일에서 읽는다
        with mock.patch.object(embed, "_call", fake_call(sent)):
            embed.rank("얼마 드나", CHUNKS)
        self.assertEqual(len([s for s in sent if s[0] == "RETRIEVAL_DOCUMENT"]), 1)

    def test_masks_before_sending(self):
        sent = []
        with mock.patch.object(embed, "_call", fake_call(sent)):
            embed.rank("정수연 팀장이 말한 금액", CHUNKS)
        out = " ".join(t for _, texts in sent for t in texts)
        for secret in ("정수연", "김도현", "010-1234-5678"):
            self.assertNotIn(secret, out)
        self.assertIn("[인물", out)

    def test_failure_falls_back_to_keywords(self):
        def boom(texts, task):
            raise urllib.error.URLError("연결 실패")
        with mock.patch.object(embed, "_call", boom):
            self.assertEqual(embed.rank("얼마", CHUNKS), [])
            self.assertIn("URLError", embed.last_error)
            with tempfile.TemporaryDirectory() as d:
                Path(d, "transcript_01.txt").write_text("[2026-09-03] 참석: 정수연\n정수연: 도입 금액은 일억 원 안쪽입니다\n김도현: 검사 속도는 초당 두 개입니다\n", encoding="utf-8")
                got = retrieval.search("T0001-P009", Path(d), Path(d), "도입 금액")
        self.assertEqual(got[0]["speaker"], "정수연")          # 키워드만으로 답한다

    def test_search_merges_vector_ranking(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "transcript_01.txt").write_text("[2026-09-03] 참석: 정수연\n정수연: 도입 금액은 일억 원 안쪽입니다\n김도현: 검사 속도는 초당 두 개입니다\n", encoding="utf-8")
            with mock.patch.object(embed, "_call", fake_call([])):
                got = retrieval.search("T0001-P008", Path(d), Path(d), "얼마 드나")  # 겹치는 낱말이 없다
        self.assertEqual(got[0]["speaker"], "정수연")


if __name__ == "__main__":
    unittest.main()
