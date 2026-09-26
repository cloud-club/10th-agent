"""Week 4: 딜 이력 검색(BM25·RRF·필터·앞뒤 발화)과 관계 그래프, 실행 기록을 LLM 없이 검증한다."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent import graph, loop as agent_mod, memory, retrieval, tools

T1 = """[2026-10-01 10:00 / 방문]
참석: (고객) 정수연 품질팀장 / (당사) 김도현
김도현: 오늘은 결제 조건을 정리하겠습니다.
정수연: 저희 회사는 선금은 안 주고 검수 후에 일시불이에요.
김도현: 그럼 선금 이십 퍼센트로 낮추면 어떨까요.
정수연: 그건 구매팀 확인이 필요해요.
김도현: 네 수정 제안서는 수요일까지 드리겠습니다."""
T2 = """[2026-10-08 10:00 / 화상]
참석: (고객) 정수연 품질팀장 / (당사) 김도현
정수연: 검사 속도 문서 잘 받았습니다.
김도현: 조명은 돔 조명으로 확정하겠습니다."""


class RetrievalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "transcript_01.txt").write_text(T1, encoding="utf-8")
        (self.tmp / "transcript_02.txt").write_text(T2, encoding="utf-8")
        retrieval._cache.clear()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def search(self, q, **kw):
        return retrieval.search("T1", self.tmp, self.tmp, q, **kw)

    def test_chunks_carry_context_header(self):
        c = retrieval.build_chunks("T1", self.tmp, self.tmp)[0]
        self.assertEqual(c["header"], "[T1 1차 · 2026-10-01 · 발화 · 김도현]")

    def test_bigram_matches_despite_particles(self):
        # '선금은'·'선금'처럼 조사가 달라도 2글자 조각 '선금'으로 찾는다
        self.assertIn("선금", self.search("선금이")[0]["text"])

    def test_meeting_filter(self):
        self.assertTrue(all(r["meeting"] == 2 for r in self.search("조명 속도", meeting_from=2)))
        self.assertTrue(all(r["meeting"] == 1 for r in self.search("조명 결제", meeting_to=1)))

    def test_window_and_freshness(self):
        r = self.search("수정 제안서 수요일", latest_meeting=2)[0]
        self.assertIn("구매팀", r["before"][-1])
        self.assertIn("딜 기억 기준", r["freshness"])
        self.assertIn("(앞)", retrieval.format_results([r]))

    def test_rrf_prefers_items_ranked_high_in_both(self):
        fused = retrieval.rrf([["a", "b", "c"], ["c", "b", "a"], ["b"]])
        self.assertEqual(fused[0], "b")

    def test_vector_retriever_slot_is_fused(self):
        target = [c["id"] for c in retrieval.build_chunks("T1", self.tmp, self.tmp) if "돔" in c["text"]][0]
        with mock.patch.object(retrieval, "VECTOR_RETRIEVER", lambda q, cand: [target]):
            ids = [r["id"] for r in self.search("결제 방식", top_k=10)]
        self.assertIn(target, ids)  # 키워드로는 안 걸려도 벡터 검색 몫으로 들어온다

    def test_index_is_cached_until_files_change(self):
        self.search("선금")
        first = retrieval._cache["T1"][2]
        self.search("조명")
        self.assertIs(retrieval._cache["T1"][2], first)


class GraphTest(unittest.TestCase):
    def test_graph_links_people_actions_meetings(self):
        from tests.test_memory import minutes
        st = memory.update_state(memory.empty_state("T1"), minutes(1), 1)
        g = graph.build_graph(st, "테스트상사(주)")
        rels = {(e["src"], e["rel"], e["dst"]) for e in g["edges"]}
        self.assertIn(("person:정수연", "참석", "meeting:1"), rels)
        self.assertIn(("action:A01-1", "담당", "person:김도현"), rels)
        self.assertIn(("q:Q01", "확보", "meeting:1"), rels)
        view = graph.person_view(g, "김도현")
        self.assertIn("견적서 제공", view)
        self.assertIn("1차", view)


class RunLogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        d = self.tmp / "deals" / "T1"
        d.mkdir(parents=True)
        (d / "customer.md").write_text("# 고객정보 — 테스트상사(주)\n- 고객 접점(최초): 정수연 품질팀장", encoding="utf-8")
        (d / "transcript_01.txt").write_text(T1, encoding="utf-8")
        self.patches = [mock.patch.object(tools, "DEALS_DIR", self.tmp / "deals"),
                        mock.patch.object(tools, "OUTPUT_DIR", self.tmp / "out"),
                        mock.patch.object(agent_mod, "RUNS_DIR", self.tmp / "runs")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_run_log_is_saved_masked(self):
        from tests.test_agent import FakeLLM
        r = agent_mod.run_agent("T1", 1, llm=FakeLLM([("read_transcript", {"deal_id": "T1", "meeting_no": 1})]))
        log = json.loads(Path(r.run_log).read_text(encoding="utf-8"))
        self.assertEqual(log["deal_id"], "T1")
        dump = json.dumps(log["messages"], ensure_ascii=False)
        self.assertIn("선금", dump)          # 녹취 내용은 남고
        self.assertNotIn("정수연", dump)     # 실명은 자리표시자로
        self.assertIn("[인물", dump)


if __name__ == "__main__":
    unittest.main()
