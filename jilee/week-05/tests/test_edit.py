"""사람이 회의록 초안을 고치는 경로를 검증한다: 초안일 때만, 형식은 지켜야, 결과지 제안은 그대로, 원문은 한 번만 남는다."""

import json
import shutil
import unittest

from agent import edit, loop as agent_mod, tools
from tests.helpers import CID, PID, SHEET_UPDATE, FakeLLM, ids, make_project, minutes


class EditMinutesTest(unittest.TestCase):
    def setUp(self):
        self.tmp, self.patches = make_project()
        for p in self.patches:
            p.start()
        self.project = self.tmp / "customers" / CID / "projects" / PID
        agent_mod.run_agent(CID, PID, 1, llm=FakeLLM([               # 에이전트가 초안을 만든 상태
            ("update_result_sheet", ids(meeting_no=1, **SHEET_UPDATE)),
            ("save_minutes", ids(meeting_no=1, markdown=minutes()))]))

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def draft(self):
        return json.loads((self.project / "state" / "draft_01.json").read_text(encoding="utf-8"))

    def test_edit_rebuilds_memory_but_keeps_sheet_proposal(self):
        before = self.draft()
        edited = minutes(action_due="10월 9일").replace("견적서 제공", "수정 견적서 제공").replace("1차 합의", "두 품목부터 시작")
        r = edit.save_minutes_edit(CID, PID, 1, edited, "이정인")
        self.assertEqual((r["ok"], r["warnings"], r["edited_by"]), (True, [], "이정인"))
        after = self.draft()
        self.assertEqual((after["action_items"][0]["task"], after["action_items"][0]["due"]), ("수정 견적서 제공", "10월 9일"))
        self.assertEqual(after["decisions"][0]["text"], "두 품목부터 시작")
        self.assertEqual(after["sheet"], before["sheet"])                     # 결과지 변경 제안은 그대로
        self.assertIn("수정 견적서 제공", (self.project / "minutes_01.md").read_text(encoding="utf-8"))
        m = tools.meeting_view(CID, PID, 1)
        self.assertEqual((m["minutes"]["status"], m["minutes"]["edited_by"], m["gate"]["state"]), ("초안", "이정인", "회의록 검토"))
        self.assertTrue(m["changes"])                                          # 이 회의의 결과지 변경 목록도 그대로

    def test_agent_original_is_kept_once_and_diff_shows_what_changed(self):
        self.assertIsNone(edit.diff_from_agent(CID, PID, 1))                  # 고친 적이 없으면 견줄 것이 없다
        edit.save_minutes_edit(CID, PID, 1, minutes().replace("견적서 제공", "수정 견적서 제공"))
        edit.save_minutes_edit(CID, PID, 1, minutes().replace("견적서 제공", "최종 견적서 제공"))
        original = (self.project / "state" / "minutes_01.agent.md").read_text(encoding="utf-8")
        self.assertIn("| 견적서 제공 |", original)                             # 두 번 고쳐도 에이전트가 쓴 원문이 남는다
        d = edit.diff_from_agent(CID, PID, 1)
        self.assertEqual((d["added"], d["removed"]), (1, 1))
        self.assertTrue(any(l.startswith("+") and "최종 견적서 제공" in l for l in d["lines"]))
        self.assertTrue(any(l.startswith("-") and "| 견적서 제공 |" in l for l in d["lines"]))

    def test_format_violation_is_not_saved(self):
        before = (self.project / "minutes_01.md").read_text(encoding="utf-8")
        r = edit.save_minutes_edit(CID, PID, 1, minutes(action_due="확인 불가"))
        self.assertFalse(r["ok"])
        self.assertTrue(any("7절" in e and "기한" in e for e in r["errors"]))
        self.assertEqual((self.project / "minutes_01.md").read_text(encoding="utf-8"), before)
        self.assertFalse((self.project / "state" / "minutes_01.agent.md").exists())
        self.assertEqual(self.draft()["action_items"][0]["due"], "10월 8일")
        missing = edit.save_minutes_edit(CID, PID, 1, minutes().replace("## 5. 합의 사항", "## 합의"))
        self.assertTrue(any("5. 합의 사항" in e for e in missing["errors"]))

    def test_unknown_name_is_only_a_warning(self):
        # 녹취에 잘못 들린 이름을 사람이 바로잡는 경우가 있으므로 이름 검사는 막지 않는다
        r = edit.save_minutes_edit(CID, PID, 1, minutes(owner="홍길동(당사)"))
        self.assertTrue(r["ok"])
        self.assertTrue(any("홍길동" in w for w in r["warnings"]))
        self.assertEqual(self.draft()["action_items"][0]["owner"], "홍길동(당사)")

    def test_reflected_minutes_can_still_be_edited(self):
        tools.confirm_minutes(CID, PID, 1, "자동 반영")                        # 확정 단계는 없다. 반영된 뒤에도 고친다
        r = edit.save_minutes_edit(CID, PID, 1, minutes().replace("견적서 제공", "수정 견적서 제공"), "이정인")
        self.assertTrue(r["ok"])
        st = json.loads((self.project / "state" / "state_01.json").read_text(encoding="utf-8"))
        self.assertEqual(st["action_items"][0]["task"], "수정 견적서 제공")    # 그 회차의 기억이 고친 회의록으로 다시 만들어진다
        self.assertFalse((self.project / "state" / "draft_01.json").exists())
        m = tools.meeting_view(CID, PID, 1)
        self.assertEqual((m["minutes"]["status"], m["minutes"]["edited_by"]), ("확정", "이정인"))
        with self.assertRaises(FileNotFoundError):
            edit.save_minutes_edit(CID, PID, 2, minutes(2))                   # 회의록이 없는 회차

    def test_sheet_values_are_edited_by_a_person(self):
        tools.confirm_minutes(CID, PID, 1, "자동 반영")
        sh = lambda: json.loads((self.project / "state" / "state_01.json").read_text(encoding="utf-8"))["sheet"]
        tools.edit_sheet(CID, PID, "item", "B1", {"value": "내년 예산에 반영 예정"}, "이정인")
        tools.edit_sheet(CID, PID, "summary", "", {"value": "고친 요약"}, "이정인")
        tools.edit_sheet(CID, PID, "questions", "", {"value": ["예산 확정일", " "]}, "이정인")
        now = sh()
        self.assertEqual((now["items"]["B1"]["value"], now["items"]["B1"]["status"], now["items"]["B1"]["history"][-1]["action"]),
                         ("내년 예산에 반영 예정", "확인", "수정"))
        self.assertEqual((now["summary"], now["next_questions"]), ("고친 요약", ["예산 확정일"]))
        with self.assertRaises(ValueError):
            tools.edit_sheet(CID, PID, "item", "B1", {"value": " "}, "이정인")
        self.assertIn("B1", tools.read_project_context(CID, PID, 2))          # 다음 회의의 에이전트가 고친 결과지를 읽는다

    def test_go_decision_is_stacked_and_needs_a_reason(self):
        from agent import store
        with self.assertRaisesRegex(ValueError, "사유"):
            store.add_decision(CID, PID, "보류", " ", "이정인")
        store.add_decision(CID, PID, "보류", "예산 미확정", "이정인")
        rows = store.add_decision(CID, PID, "전환", "", "이정인")
        self.assertEqual([(r["round"], r["result"]) for r in rows], [(1, "보류"), (2, "전환")])


if __name__ == "__main__":
    unittest.main()
