"""회의 품질(입력)과 회의록 품질(출력)의 코드 측정을 LLM 없이 검증한다."""

import shutil
import unittest

from agent import memory, quality, sheet, store
from tests.helpers import CID, PID, SHEET_UPDATE, TRANSCRIPT, make_project, minutes

UTTS = store.utterances(TRANSCRIPT)
KNOWN = ["정수연", "김도현"]
NEW_SHEET = sheet.apply(sheet.empty(), SHEET_UPDATE, 1, UTTS, KNOWN, TRANSCRIPT)[0]


def check(result: dict, key: str) -> dict:
    return next(c for c in result["checks"] if c["key"] == key)


class KoreanNumbersTest(unittest.TestCase):
    def test_amounts_keep_the_whole_value_and_each_unit_part(self):
        got = quality.korean_numbers("총 금액이 일억 이천 사백만 원 부가세 별도고요")
        self.assertLessEqual({"124000000", "1", "2400"}, got)  # 회의록의 '1억 2,400만'과 맞추려고 마디 값도 넣는다
        self.assertLessEqual({"40000000", "4000"}, quality.korean_numbers("클레임 비용이 사천만 원 넘게"))

    def test_plain_numbers_decimals_and_seconds(self):
        self.assertIn("94", quality.korean_numbers("검출률이 구십사 퍼센트 정도"))
        self.assertLessEqual({"0.7", "0.9"}, quality.korean_numbers("평균 영 점 칠 초 최대 영 점 구 초였습니다"))
        self.assertIn("2.8", quality.korean_numbers("우리 라인 택트가 이 초 팔인데"))
        self.assertNotIn("1.2", quality.korean_numbers("일 초 이내를 성능 조건으로"))  # '이내'의 '이'는 숫자가 아니다

    def test_day_and_particle_do_not_bend_the_number(self):
        self.assertIn("29", quality.korean_numbers("이번 달은 이십구일이에요"))   # '일'·'이'를 앞 수에 붙이지 않는다
        self.assertLessEqual({"12", "26"}, quality.korean_numbers("십이월 이십육일까지"))

    def test_native_numerals_and_month_names(self):
        self.assertIn("12", quality.korean_numbers("검사원이 열두 명이에요"))
        self.assertLessEqual({"18", "17"}, quality.korean_numbers("열여덟 개 중에 열일곱 개"))
        self.assertIn("10", quality.korean_numbers("시월 중순에 품의"))


class MeetingQualityTest(unittest.TestCase):
    def test_useful_meeting_scores_full(self):
        r = quality.meeting_quality(TRANSCRIPT, sheet.empty(), NEW_SHEET)
        self.assertEqual(r["score"], 100)
        self.assertEqual(check(r, "customer_share")["value"], 73)       # 고객 측 발화 글자 수 비율
        gained = check(r, "gained")
        self.assertEqual(gained["value"], 6)                            # S1 S2 S3 S4 D1 D2
        self.assertIn("아직 미확보: B1 예산 상태", gained["detail"])
        self.assertEqual(set(check(r, "speakers")), {"key", "label", "value", "ok", "detail"})

    def test_memo_without_header_or_speakers(self):
        r = quality.meeting_quality("오늘 통화함. 예산은 다음에 얘기하기로.", sheet.empty(), sheet.empty())
        self.assertFalse(check(r, "header")["ok"])
        self.assertFalse(check(r, "speakers")["ok"])
        self.assertIsNone(check(r, "customer_share")["ok"])   # 잴 수 없는 것은 점수의 분모에서 뺀다
        self.assertFalse(check(r, "gained")["ok"])
        self.assertFalse(check(r, "next_action")["ok"])       # 미확보가 남았는데 다음 질문이 없다
        self.assertEqual(r["score"], 0)

    def test_we_talked_more_than_the_customer(self):
        text = "[2026-10-01 10:00 / 화상]\n참석: (고객) 정수연 품질팀장 / (당사) 김도현\n김도현: " + "제품 설명 " * 30 + "\n정수연: 네 알겠습니다."
        share = check(quality.meeting_quality(text, sheet.empty(), sheet.empty()), "customer_share")
        self.assertFalse(share["ok"])
        self.assertLess(share["value"], 40)

    def test_nothing_left_to_gain_is_not_scored(self):
        done = sheet.gate(NEW_SHEET)["by_code"]
        r = quality.meeting_quality(TRANSCRIPT, NEW_SHEET, NEW_SHEET)
        self.assertEqual(done["S1"], "초안")
        self.assertFalse(check(r, "gained")["ok"])            # B1~B4가 비어 있었는데 이 회의로 채운 것이 없다
        deal = {"사업 유형": "정부지원사업(보조금 + 자부담)"}
        self.assertIn("C1 재원·자부담 조달", check(quality.meeting_quality(TRANSCRIPT, NEW_SHEET, NEW_SHEET, deal), "gained")["detail"])


class MinutesQualityTest(unittest.TestCase):
    def test_fixture_minutes(self):
        r = quality.minutes_quality(minutes(), TRANSCRIPT, KNOWN)
        self.assertTrue(check(r, "format")["ok"])
        self.assertEqual(check(r, "numbers")["value"], 100)   # 2026 · 10 · 12(열두 명) 모두 녹취에 있다
        self.assertTrue(check(r, "people")["ok"])
        self.assertFalse(check(r, "structure")["ok"])         # 주제가 하나뿐이다
        self.assertFalse(check(r, "gist")["ok"])
        dropped = check(r, "coverage")
        self.assertFalse(dropped["ok"])
        self.assertIn("#6 한지은", dropped["detail"])         # 구매 담당의 발언이 회의록에 없다

    def test_invented_number_is_listed(self):
        md = minutes().replace("- 1차 합의", "- 총 사업예산 3억 5,000만 원으로 합의")
        numbers = check(quality.minutes_quality(md, TRANSCRIPT, KNOWN), "numbers")
        self.assertFalse(numbers["ok"])
        self.assertIn("5000", numbers["detail"])              # 녹취 어디에도 없는 금액
        spoken = minutes().replace("- 1차 합의", "- 클레임 비용 4,000만 원 이상")
        self.assertTrue(check(quality.minutes_quality(spoken, TRANSCRIPT, KNOWN), "numbers")["ok"])  # '사천만 원'으로 말했다

    def test_carried_over_actions_and_headings_are_not_counted_as_numbers(self):
        md = minutes(no=2, review="| 샘플 이미지 50장 제공 | 김도현 | 9월 30일 | 완료 |")
        detail = check(quality.minutes_quality(md, TRANSCRIPT, KNOWN), "numbers")["detail"]
        self.assertNotIn("50", detail)                        # 2절은 지난 회의에서 넘어온 것이라 이번 녹취와 맞추지 않는다
        self.assertNotIn("30", detail)

    def test_structure_gist_and_unknown_person(self):
        md = minutes(owner="홍길동(당사)").replace("- 일시:", "- 핵심 요약: 두 품목부터 자동화하기로 했다.\n- 일시:")
        md = md.replace("## 4. 고객", "### 범위 — 두 품목부터 시작한다\n- 하우징 두 종\n\n## 4. 고객")
        r = quality.minutes_quality(md, TRANSCRIPT, KNOWN)
        self.assertTrue(check(r, "structure")["ok"])
        self.assertTrue(check(r, "gist")["ok"])
        self.assertIn("홍길동", check(r, "people")["detail"])
        self.assertFalse(check(r, "format")["ok"])            # memory.validate도 같은 이름을 잡는다
        self.assertEqual(r["score"], 50)                      # 여섯 가운데 숫자·구조·핵심 요약만 통과


class ReportTest(unittest.TestCase):
    def setUp(self):
        self.tmp, self.patches = make_project()
        for p in self.patches:
            p.start()
        self.project = store.project_dir(CID, PID)

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_before_the_agent_runs_there_is_no_minutes_score(self):
        r = quality.report(CID, PID, 1)
        self.assertIsNone(r["minutes"])
        self.assertEqual(check(r["meeting"], "gained")["value"], 0)   # 결과지가 아직 그대로다
        with self.assertRaisesRegex(ValueError, "녹취가 없어"):
            quality.report(CID, PID, 2)

    def test_draft_is_read_before_it_is_confirmed(self):
        draft = memory.update_state(memory.empty_state(store.key(CID, PID)), minutes(), 1, NEW_SHEET)
        memory._write_json(memory.draft_path(self.project, 1), draft)
        (self.project / "minutes_01.md").write_text(minutes(), encoding="utf-8")
        r = quality.report(CID, PID, 1)
        self.assertEqual(check(r["meeting"], "gained")["value"], 6)   # 초안의 결과지를 본다
        self.assertEqual(r["minutes"]["score"], 50)
        memory.save_snapshot(self.project, draft)                     # 확정된 뒤에도 같은 값이 나온다
        memory.draft_path(self.project, 1).unlink()
        self.assertEqual(check(quality.report(CID, PID, 1)["meeting"], "gained")["value"], 6)


if __name__ == "__main__":
    unittest.main()
