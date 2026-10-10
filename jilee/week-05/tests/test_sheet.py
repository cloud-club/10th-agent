"""Week 5: 결과지(근거 대조 · 초안/확정 · 번복 · 판정)와 고객 담당자·고객 프로필 반영을 LLM 없이 검증한다."""

import shutil
import unittest

from agent import sheet, store, tools
from tests.helpers import CID, PID, SHEET_UPDATE, TRANSCRIPT, make_project

UTTS = store.utterances(TRANSCRIPT)
KNOWN = ["정수연", "김도현"]


def applied(upd=SHEET_UPDATE, base=None, meeting_no=1):
    return sheet.apply(base or sheet.empty(), upd, meeting_no, UTTS, KNOWN, TRANSCRIPT)


class GroundingTest(unittest.TestCase):
    def test_evidence_is_replaced_by_the_real_utterance(self):
        sh, errs, _ = applied()
        self.assertEqual(errs, [])
        ev = sh["items"]["S1"]["evidence"][0]
        self.assertEqual((ev["speaker"], ev["pos"], ev["meeting"]), ("정수연", 2, 1))
        self.assertIn("검사원이 열두 명", ev["text"])  # 모델이 적은 인용이 아니라 녹취의 발언이 저장된다

    def test_evidence_can_point_to_an_utterance_number(self):
        # 긴 녹취는 구간 정리본으로 읽으므로 원문을 그대로 옮길 수 없다. 그래서 발언 번호로 가리킨다
        sh, errs, _ = applied({"summary": "요약", "items": [{"code": "S1", "value": "육안 검사", "evidence": "#2"}]})
        self.assertEqual(errs, [])
        self.assertEqual(sh["items"]["S1"]["evidence"][0]["speaker"], "정수연")
        self.assertIn("#2 정수연: 저희는 외관 검사를", store.numbered(TRANSCRIPT))
        _, errs, _ = applied({"summary": "요약", "items": [{"code": "S1", "value": "육안 검사", "evidence": "#99"}]})
        self.assertTrue(any("#99" in e and "이번 녹취에 없다" in e for e in errs))

    def test_invented_evidence_is_rejected_with_nearest_utterances(self):
        upd = dict(SHEET_UPDATE, items=[{"code": "B2", "value": "3억", "evidence": "총 사업예산은 삼억 이천만 원으로 잡혀 있습니다"}])
        sh, errs, _ = applied(upd)
        self.assertTrue(any("B2" in e and "찾지 못했다" in e for e in errs))
        self.assertEqual(sh, sheet.empty())  # 하나라도 어긋나면 아무것도 반영하지 않는다

    def test_unknown_person_and_bad_role_are_rejected(self):
        ghost = dict(SHEET_UPDATE, stakeholders=[{"name": "홍길동", "roles": []}])
        self.assertTrue(any("홍길동" in e for e in applied(ghost)[1]))
        bad = dict(SHEET_UPDATE, stakeholders=[{"name": "정수연", "roles": ["사장"], "evidence": "#7"}])
        self.assertTrue(any("사장" in e for e in applied(bad)[1]))
        title_only = dict(SHEET_UPDATE, stakeholders=[{"name": "공장장", "roles": ["의사결정자"], "evidence": "#7"}])
        self.assertTrue(any("사람 이름이 아니다" in e for e in applied(title_only)[1]))  # 이름을 모르면 사람으로 등록하지 않는다

    def test_our_own_people_are_not_given_customer_roles(self):
        upd = {"summary": "요약", "stakeholders": [{"name": "김도현", "roles": ["챔피언"], "evidence": "#1"}]}
        sh, errs, extra = sheet.apply(sheet.empty(), upd, 1, UTTS, KNOWN, TRANSCRIPT, own_names=["김도현"])
        self.assertEqual((errs, sh["stakeholders"], extra["contacts"]), ([], [], []))  # 우리 쪽은 조직 정보에서 온다


class GateTest(unittest.TestCase):
    def test_drafts_do_not_count_until_a_person_confirms(self):
        sh, _, _ = applied()
        g = sheet.gate(sh)
        self.assertEqual((g["confirmed"], g["draft"], g["missing"]), (0, 6, 4))  # S1 S2 S3 S4 D1 D2 초안 · B1~B4 미확보
        self.assertEqual(g["by_code"]["D2"], "초안")  # '구매' 역할이 계약 담당자 자리를 채운다
        self.assertEqual(sheet.confirm(sh, "item", "S1", "이정인"), 1)
        self.assertEqual(sheet.confirm(sh, "stakeholder", "최민호", "이정인"), 1)
        g = sheet.gate(sh)
        self.assertEqual((g["confirmed"], g["by_code"]["S1"], g["by_code"]["D1"]), (2, "확인", "확인"))

    def test_changed_value_returns_to_draft_and_keeps_the_old_one(self):
        sh, _, _ = applied()
        sheet.confirm(sh, "all", "", "이정인")
        again = {"summary": "요약", "items": [{"code": "S1", "value": "검사원 12명이 눈으로 검사", "evidence": "외관 검사를 사람이 눈으로 합니다"}]}
        sh2, errs, _ = sheet.apply(sh, again, 2, UTTS, KNOWN, TRANSCRIPT)
        self.assertEqual(errs, [])
        self.assertEqual(sh2["items"]["S1"]["status"], "초안")
        self.assertIn("previous", sh2["items"]["S1"])
        self.assertEqual(sh2["items"]["S1"]["since"], 1)   # 처음 확보한 회차는 그대로
        self.assertEqual(sh2["items"]["S2"]["status"], "확인")  # 건드리지 않은 항목은 확정 그대로

    def test_same_goal_is_updated_not_duplicated(self):
        sh, _, _ = applied()
        more = {"summary": "요약", "goals": [{"goal": "불량 유출을 줄이기", "target": "분기 0건", "evidence": "유출을 분기 한 건 이하로 줄이는 게 목표입니다"}]}
        sh2, errs, _ = sheet.apply(sh, more, 2, UTTS, KNOWN, TRANSCRIPT)
        self.assertEqual((errs, len(sh2["goals"]), sh2["goals"][0]["target"]), ([], 1, "분기 0건"))


class MasterDataTest(unittest.TestCase):
    def setUp(self):
        self.tmp, self.patches = make_project()
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_contacts_and_profile_go_to_the_customer_not_the_project(self):
        profile = [{"field": "주요 설비", "value": "외관 검사 라인(검사원 12명)", "evidence": "검사원이 열두 명이에요 외관 검사를 사람이 눈으로"}]
        out = tools.update_result_sheet(CID, PID, 1, customer_profile=profile, **SHEET_UPDATE)
        self.assertIn("아직 미확보: B1 예산 상태", out)
        cust = store.load_customer(CID)
        han = next(c for c in cust["contacts"] if c["name"] == "한지은")
        self.assertEqual((han["phone"], han["dept"]), ("010-1234-5678", "구매팀"))
        self.assertEqual(len([c for c in cust["contacts"] if c["name"] == "정수연"]), 1)  # 이미 있던 사람은 늘지 않는다
        self.assertEqual(cust["profile"]["주요 설비"]["status"], "초안")
        self.assertIn("주요 설비 = 외관 검사 라인", tools.read_project_context(CID, "P2", 1))  # 다른 프로젝트에서도 보인다

    def test_deal_terms_are_set_by_a_person_and_add_checks(self):
        hint = [{"field": "사업 유형", "value": "정부지원사업(보조금 + 자부담)", "evidence": "#3"}]
        tools.update_result_sheet(CID, PID, 1, deal_hints=hint, **SHEET_UPDATE)
        self.assertIn("사업 유형 = 미정", tools.read_project_context(CID, PID, 1))  # 에이전트의 제안은 값을 바꾸지 않는다
        self.assertEqual(sheet.required_extras(store.load_project(CID, PID).get("deal")), [])
        store.set_deal(CID, PID, "사업 유형", "정부지원사업(보조금 + 자부담)")                   # 사람이 정한다
        store.set_deal(CID, PID, "계약 지위", "하도급")
        ctx = tools.read_project_context(CID, PID, 1)
        self.assertIn("사업 유형 = 정부지원사업(보조금 + 자부담)", ctx)
        self.assertIn("| C1 | 재원·자부담 조달 | 미확보", ctx)   # 정부지원이면 자부담을 확인해야 한다
        self.assertIn("| C2 | 원청·주관기관의 결정 구조 | 미확보", ctx)
        self.assertNotIn("| C3 |", ctx)
        with self.assertRaises(ValueError):
            store.set_deal(CID, PID, "비밀유지 체결", "아마도")
        bad = dict(SHEET_UPDATE, deal_hints=[{"field": "비밀유지 체결", "value": "아마도", "evidence": "#1"}])
        self.assertTrue(any("deal_hints" in e for e in sheet.apply(sheet.empty(), bad, 1, UTTS, KNOWN, TRANSCRIPT)[1]))

    def test_deal_axes_and_events_do_not_mix(self):
        # 분류 축은 축마다 하나, 조건부 이벤트는 서로 독립이라 함께 켜진다
        self.assertFalse(set(store.DEAL_AXES) & set(store.DEAL_EVENTS))
        for values in store.DEAL_AXES.values():
            self.assertEqual(len(values), len(set(values)))
        deal = {"계약 목적물": ["용역(개발·구축)", "물품(HW·설비)"], "비밀유지계약 체결": "필요", "공동계약 방식": "분담이행(맡은 부분만 책임)", "시연·실증": "해당 없음", "계약 방법": "경쟁입찰"}
        self.assertEqual(sheet.required_extras(deal), ["C2", "C3", "C4", "C5", "C7"])

    def test_meeting_is_read_from_the_transcript_header_or_registered(self):
        m = store.load_meeting(CID, PID, 1)
        self.assertEqual((m["date"], m["time"], m["type"], m["place"]), ("2026-10-01", "10:00", "방문", "테스트상사 본사"))
        self.assertEqual(m["attendees"], ["정수연", "한지은", "김도현"])
        n = store.register_meeting(CID, PID, "2026-10-30", "14:00", "화상", "")
        self.assertEqual((n, tools.gate_of(CID, PID, n)["state"]), (2, "예정"))
        self.assertEqual(store.list_tree()[0]["projects"][0]["meetings"], [1, 2])

    def test_team_is_resolved_from_org_directory(self):
        self.assertEqual(store.team(CID, PID)[0]["email"], "kim@example.com")
        self.assertEqual([p["project_id"] for p in store.list_tree()[0]["projects"]], ["P1", "P2"])


if __name__ == "__main__":
    unittest.main()
