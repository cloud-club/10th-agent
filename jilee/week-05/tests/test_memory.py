"""프로젝트 기억(상태 파일)·회의록 저장 전 규칙 검사·마스킹·긴 녹취 분할을 LLM 없이 검증한다."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from agent import memory, tools
from agent.masking import MaskedLLM, Masker, build_dictionary
from tests.helpers import TRANSCRIPT, minutes

KNOWN = "정수연 김도현"


class ValidateTest(unittest.TestCase):
    def test_good_minutes_pass(self):
        self.assertEqual(memory.validate(minutes(), KNOWN), [])

    def test_action_without_due_is_rejected(self):
        errs = memory.validate(minutes(action_due="확인 불가"), KNOWN)
        self.assertTrue(any("7절" in e and "기한" in e for e in errs))

    def test_unknown_person_is_rejected(self):
        errs = memory.validate(minutes(owner="홍길동(당사)"), KNOWN)
        self.assertTrue(any("홍길동" in e for e in errs))

    def test_flat_discussion_list_is_rejected(self):
        # 논의 요지를 한 줄 목록으로 늘어놓으면 읽는 사람이 요지를 못 잡는다. 주제로 묶어야 통과한다
        flat = minutes().split("## 3. 논의 요지")[0] + "## 3. 논의 요지\n" + "\n".join(f"- 논의 {i}" for i in range(6)) + "\n\n## 4." + minutes().split("## 4.")[1]
        self.assertTrue(any("3절" in e and "주제" in e for e in memory.validate(flat, KNOWN)))
        self.assertEqual(memory.validate(minutes(), KNOWN), [])

    def test_missing_section(self):
        md = minutes().replace("## 5. 합의 사항", "## 합의")
        self.assertTrue(any("5. 합의 사항" in e for e in memory.validate(md, KNOWN)))


class StateTest(unittest.TestCase):
    def test_state_accumulates_across_meetings(self):
        st = memory.update_state(memory.empty_state("T1-P1"), minutes(1), 1)
        self.assertEqual(st["action_items"][0]["status"], "미착수")
        st2 = memory.update_state(st, minutes(2, review="| 견적서 제공 | 김도현 | 10월 8일 | 완료 (메일 발송) |"), 2)
        self.assertEqual(st2["action_items"][0]["status"], "완료")  # 2절 점검 결과 반영
        self.assertEqual([d["meeting"] for d in st2["decisions"]], [1, 2])  # 합의 누적
        self.assertEqual(memory.kpi(st2)["이행률"], "1/1 (100%)")
        rendered = memory.render_state(st2)
        self.assertIn("(1차) 1차 합의", rendered)
        self.assertIn("결과지 현황 — 확정 0/10", rendered)

    def test_only_confirmed_meetings_enter_memory(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            st1 = memory.update_state(memory.empty_state("T1-P1"), minutes(1), 1)
            memory.save_snapshot(tmp, st1)                                             # 1차: 회의록 확정됨
            memory._write_json(memory.draft_path(tmp, 2), memory.update_state(st1, minutes(2), 2))  # 2차: 초안뿐
            (tmp / "minutes_02.md").write_text(minutes(2), encoding="utf-8")
            self.assertEqual(memory.load_before("T1-P1", tmp, 3)["meeting_no"], 1)  # 확정되지 않은 2차는 건너뛴다
            self.assertEqual(memory.load_before("T1-P1", tmp, 2)["meeting_no"], 1)  # 2차를 다시 돌릴 때는 1차까지만
            self.assertEqual(memory.latest("T1-P1", tmp)["meeting_no"], 1)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class MaskingTest(unittest.TestCase):
    def test_names_company_phone_and_email_are_hidden_then_restored(self):
        seen = []

        class Spy:
            def chat(self, messages, tools=None):
                seen.append(json.dumps(messages, ensure_ascii=False))
                return {"role": "assistant", "content": "[인물1]님과 [고객사A] 확인, 연락처 [전화1]"}

        masker = Masker(*build_dictionary("테스트상사(주)", [TRANSCRIPT], ["최민호"]))
        out = MaskedLLM(Spy(), masker).chat([{"role": "user", "content": "테스트상사(주) 정수연 팀장, 김도현, 최민호 · 010-1234-5678 · han@test.co.kr"}])
        for secret in ("정수연", "김도현", "최민호", "테스트상사", "010-1234-5678", "han@test.co.kr"):
            self.assertNotIn(secret, seen[0])
        self.assertIn("[고객사A]", seen[0])
        self.assertIn("[메일1]", seen[0])
        self.assertNotIn("[인물", out["content"])       # 돌아온 답은 실명으로 복원
        self.assertIn("010-1234-5678", out["content"])  # 전화번호도 복원


class ChunkTest(unittest.TestCase):
    def test_split_keeps_header_and_whole_lines(self):
        text = "[2026-10-01 / 방문]\n참석: (고객) 정수연\n" + "\n".join(f"김도현: 발언 {i} " + "가" * 300 for i in range(20))
        head, chunks = tools.split_transcript(text, size=1000)
        self.assertTrue(head.startswith("[2026"))
        self.assertGreater(len(chunks), 3)
        self.assertTrue(all(c.startswith("김도현:") for c in chunks))
        self.assertEqual(sum(c.count("김도현:") for c in chunks), 20)


if __name__ == "__main__":
    unittest.main()
