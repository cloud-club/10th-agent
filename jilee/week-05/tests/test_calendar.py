"""캘린더: 말로 적힌 기한을 날짜로 풀기, iCal 읽기·쓰기, 우리 일정 모으기, 구글 일정과 합치기를 네트워크 없이 검증한다."""

import shutil
import unittest
from datetime import date
from unittest import mock

from agent import dates, gcal, memory, store
from tests.helpers import CID, PID, make_project, minutes

BASE = date(2026, 10, 16)  # 금요일

ICS = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
    "BEGIN:VEVENT\r\nUID:a1@google.com\r\nDTSTART;VALUE=DATE:20261030\r\nDTEND;VALUE=DATE:20261031\r\nSUMMARY:창립기념일\r\nEND:VEVENT\r\n"
    "BEGIN:VEVENT\r\nUID:a2@google.com\r\nDTSTART:20261001T010000Z\r\nDTEND:20261001T020000Z\r\n"
    "SUMMARY:테스트상사 방문\\, 검사 공정 청취\r\nLOCATION:테스트상사 본사\\; 2층\r\n"
    "BEGIN:VALARM\r\nACTION:DISPLAY\r\nDESCRIPTION:알림 문구\r\nEND:VALARM\r\nEND:VEVENT\r\n"
    "BEGIN:VEVENT\r\nUID:a3@google.com\r\nDTSTART;TZID=Asia/Seoul:20261105T140000\r\nDTEND;TZID=Asia/Seoul:20261105T153000\r\n"
    "SUMMARY:주간 영업 회의에서 다룰 안건을 길게 적어 한 줄이 일흔다섯 바이트를 넘도록 만든\r\n  제목\r\nRRULE:FREQ=WEEKLY\r\nEND:VEVENT\r\n"
    "BEGIN:VEVENT\r\nUID:a3@google.com\r\nRECURRENCE-ID:20261112T050000Z\r\nDTSTART:20261112T060000Z\r\nSUMMARY:옮겨진 회차\r\nEND:VEVENT\r\n"
    "END:VCALENDAR\r\n"
)


class DatesTest(unittest.TestCase):
    def check(self, text, day, approx=False):
        r = dates.resolve(text, BASE)
        self.assertEqual((r["date"], r["approx"], r["raw"]), (day, approx, text))

    def test_exact_dates(self):
        self.check("2026-10-08", "2026-10-08")
        self.check("2026.10.8", "2026-10-08")
        self.check("10월 8일까지", "2026-10-08")
        self.check("10/8", "2026-10-08")
        self.check("1월 15일", "2027-01-15")       # 기준일보다 한 달 넘게 앞이면 해를 넘긴 기한이다
        self.check("다음 주 수요일", "2026-10-21")
        self.check("다음 주 월요일", "2026-10-19")
        self.check("이번 주 일요일", "2026-10-18")
        self.check("내일", "2026-10-17")
        self.check("모레 오전", "2026-10-18")

    def test_spoken_dates_are_approximate(self):
        self.check("이번 주 안", "2026-10-16", True)   # 그 주 금요일
        self.check("다음 주까지", "2026-10-23", True)
        self.check("그 다음 주", "2026-10-30", True)
        self.check("10월 말", "2026-10-31", True)
        self.check("11월 초", "2026-11-05", True)
        self.check("12월 중순", "2026-12-15", True)
        self.check("12월 셋째 주", "2026-12-18", True)
        self.check("2026-12-3rd week", "2026-12-18", True)  # 12월 3일로 읽지 않는다
        self.check("이달 말", "2026-10-31", True)
        self.check("이번 달 말까지", "2026-10-31", True)

    def test_exact_wins_when_mixed_and_unreadable_is_none(self):
        self.check("2026-10-08 (10월 8일까지)", "2026-10-08")
        self.check("다음 주 수요일(10월 22일)", "2026-10-22")
        for text in ("추후 협의", "확인 불가", "", "2월 30일"):
            self.check(text, None)
        self.assertEqual(dates.resolve(None, BASE), {"date": None, "approx": False, "raw": ""})


class IcsTest(unittest.TestCase):
    def test_parse_all_day_utc_tzid_folded_and_escaped(self):
        ev = gcal.parse_ics(ICS)
        self.assertEqual([e["uid"] for e in ev], ["a2@google.com", "a1@google.com", "a3@google.com"])  # 날짜순 · 개별 수정분은 뺀다
        visit, holiday, weekly = ev
        self.assertEqual((visit["date"], visit["time"], visit["end_time"], visit["all_day"]), ("2026-10-01", "10:00", "11:00", False))  # UTC → 서울
        self.assertEqual((visit["title"], visit["location"]), ("테스트상사 방문, 검사 공정 청취", "테스트상사 본사; 2층"))
        self.assertEqual((holiday["date"], holiday["end_date"], holiday["time"], holiday["all_day"]), ("2026-10-30", "2026-10-30", "", True))
        self.assertEqual((weekly["date"], weekly["time"], weekly["end_time"], weekly.get("recurring")), ("2026-11-05", "14:00", "15:30", True))
        self.assertTrue(weekly["title"].endswith("넘도록 만든 제목"))  # 접힌 줄을 폈다
        self.assertNotIn("recurring", visit)

    def test_fetch_only_talks_to_google_calendar(self):
        for url in ("https://example.com/basic.ics", "webcal://example.com/basic.ics", "http://calendar.google.com/x.ics", ""):
            with self.assertRaises(ValueError):
                gcal.fetch_google(url)

        class Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return ICS.encode("utf-8")

        with mock.patch.object(gcal.urllib.request, "build_opener") as opener:
            opener.return_value.open.return_value = Resp()
            ev = gcal.fetch_google("webcal://calendar.google.com/calendar/ical/x/private-abc/basic.ics")
            asked = opener.return_value.open.call_args[0][0]
        self.assertEqual(len(ev), 3)
        self.assertEqual(asked.full_url, "https://calendar.google.com/calendar/ical/x/private-abc/basic.ics")


class EventsTest(unittest.TestCase):
    def setUp(self):
        self.tmp, self.patches = make_project()
        for p in self.patches:
            p.start()
        d = store.project_dir(CID, PID)
        st = memory.update_state(memory.empty_state(store.key(CID, PID)), minutes(1), 1)  # 회의일 2026-10-01 · 견적서 제공(10월 8일)
        st["action_items"] += [
            {"id": "A01-2", "meeting": 1, "task": "샘플 판정", "owner": "정수연(고객)", "due": "추후 협의", "status": "진행 중"},
            {"id": "A01-3", "meeting": 1, "task": "명함 전달", "owner": "김도현(당사)", "due": "10월 2일", "status": "완료"},
        ]
        st["sheet"]["items"]["B4"] = {"value": "10월 말 계약 체결 예정", "status": "초안", "since": 1, "updated": 1, "evidence": []}
        memory.save_snapshot(d, st)

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_meetings_open_actions_and_contract_in_one_list(self):
        rows = gcal.events()
        self.assertEqual([(r["kind"], r["date"]) for r in rows],
                         [("meeting", "2026-10-01"), ("action", "2026-10-08"), ("contract", "2026-10-31"), ("action", None)])
        meeting, action, contract, undated = rows
        self.assertEqual((meeting["time"], meeting["no"], meeting["detail"], meeting["customer"]), ("10:00", 1, "테스트상사 본사", "테스트상사(주)"))
        self.assertIn(meeting["state"], ("결과지 확인", "완료"))
        self.assertEqual((action["title"], action["detail"], action["state"], action["approx"]), ("견적서 제공", "김도현(당사)", "미착수", False))
        self.assertEqual((contract["approx"], contract["project_id"]), (True, PID))
        self.assertEqual((undated["title"], undated["raw"]), ("샘플 판정", "추후 협의"))  # 날짜를 못 읽은 기한도 빠뜨리지 않는다
        self.assertNotIn("명함 전달", [r["title"] for r in rows])                         # 끝난 일은 뺀다
        keys = {"date", "time", "kind", "title", "customer_id", "customer", "project_id", "project", "no", "approx", "state", "detail"}
        self.assertTrue(all(keys <= set(r) for r in rows))

    def test_feed_round_trips_and_keeps_stable_ids(self):
        ics = gcal.feed_ics()
        self.assertTrue(ics.startswith("BEGIN:VCALENDAR\r\n") and ics.endswith("END:VCALENDAR\r\n"))
        self.assertTrue(all(len(l.encode("utf-8")) <= 75 for l in ics.split("\r\n")))
        back = gcal.parse_ics(ics)
        self.assertEqual([(e["date"], e["time"], e["all_day"]) for e in back],
                         [("2026-10-01", "10:00", False), ("2026-10-08", "", True), ("2026-10-31", "", True)])  # 기한 미정은 내보내지 않는다
        self.assertEqual(back[0]["title"], "[회의] 테스트상사(주) 검사 자동화 1차")
        self.assertEqual((back[0]["end_time"], back[0]["location"]), ("11:00", "테스트상사 본사"))
        self.assertEqual(back[1]["title"], "[기한] 테스트상사(주) 검사 자동화 — 견적서 제공")
        self.assertTrue(back[2]["title"].startswith("[계약 예상]") and back[2]["title"].endswith("(어림)"))
        self.assertEqual(back[0]["uid"], f"meeting-{CID}-{PID}-1@sales-ax")
        self.assertEqual([e["uid"] for e in back], [e["uid"] for e in gcal.parse_ics(gcal.feed_ics())])  # 다시 내보내도 같은 UID

    def test_merge_marks_google_events_that_are_our_meetings(self):
        merged = gcal.merge(gcal.events(), gcal.parse_ics(ICS))
        google = {r["uid"]: r for r in merged if r["kind"] == "google"}
        self.assertEqual(google["a2@google.com"]["matched"], {"customer_id": CID, "project_id": PID, "no": 1})  # 같은 날 같은 시각
        self.assertNotIn("matched", google["a1@google.com"])   # 종일 일정은 맞춰 보지 않는다
        self.assertNotIn("matched", google["a3@google.com"])
        self.assertTrue(google["a3@google.com"]["recurring"])
        self.assertEqual(len(merged), 7)
        self.assertEqual(merged[-1]["date"], None)             # 날짜 없는 것은 맨 뒤
        self.assertNotIn("창립기념일", gcal.feed_ics(merged))   # 구글에서 온 일정을 다시 내보내지 않는다


if __name__ == "__main__":
    unittest.main()
