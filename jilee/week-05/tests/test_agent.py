"""가짜 LLM으로 Agent Loop의 끝내기 조건·거부 후 재시도·도구 오류 처리를 검증한다. 실행: python -m unittest"""

import json
import shutil
import unittest

from agent import loop as agent_mod
from agent import tools
from tests.helpers import CID, PID, SHEET_UPDATE, FakeLLM, ids, make_project, minutes


class AgentLoopTest(unittest.TestCase):
    def setUp(self):
        self.tmp, self.patches = make_project()
        for p in self.patches:
            p.start()
        self.project = self.tmp / "customers" / CID / "projects" / PID

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def tool_names(self, r):
        return [t["name"] for t in r.trace if t["type"] == "tool"]

    def test_sheet_then_minutes_then_stop(self):
        llm = FakeLLM([
            ("read_project_context", ids(meeting_no=1)),
            ("read_transcript", ids(meeting_no=1)),
            ("update_result_sheet", ids(meeting_no=1, **SHEET_UPDATE)),
            ("save_minutes", ids(meeting_no=1, markdown=minutes())),
            ("save_minutes", ids(meeting_no=1, markdown="# 중복")),  # 정지조건이 막아야 함
        ])
        r = agent_mod.run_agent(CID, PID, 1, llm=llm)
        self.assertEqual(self.tool_names(r), ["read_project_context", "read_transcript", "update_result_sheet", "save_minutes"])
        self.assertEqual(r.final_answer, "보고: 저장 완료")
        self.assertEqual(llm.calls, 5)  # 도구 4회 + 보고 1회
        self.assertIn("저장 완료(초안)", r.saved_path)
        self.assertFalse((self.project / "state" / "pending_01.json").exists())

    def test_three_gates_meeting_minutes_sheet(self):
        gate = lambda n=1: tools.gate_of(CID, PID, n)["state"]
        self.assertEqual(gate(), "녹취 확보")                      # ① 회의: 녹취가 있다
        self.assertEqual(gate(2), "예정")
        agent_mod.run_agent(CID, PID, 1, llm=FakeLLM([
            ("update_result_sheet", ids(meeting_no=1, **SHEET_UPDATE)),
            ("save_minutes", ids(meeting_no=1, markdown=minutes()))]))
        self.assertEqual(gate(), "회의록 검토")
        self.assertFalse((self.project / "state" / "state_01.json").exists())     # 확정 전에는 기억에 들어가지 않는다
        self.assertIn("확정 0/10 · AI 초안 0", tools.read_project_context(CID, PID, 2))
        changes = tools.meeting_view(CID, PID, 1)["changes"]
        self.assertEqual({c["action"] for c in changes}, {"신규"})
        self.assertIn(("stakeholder", "최민호"), {(c["kind"], c["key"]) for c in changes})

        # 앞 회의의 회의록이 확정되지 않으면 다음 회의를 처리하지 않는다
        (self.project / "transcript_02.txt").write_text("[2026-10-08 10:00 / 화상]\n김도현: 안녕하세요", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "1차 회의록이 아직 확정되지 않았습니다"):
            agent_mod.run_agent(CID, PID, 2, llm=FakeLLM([]))

        g = tools.confirm_minutes(CID, PID, 1, "이정인")              # ② 회의록 확정 → 기억과 결과지에 반영
        self.assertEqual((g["state"], g["pending"]), ("결과지 확인", 6))  # 항목 2 · 목표 1 · 요구사항 1 · 사람 2
        snap = json.loads((self.project / "state" / "state_01.json").read_text(encoding="utf-8"))
        self.assertEqual(snap["sheet"]["items"]["S1"]["status"], "초안")   # 반영되어도 AI가 채운 것은 초안
        self.assertEqual(tools.meeting_view(CID, PID, 1)["minutes"]["confirmed_by"], "이정인")
        self.assertEqual(tools.confirm_sheet(CID, PID, "all", "", "이정인"), 6)   # ③ 결과지 확인
        self.assertEqual(gate(), "완료")
        rows = tools.all_meetings()
        self.assertEqual([(r["no"], r["gate"]["state"]) for r in rows], [(2, "녹취 확보"), (1, "완료")])  # 최근 것부터

    def test_minutes_before_sheet_is_rejected_then_recovered(self):
        llm = FakeLLM([
            ("save_minutes", ids(meeting_no=1, markdown=minutes())),            # 결과지 갱신 없이 저장 시도
            ("update_result_sheet", ids(meeting_no=1, **SHEET_UPDATE)),
            ("save_minutes", ids(meeting_no=1, markdown=minutes(action_due="확인 불가"))),  # 양식 규칙 위반
            ("save_minutes", ids(meeting_no=1, markdown=minutes())),
        ])
        r = agent_mod.run_agent(CID, PID, 1, llm=llm)
        saves = [e for e in r.trace if e.get("name") == "save_minutes"]
        self.assertEqual([e["ok"] for e in saves], [False, False, True])
        self.assertIn("결과지 갱신이 없다", saves[0]["output_preview"])
        self.assertIn("저장 거부", saves[1]["output_preview"])

    def test_ungrounded_evidence_is_rejected_and_nothing_is_applied(self):
        bad = dict(SHEET_UPDATE, items=[{"code": "B1", "value": "예산 3억 확보", "evidence": "예산은 삼억 원으로 이미 확정되어 있습니다"}])
        r = agent_mod.run_agent(CID, PID, 1, llm=FakeLLM([("update_result_sheet", ids(meeting_no=1, **bad))]))
        t = [e for e in r.trace if e["type"] == "tool"][0]
        self.assertFalse(t["ok"])
        self.assertIn("찾지 못했다", t["output_preview"])
        self.assertFalse((self.project / "state" / "pending_01.json").exists())

    def test_tool_error_is_returned_to_model_not_raised(self):
        r = agent_mod.run_agent(CID, PID, 1, llm=FakeLLM([("read_transcript", ids(meeting_no=9))]))
        t = [e for e in r.trace if e["type"] == "tool"][0]
        self.assertFalse(t["ok"])
        self.assertIn("녹취 파일이 없습니다", t["output_preview"])

    def test_max_steps_guard(self):
        r = agent_mod.run_agent(CID, PID, 1, llm=FakeLLM([("read_project_context", ids(meeting_no=1))] * 30))
        self.assertEqual(len(self.tool_names(r)), agent_mod.MAX_STEPS)
        self.assertIn("최대 단계", r.final_answer)

    def test_context_shows_other_projects_but_no_contact_details(self):
        ctx = tools.read_project_context(CID, PID, 1)
        self.assertIn("P2 MES 연동", ctx)            # 같은 고객의 다른 프로젝트
        self.assertIn("김도현(영업1팀 · 주 담당)", ctx)  # 우리 조직 정보에서 끌어온 담당
        self.assertNotIn("kim@example.com", ctx)       # 연락처는 외부 LLM으로 보내지 않는다


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


if __name__ == "__main__":
    unittest.main()
