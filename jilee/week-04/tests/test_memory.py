"""Week 4: 딜 기억(상태 파일)·저장 전 규칙 검사·마스킹·긴 녹취 분할을 LLM 없이 검증한다."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent import loop as agent_mod
from agent import memory, tools
from agent.masking import MaskedLLM, Masker, build_dictionary

Q_ROWS = "\n".join(f"| Q{i:02d} | 항목 | {'이번 확보' if i <= 3 else '미확보'} | 근거 또는 질문 |" for i in range(1, 12))


def minutes(no=1, action_due="10월 8일", q_rows=Q_ROWS, owner="김도현(당사)", review=""):
    return f"""# 회의록 — 테스트상사 {no}차 회의

- 일시: 2026-10-0{no} 10:00

## 1. 참석자
| 구분 | 이름 | 직위/역할 | 비고(결정권 등) |
| --- | --- | --- | --- |
| 고객 | 정수연 | 품질팀장 | 최초 접점 |
| 당사 | 김도현 | 영업 | |

## 2. 지난 회의 액션아이템 점검
| 항목 | 담당 | 기한 | 결과(완료 · 진행 중 · 미착수) |
| --- | --- | --- | --- |
{review}

## 3. 논의 요지
- 요지

## 4. 고객 요구·문제점
| 구분 | 내용(고객 표현 그대로) | 중요도 | 시급성 |
| --- | --- | --- | --- |
| 문제점 | 불량 유출 | 높음 | 보통 |

## 5. 합의 사항
- {no}차 합의

## 6. 미합의 사항 · 다음 행동
- 예산 → 다음 회의 확인

## 7. 액션아이템
| 항목 | 담당 | 기한 |
| --- | --- | --- |
| 견적서 제공 | {owner} | {action_due} |

## 8. 확보된 표준 질문 항목
- 니즈/정보 확보율: 3/11 (27%)

| 코드 | 확인 항목 | 상태 | 근거 · 다음 회의 확인 사항 |
| --- | --- | --- | --- |
{q_rows}
"""


KNOWN = "정수연 김도현"


class ValidateTest(unittest.TestCase):
    def test_good_minutes_pass(self):
        self.assertEqual(memory.validate(minutes(), KNOWN, None), [])

    def test_action_without_due_is_rejected(self):
        errs = memory.validate(minutes(action_due="확인 불가"), KNOWN, None)
        self.assertTrue(any("7절" in e and "기한" in e for e in errs))

    def test_missing_q_rows_and_bad_status(self):
        rows = Q_ROWS.replace("| Q11 | 항목 | 미확보 | 근거 또는 질문 |", "").replace("| Q01 | 항목 | 이번 확보", "| Q01 | 항목 | 확보됨")
        errs = memory.validate(minutes(q_rows=rows), KNOWN, None)
        self.assertTrue(any("Q11" in e for e in errs))
        self.assertTrue(any("Q01" in e and "허용 값" in e for e in errs))

    def test_unknown_person_is_rejected(self):
        errs = memory.validate(minutes(owner="홍길동(당사)"), KNOWN, None)
        self.assertTrue(any("홍길동" in e for e in errs))

    def test_regressing_q_code_is_rejected(self):
        prev = {"questions": {"Q05": {"status": "확보", "since": 1}}}
        errs = memory.validate(minutes(), KNOWN, prev)  # Q05는 미확보로 적혀 있다
        self.assertTrue(any("Q05" in e and "기존 확보" in e for e in errs))

    def test_missing_section(self):
        md = minutes().replace("## 5. 합의 사항", "## 합의")
        self.assertTrue(any("5. 합의 사항" in e for e in memory.validate(md, KNOWN, None)))


class StateTest(unittest.TestCase):
    def test_state_accumulates_across_meetings(self):
        st = memory.update_state(memory.empty_state("T1"), minutes(1), 1)
        self.assertEqual(st["questions"]["Q01"], {"status": "확보", "since": 1, "evidence": "근거 또는 질문"})
        self.assertEqual(st["action_items"][0]["status"], "미착수")

        rows2 = Q_ROWS.replace("| Q01 | 항목 | 이번 확보", "| Q01 | 항목 | 기존 확보").replace("| Q04 | 항목 | 미확보", "| Q04 | 항목 | 이번 확보")
        md2 = minutes(2, q_rows=rows2, review="| 견적서 제공 | 김도현 | 10월 8일 | 완료 (메일 발송) |")
        st2 = memory.update_state(st, md2, 2)
        self.assertEqual(st2["questions"]["Q01"]["since"], 1)       # 처음 확보한 회차 유지
        self.assertEqual(st2["questions"]["Q04"]["since"], 2)
        self.assertEqual(st2["action_items"][0]["status"], "완료")  # 2절 점검 결과 반영
        self.assertEqual([d["meeting"] for d in st2["decisions"]], [1, 2])  # 합의 누적
        self.assertEqual(memory.kpi(st2)["이행률"], "1/1 (100%)")
        self.assertIn("(1차) 1차 합의", memory.render_state(st2))

    def test_replay_from_old_minutes_without_snapshot(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            (tmp / "minutes_01.md").write_text(minutes(1), encoding="utf-8")
            (tmp / "minutes_02.md").write_text(minutes(2), encoding="utf-8")
            st = memory.load_before("T1", tmp, tmp, 3)
            self.assertEqual(st["meeting_no"], 2)
            st1 = memory.load_before("T1", tmp, tmp, 2)            # 2차를 다시 돌릴 때는 1차까지만
            self.assertEqual(st1["meeting_no"], 1)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class SaveRejectionLoopTest(unittest.TestCase):
    """규칙 위반 회의록은 저장되지 않고, 모델이 사유를 받아 고쳐 다시 저장한다."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        d = self.tmp / "deals" / "T1"
        d.mkdir(parents=True)
        (d / "customer.md").write_text("# 고객정보 — 테스트상사(주)\n- 당사 담당: 김도현(영업)\n- 고객 접점(최초): 정수연 품질팀장", encoding="utf-8")
        (d / "transcript_01.txt").write_text("[2026-10-01]\n참석: (고객) 정수연 품질팀장 / (당사) 김도현\n김도현: 안녕하세요 테스트상사", encoding="utf-8")
        self.patches = [mock.patch.object(tools, "DEALS_DIR", self.tmp / "deals"),
                        mock.patch.object(tools, "OUTPUT_DIR", self.tmp / "out")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_reject_then_fix(self):
        from tests.test_agent import FakeLLM
        llm = FakeLLM([
            ("save_minutes", {"deal_id": "T1", "meeting_no": 1, "markdown": minutes(action_due="확인 불가")}),
            ("save_minutes", {"deal_id": "T1", "meeting_no": 1, "markdown": minutes()}),
        ])
        r = agent_mod.run_agent("T1", 1, llm=llm)
        saves = [e for e in r.trace if e.get("name") == "save_minutes"]
        self.assertEqual([e["ok"] for e in saves], [False, True])
        self.assertIn("저장 거부", saves[0]["output_preview"])
        snap = json.loads((self.tmp / "out" / "T1" / "state" / "state_01.json").read_text(encoding="utf-8"))
        self.assertEqual(snap["meeting_no"], 1)
        self.assertIn("딜 기억", tools.read_deal_context("T1", 2))

    def test_masking_hides_names_from_llm(self):
        seen = []

        class Spy:
            def chat(self, messages, tools=None):
                seen.append(json.dumps(messages, ensure_ascii=False))
                return {"role": "assistant", "content": "[인물1]님과 [고객사A] 확인"}

        masker = Masker(*build_dictionary(self.tmp / "deals" / "T1"))
        out = MaskedLLM(Spy(), masker).chat([{"role": "user", "content": "테스트상사(주) 정수연 팀장, 김도현"}])
        self.assertNotIn("정수연", seen[0])
        self.assertNotIn("테스트상사", seen[0])
        self.assertIn("[고객사A]", seen[0])
        self.assertNotIn("[인물", out["content"])  # 돌아온 답은 실명으로 복원


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
