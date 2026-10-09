"""가짜 LLM으로 Agent Loop의 순서·정지조건·도구 오류 처리를 검증한다. 실행: python -m unittest"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent import loop as agent_mod
from agent import tools


class FakeLLM:
    """정해진 순서대로 tool_calls를 내고, 도구가 없으면 요약 문장을 낸다."""

    def __init__(self, plan):
        self.plan = list(plan)
        self.calls = 0

    def chat(self, messages, tools=None):
        self.calls += 1
        if tools is None or not self.plan:
            return {"role": "assistant", "content": "요약: 저장 완료"}
        name, args = self.plan.pop(0)
        return {"role": "assistant", "content": "", "tool_calls": [
            {"id": f"c{self.calls}", "type": "function",
             "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}]}


class AgentLoopTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "deals" / "T1").mkdir(parents=True)
        (self.tmp / "deals" / "T1" / "customer.md").write_text("# 고객 T1", encoding="utf-8")
        (self.tmp / "deals" / "T1" / "transcript_01.txt").write_text("김영업: 안녕하세요", encoding="utf-8")
        self.patches = [
            mock.patch.object(tools, "DEALS_DIR", self.tmp / "deals"),
            mock.patch.object(tools, "OUTPUT_DIR", self.tmp / "out"),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_normal_path_saves_and_stops(self):
        llm = FakeLLM([
            ("read_deal_context", {"deal_id": "T1"}),
            ("read_transcript", {"deal_id": "T1", "meeting_no": 1}),
            ("save_minutes", {"deal_id": "T1", "meeting_no": 1, "markdown": "# 회의록"}),
            ("save_minutes", {"deal_id": "T1", "meeting_no": 1, "markdown": "# 중복"}),  # 정지조건이 막아야 함
        ])
        r = agent_mod.run_agent("T1", 1, llm=llm)
        names = [t["name"] for t in r.trace if t["type"] == "tool"]
        self.assertEqual(names, ["read_deal_context", "read_transcript", "save_minutes"])
        self.assertEqual(r.final_answer, "요약: 저장 완료")
        self.assertEqual((self.tmp / "out" / "T1" / "minutes_01.md").read_text(encoding="utf-8"), "# 회의록\n")
        self.assertEqual(llm.calls, 4)  # 도구 3회 + 요약 1회

    def test_previous_minutes_are_carried_into_context(self):
        (self.tmp / "out" / "T1").mkdir(parents=True)
        (self.tmp / "out" / "T1" / "minutes_01.md").write_text("액션아이템 A", encoding="utf-8")
        self.assertIn("액션아이템 A", tools.read_deal_context("T1"))

    def test_tool_error_is_returned_to_model_not_raised(self):
        llm = FakeLLM([("read_transcript", {"deal_id": "T1", "meeting_no": 9})])
        r = agent_mod.run_agent("T1", 1, llm=llm)
        self.assertFalse(r.trace[0]["ok"])
        self.assertIn("녹취 파일이 없습니다", r.trace[0]["output_preview"])

    def test_max_steps_guard(self):
        llm = FakeLLM([("read_deal_context", {"deal_id": "T1"})] * 20)
        r = agent_mod.run_agent("T1", 1, llm=llm)
        self.assertEqual(len(r.trace), agent_mod.MAX_STEPS)
        self.assertIn("최대 단계", r.final_answer)


if __name__ == "__main__":
    unittest.main()


class SopSearchTest(unittest.TestCase):
    """SOP 검색은 LLM 없이 결정론적으로 동작해야 한다."""

    def test_budget_question_hits_bant_or_budget_sections(self):
        from agent.sop_agent import search_sop
        hits = search_sop("예산 확인 시 무엇을 기록해야 하나")
        self.assertTrue(hits)
        self.assertTrue(any("예산" in h["title"] or "BANT" in h["title"] for h in hits))
        self.assertLessEqual(sum(len(h["text"]) for h in hits), 2500)

    def test_unknown_terms_return_empty(self):
        from agent.sop_agent import search_sop
        self.assertEqual(search_sop("zzqqxx"), [])
