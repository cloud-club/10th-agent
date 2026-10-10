"""전역 채팅: 읽기 전용 도구만 쓰는지, 문맥·상한·오류 처리·마스킹이 지켜지는지를 LLM 없이 검증한다."""

import json
import os
import shutil
import unittest
from unittest import mock

from agent import chat, memory, store
from tests.helpers import CID, PID, ids, make_project, minutes


class ScriptedLLM:
    """정해진 순서대로 tool_calls를 내고, 다 쓰거나 도구가 없으면 reply를 낸다. 받은 메시지를 모두 적어 둔다."""

    def __init__(self, plan, reply="답입니다"):
        self.plan, self.reply = list(plan), reply
        self.seen, self.tools_given = [], []

    def chat(self, messages, tools=None):
        self.seen.append(json.dumps(messages, ensure_ascii=False))
        self.tools_given.append(tools)
        if tools is None or not self.plan:
            return {"role": "assistant", "content": self.reply}
        name, args = self.plan.pop(0)
        return {"role": "assistant", "content": "", "tool_calls": [
            {"id": f"c{len(self.seen)}", "type": "function",
             "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}]}


def ask(text="이 고객 예산 어떻게 됐지?"):
    return [{"role": "user", "content": text}]


class ChatTest(unittest.TestCase):
    def setUp(self):
        self.tmp, self.patches = make_project()
        self.patches.append(mock.patch.dict(os.environ, {"MASKING": "on"}))
        for p in self.patches:
            p.start()
        self.project = self.tmp / "customers" / CID / "projects" / PID

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_reads_then_answers_with_sources(self):
        llm = ScriptedLLM([("read_project_context", ids())], reply="예산은 아직 미확보입니다.")
        steps = []
        r = chat.answer(ask(), CID, PID, llm=llm, on_step=steps.append)
        self.assertEqual(r["answer"], "예산은 아직 미확보입니다.")
        self.assertEqual([(t["name"], t["ok"]) for t in r["trace"]], [("read_project_context", True)])
        self.assertEqual(r["sources"], ["P1 프로젝트 기억"])
        self.assertEqual(steps, r["trace"])
        self.assertIn("결과지 현황", llm.seen[1])          # 도구 결과가 다음 호출에 들어간다
        self.assertIn("검사 자동화", llm.seen[0])          # 시스템 프롬프트에 지금 보고 있는 프로젝트

    def test_ids_are_filled_from_the_screen_when_the_model_omits_them(self):
        r = chat.answer(ask(), CID, PID, llm=ScriptedLLM([("read_project_context", {}), ("search_project_history", {"query": "클레임 비용"})]))
        self.assertEqual([t["ok"] for t in r["trace"]], [True, True])
        self.assertEqual(r["trace"][0]["args"], {"customer_id": CID, "project_id": PID})
        self.assertEqual(r["sources"], ["P1 프로젝트 기억", "이력 검색: 클레임 비용"])

    def test_chat_has_no_write_tools(self):
        names = {t["function"]["name"] for t in chat.CHAT_TOOLS}
        self.assertEqual(names, {"list_projects", "read_project_context", "search_project_history", "read_minutes", "consult_sop"})
        llm = ScriptedLLM([("save_minutes", ids(meeting_no=1, markdown=minutes())),
                           ("update_result_sheet", ids(meeting_no=1, summary="요약"))])
        r = chat.answer(ask("회의록 저장해줘"), CID, PID, llm=llm)
        self.assertEqual([t["ok"] for t in r["trace"]], [False, False])   # 모델이 불러도 실행되지 않는다
        self.assertIn("쓸 수 없는 도구", r["trace"][0]["output_preview"])
        self.assertFalse((self.project / "minutes_01.md").exists())
        self.assertFalse((self.project / "state").exists())
        self.assertEqual(r["sources"], [])
        self.assertIn("회의록 확정", llm.seen[0])                          # 대신 바꾸는 방법을 안내하도록 일러둔다

    def test_without_context_it_finds_projects_by_listing(self):
        llm = ScriptedLLM([("list_projects", {})])
        r = chat.answer(ask("지금 처리할 회의가 뭐가 있지?"), llm=llm)
        self.assertIn("지정되지 않았다", llm.seen[0])
        self.assertEqual(r["sources"], ["고객 · 프로젝트 목록"])
        listing = chat.list_projects()
        self.assertIn("프로젝트 P1 검사 자동화", listing)
        self.assertIn("1차 2026-10-01 방문 · 녹취 확보 — 할 일: 실행 대기", listing)
        self.assertIn("프로젝트 P2 MES 연동 · 단계 발굴 · 회의 0건", listing)

    def test_tool_error_goes_back_to_the_model(self):
        llm = ScriptedLLM([("read_minutes", ids(meeting_no=1))])
        r = chat.answer(ask("1차 회의록 보여줘"), CID, PID, llm=llm)
        self.assertFalse(r["trace"][0]["ok"])
        self.assertIn("1차 회의록이 없습니다", r["trace"][0]["output_preview"])
        self.assertIn("오류: 1차 회의록이 없습니다", llm.seen[1])
        self.assertEqual(r["answer"], "답입니다")

    def test_unconfirmed_minutes_are_marked_as_draft(self):
        (self.project / "minutes_01.md").write_text(minutes(), encoding="utf-8")
        memory._write_json(memory.draft_path(self.project, 1), memory.empty_state(store.key(CID, PID)))
        self.assertTrue(chat.read_minutes(CID, PID, 1).startswith("(회의록 초안"))
        memory.draft_path(self.project, 1).unlink()
        self.assertTrue(chat.read_minutes(CID, PID, 1).startswith("# 회의록"))

    def test_step_cap_then_answer_without_tools(self):
        llm = ScriptedLLM([("list_projects", {})] * 20, reply="여기까지 확인했습니다.")
        r = chat.answer(ask(), CID, PID, llm=llm)
        self.assertEqual(len(r["trace"]), chat.MAX_STEPS)
        self.assertEqual(len(llm.seen), chat.MAX_STEPS + 1)
        self.assertIsNone(llm.tools_given[-1])                 # 마지막 호출은 도구 없이
        self.assertTrue(all(llm.tools_given[:-1]))
        self.assertEqual(r["answer"], "여기까지 확인했습니다.")
        self.assertEqual(r["sources"], ["고객 · 프로젝트 목록"])  # 같은 출처는 한 번만

    def test_only_recent_history_is_sent_and_empty_question_is_refused(self):
        talk = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"말 {i}"} for i in range(11)]
        llm = ScriptedLLM([])
        chat.answer(talk, CID, PID, llm=llm)
        sent = json.loads(llm.seen[0])
        self.assertEqual([m["role"] for m in sent], ["system"] + ["assistant", "user"] * 3)
        self.assertEqual(sent[-1]["content"], "말 10")
        for bad in ([], [{"role": "assistant", "content": "안녕하세요"}], [{"role": "user", "content": "  "}]):
            with self.assertRaises(ValueError):
                chat.answer(bad, CID, PID, llm=ScriptedLLM([]))

    def test_long_tool_output_is_cut(self):
        with mock.patch.object(chat, "list_projects", lambda: "가" * 9000):
            llm = ScriptedLLM([("list_projects", {})])
            chat.answer(ask(), llm=llm)
        tool_msg = [m for m in json.loads(llm.seen[1]) if m["role"] == "tool"][0]["content"]
        self.assertLess(len(tool_msg), chat.MAX_TOOL_CHARS + 100)
        self.assertIn("잘랐다", tool_msg)

    def test_names_are_masked_on_the_way_out_and_restored_in_the_answer(self):
        llm = ScriptedLLM([("list_projects", {}), ("read_project_context", ids())], reply="[인물1]님이 [고객사A] 건을 맡고 있습니다.")
        r = chat.answer(ask("테스트상사(주) 정수연 팀장 건은 김도현이 맡았나?"), CID, PID, llm=llm)
        out = "".join(llm.seen)
        for secret in ("정수연", "김도현", "한지은", "테스트상사"):
            self.assertNotIn(secret, out)
        self.assertIn("[고객사A]", out)
        self.assertIn("[인물", out)
        self.assertNotIn("[인물", r["answer"])                 # 돌아온 답은 실명으로 복원
        self.assertIn("테스트상사(주)", r["answer"])
        with mock.patch.dict(os.environ, {"MASKING": "off"}):
            plain = ScriptedLLM([])
            chat.answer(ask("정수연 팀장"), CID, PID, llm=plain)
            self.assertIn("정수연", plain.seen[0])

    def test_contact_details_never_reach_the_llm(self):
        store.upsert_contact(CID, {"name": "정수연", "phone": "010-9999-8888", "email": "jsy@test.co.kr"}, "시험")
        llm = ScriptedLLM([("list_projects", {}), ("read_project_context", ids()),
                           ("search_project_history", ids(query="구매팀 발주 번호"))])
        chat.answer(ask("한지은 대리 연락처 알려줘"), CID, PID, llm=llm)
        out = "".join(llm.seen)
        for secret in ("010-9999-8888", "jsy@test.co.kr", "kim@example.com"):  # 고객 담당자·우리 조직의 연락처는 문맥에 없다
            self.assertNotIn(secret, out)
        self.assertNotIn("010-1234-5678", out)                # 녹취에 섞여 나온 번호는 모양으로 가린다
        self.assertIn("[전화1]", out)

    def test_each_customer_gets_its_own_placeholder(self):
        other = self.tmp / "customers" / "T2"
        (other / "projects" / "P1").mkdir(parents=True)
        (other / "customer.json").write_text(json.dumps({"customer_id": "T2", "name": "다른회사(주)",
                                                         "contacts": [{"id": "CT001", "name": "박민서"}]}, ensure_ascii=False), encoding="utf-8")
        masker = chat.build_masker()
        self.assertEqual({masker.forward["테스트상사(주)"], masker.forward["다른회사(주)"]}, {"[고객사A]", "[고객사B]"})
        self.assertEqual(masker.forward["테스트상사"], masker.forward["테스트상사(주)"])   # 약칭은 같은 자리표시자
        text = masker.mask("테스트상사(주)와 다른회사 박민서 과장")
        self.assertNotIn("박민서", text)
        self.assertEqual(masker.unmask(text), "테스트상사(주)와 다른회사(주) 박민서 과장")  # 약칭은 정식 명칭으로 돌아온다


if __name__ == "__main__":
    unittest.main()
