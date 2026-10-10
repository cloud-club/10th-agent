"""외부/내부 회의 구분, 실행 옵션(에이전트 제어), 액션아이템 수행 기록을 LLM 없이 검증한다."""

import json
import shutil
import unittest
from pathlib import Path

from agent import loop as agent_mod
from agent import store, tools
from tests.helpers import CID, PID, SHEET_UPDATE, FakeLLM, ids, make_project, minutes

INTERNAL = """[2026-10-05 16:00 / 내부 / 본사 회의실]
참석: (당사) 김도현, 박성민
김도현: 고객이 말한 외관 검사 자동화는 두 품목부터 장비를 넣는 걸로 대응합시다.
박성민: 네 검사 자동화 장비 사양은 제가 금요일까지 정리하겠습니다.
김도현: 다음 회의에서는 예산이 확보됐는지부터 물어봐야겠어요."""


class ScopeTest(unittest.TestCase):
    def setUp(self):
        self.tmp, self.patches = make_project()
        for p in self.patches:
            p.start()
        self.project = self.tmp / "customers" / CID / "projects" / PID

    def tearDown(self):
        tools.bind_options(None)
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_scope_is_read_from_attendees_and_can_be_set(self):
        self.assertEqual(store.load_meeting(CID, PID, 1)["scope"], "외부")   # 참석 줄에 (고객)이 있다
        (self.project / "transcript_02.txt").write_text(INTERNAL, encoding="utf-8")
        self.assertEqual(store.load_meeting(CID, PID, 2)["scope"], "내부")   # (당사)뿐이다
        self.assertEqual(store.set_meeting_scope(CID, PID, 2, "외부")["scope"], "외부")
        with self.assertRaises(ValueError):
            store.set_meeting_scope(CID, PID, 2, "사내")

    def test_internal_meeting_cannot_add_customer_facts(self):
        (self.project / "transcript_01.txt").write_text(INTERNAL, encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "내부 회의다"):
            tools.update_result_sheet(CID, PID, 1, **SHEET_UPDATE)
        ours = {"requirements": [{"title": "외관 검사 자동화", "response": "두 품목부터 장비 적용", "response_status": "검토 중", "evidence": "#1"}],
                "next_questions": ["예산이 확보됐는지"]}
        out = tools.update_result_sheet(CID, PID, 1, **ours)              # 우리가 정한 것은 반영된다(요약은 비워도 된다)
        self.assertIn("결과지 갱신 완료", out)
        pending = json.loads((self.project / "state" / "pending_01.json").read_text(encoding="utf-8"))
        self.assertEqual((pending["items"], pending["requirements"][0]["response"]), ({}, "두 품목부터 장비 적용"))

    def test_options_shape_the_prompt_and_the_tools(self):
        opts = agent_mod.normalize_options({"length": "간결", "update_sheet": False, "use_sop": False, "scope": "사내", "bogus": 1, "note": " 하자보수 조건을 꼼꼼히 "})
        self.assertEqual((opts["scope"], opts["note"], "bogus" in opts), ("", "하자보수 조건을 꼼꼼히", False))
        names = [t["function"]["name"] for t in agent_mod.active_tools(opts)]
        self.assertNotIn("update_result_sheet", names)
        self.assertNotIn("consult_sop", names)
        self.assertIn("search_project_history", names)
        prompt = agent_mod.build_system_prompt(opts, "내부")
        self.assertIn("결과지는 건드리지 않는다", prompt)
        self.assertIn("내부 회의다", prompt)
        self.assertIn("한 쪽 안에", prompt)
        self.assertNotIn("내부 회의다", agent_mod.build_system_prompt(None, "외부"))

    def test_minutes_only_run_does_not_need_a_sheet_update(self):
        r = agent_mod.run_agent(CID, PID, 1, llm=FakeLLM([("save_minutes", ids(meeting_no=1, markdown=minutes()))]),
                                options={"update_sheet": False, "note": "회의록만"})
        self.assertTrue(r.saved_path)                                       # 결과지 갱신 없이도 저장된다
        self.assertEqual(tools.meeting_view(CID, PID, 1)["changes"], [])
        log = json.loads(Path(r.run_log).read_text(encoding="utf-8"))
        self.assertEqual((log["options"]["update_sheet"], log["options"]["note"]), (False, "회의록만"))

    def test_action_log_is_kept_by_a_person(self):
        agent_mod.run_agent(CID, PID, 1, llm=FakeLLM([("update_result_sheet", ids(meeting_no=1, **SHEET_UPDATE)),
                                                      ("save_minutes", ids(meeting_no=1, markdown=minutes()))]))
        tools.confirm_minutes(CID, PID, 1, "이정인")
        item = tools.set_action(CID, PID, "A01-1", "완료", "이정인", "메일로 보냄")
        self.assertEqual((item["status"], item["log"][0]["by"], item["log"][0]["note"]), ("완료", "이정인", "메일로 보냄"))
        self.assertNotIn("견적서 제공", tools.read_project_context(CID, PID, 2).split("점검할 액션아이템")[1].split("###")[0])  # 끝난 일은 다음 회의의 점검 목록에서 빠진다
        with self.assertRaises(ValueError):
            tools.set_action(CID, PID, "A01-1", "보류")
        with self.assertRaises(FileNotFoundError):
            tools.set_action(CID, PID, "A99-9", "완료")


    def test_registration_takes_people_agenda_and_links(self):
        n = store.register_meeting(CID, PID, "2026-11-05", "14:00", "화상", "Meet", "외부",
                                   attendees=["김도현", " 정수연 ", "김도현", ""], agenda="하자보수 조건 확정", links=["https://example.com/quote"])
        m = store.load_meeting(CID, PID, n)
        self.assertEqual((m["attendees"], m["agenda"], m["links"]), (["김도현", "정수연"], "하자보수 조건 확정", ["https://example.com/quote"]))
        ctx = tools.read_project_context(CID, PID, n)
        self.assertIn(f"{n}차 회의 사전 정보", ctx)                              # 에이전트가 등록할 때 적은 안건을 읽는다
        self.assertIn("하자보수 조건 확정", ctx)
        with self.assertRaises(ValueError):
            store.register_meeting(CID, PID, "2026-11-06", links=["ftp://x"])

    def test_agenda_is_drafted_from_the_last_minutes(self):
        agent_mod.run_agent(CID, PID, 1, llm=FakeLLM([("update_result_sheet", ids(meeting_no=1, **SHEET_UPDATE)),
                                                      ("save_minutes", ids(meeting_no=1, markdown=minutes()))]))
        tools.confirm_minutes(CID, PID, 1, "자동 반영")
        a = tools.suggest_agenda(CID, PID)
        self.assertEqual(a["from_meeting"], 1)
        self.assertIn("[진행 예정 사항 점검]", a["text"])
        self.assertIn("견적서 제공", a["text"])                                # 끝나지 않은 일이 다음 회의의 안건이 된다
        tools.set_action(CID, PID, "A01-1", "완료", "이정인")
        self.assertNotIn("견적서 제공", tools.suggest_agenda(CID, PID)["text"])

if __name__ == "__main__":
    unittest.main()
