"""구글 로그인 연동(캘린더 · 드라이브)을 실제 구글에 닿지 않고 검증한다. HTTP가 나가는 한 곳(_request)을 가로챈다."""

import base64
import json
import os
import unittest
import urllib.parse
from unittest import mock

from agent import google
from agent import llm as llm_mod

ENV = {"GOOGLE_CLIENT_ID": "cid.apps.googleusercontent.com", "GOOGLE_CLIENT_SECRET": "secret",
       "GOOGLE_REFRESH_TOKEN": "", "GOOGLE_ACCOUNT": ""}
REDIRECT = "http://localhost:8765/oauth/google/callback"


def id_token(email: str) -> str:
    part = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return f"{part({'alg': 'RS256'})}.{part({'email': email, 'sub': '1'})}.sig"


class GoogleTest(unittest.TestCase):
    def setUp(self):
        self.calls, self.replies, self.saved = [], [], {}

        def fake_save(updates):  # 실제 .env를 건드리지 않고, save_env처럼 환경변수에는 반영한다
            self.saved.update(updates)
            os.environ.update(updates)

        def fake_request(method, url, *, headers=None, data=None, timeout=20):
            self.calls.append({"method": method, "url": url, "headers": headers or {}, "data": data})
            status, body = self.replies.pop(0)
            return status, body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")

        self.patches = [mock.patch.dict("os.environ", ENV), mock.patch.object(llm_mod, "save_env", fake_save),
                        mock.patch.object(google, "_request", fake_request),
                        mock.patch.dict(google._token, {"value": "", "until": 0.0})]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def connect(self, token="at-1", expires=3600):
        os.environ["GOOGLE_REFRESH_TOKEN"] = "rt-1"
        self.replies.append((200, {"access_token": token, "expires_in": expires}))

    def form(self, i=-1):
        return dict(urllib.parse.parse_qsl(self.calls[i]["data"].decode("utf-8")))

    # ---- 로그인 ----
    def test_auth_url_asks_for_offline_access_and_consent(self):
        url = google.auth_url(REDIRECT, "st-123")
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        self.assertTrue(url.startswith("https://accounts.google.com/o/oauth2/v2/auth?"))
        self.assertEqual((q["client_id"], q["redirect_uri"], q["response_type"], q["state"]), (ENV["GOOGLE_CLIENT_ID"], REDIRECT, "code", "st-123"))
        self.assertEqual((q["access_type"], q["prompt"], q["include_granted_scopes"]), ("offline", "consent", "true"))
        self.assertEqual(q["scope"].split(), google.SCOPES)
        self.assertIn("https://www.googleapis.com/auth/drive.file", q["scope"])  # 앱이 만든 파일만
        with mock.patch.dict("os.environ", {"GOOGLE_CLIENT_ID": ""}):
            with self.assertRaisesRegex(RuntimeError, "GOOGLE_CLIENT_ID"):
                google.auth_url(REDIRECT, "x")

    def test_exchange_saves_refresh_token_and_account(self):
        self.replies.append((200, {"access_token": "at-0", "expires_in": 3600, "refresh_token": "rt-new", "id_token": id_token("ji.lee@example.com")}))
        out = google.exchange("code-1", REDIRECT)
        self.assertEqual(out, {"connected": True, "account": "ji.lee@example.com"})
        self.assertEqual(self.saved, {"GOOGLE_REFRESH_TOKEN": "rt-new", "GOOGLE_ACCOUNT": "ji.lee@example.com"})
        f = self.form()
        self.assertEqual((f["code"], f["grant_type"], f["redirect_uri"], f["client_secret"]), ("code-1", "authorization_code", REDIRECT, "secret"))
        self.assertEqual(google.status(), {"configured": True, "connected": True, "account": "ji.lee@example.com"})
        self.assertEqual(google.access_token(), "at-0")  # 방금 받은 것을 쓰고 다시 묻지 않는다
        self.assertEqual(len(self.calls), 1)

    def test_exchange_without_refresh_token_explains_why(self):
        self.replies.append((200, {"access_token": "at-0", "expires_in": 3600, "id_token": id_token("a@b.c")}))
        with self.assertRaisesRegex(RuntimeError, "이미 동의한 계정"):
            google.exchange("code-1", REDIRECT)
        self.assertEqual(self.saved, {})

    def test_access_token_is_cached_until_a_minute_before_expiry(self):
        self.connect("at-1", expires=3600)
        self.assertEqual(google.access_token(), "at-1")
        self.assertEqual(google.access_token(), "at-1")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.form(0)["grant_type"], "refresh_token")
        google._token["until"] = google.time.time() + 30   # 만료 30초 전 → 새로 받는다
        self.replies.append((200, {"access_token": "at-2", "expires_in": 3600}))
        self.assertEqual(google.access_token(), "at-2")

    def test_access_token_needs_a_connection(self):
        with self.assertRaisesRegex(RuntimeError, "연결되지 않았습니다"):
            google.access_token()
        self.assertEqual(self.calls, [])

    def test_invalid_grant_forgets_the_connection(self):
        os.environ.update({"GOOGLE_REFRESH_TOKEN": "rt-dead", "GOOGLE_ACCOUNT": "a@b.c"})
        self.replies.append((400, {"error": "invalid_grant", "error_description": "Token has been expired or revoked."}))
        with self.assertRaisesRegex(RuntimeError, "다시 로그인"):
            google.access_token()
        self.assertEqual(self.saved, {"GOOGLE_REFRESH_TOKEN": "", "GOOGLE_ACCOUNT": ""})
        self.assertFalse(google.status()["connected"])

    def test_status_never_returns_secrets(self):
        os.environ.update({"GOOGLE_REFRESH_TOKEN": "rt-1", "GOOGLE_ACCOUNT": "a@b.c"})
        dump = json.dumps(google.status())
        self.assertEqual(google.status(), {"configured": True, "connected": True, "account": "a@b.c"})
        for secret in ("rt-1", "secret", ENV["GOOGLE_CLIENT_ID"]):
            self.assertNotIn(secret, dump)

    def test_disconnect_revokes_and_forgets_even_if_revoke_fails(self):
        os.environ.update({"GOOGLE_REFRESH_TOKEN": "rt-1", "GOOGLE_ACCOUNT": "a@b.c"})
        self.replies.append((400, {"error": "invalid_token"}))
        google.disconnect()
        self.assertEqual((self.calls[0]["url"], self.form(0)["token"]), (google.REVOKE_URL, "rt-1"))
        self.assertEqual(google.status(), {"configured": True, "connected": False, "account": ""})

    # ---- 캘린더 ----
    def test_list_events_converts_and_follows_pages(self):
        self.connect()
        self.replies.append((200, {"nextPageToken": "p2", "items": [
            {"id": "e1", "summary": "한빛정밀 5차 회의", "location": "창원공장", "htmlLink": "https://cal/e1",
             "start": {"dateTime": "2026-10-30T05:00:00Z"}, "end": {"dateTime": "2026-10-30T06:30:00Z"}},
            {"id": "gone", "status": "cancelled", "start": {"date": "2026-10-30"}}]}))
        self.replies.append((200, {"items": [
            {"id": "e2", "summary": "워크숍", "start": {"date": "2026-11-02"}, "end": {"date": "2026-11-04"}},
            {"id": "e3", "start": {"dateTime": "2026-11-05T09:00:00+09:00"}, "end": {"dateTime": "2026-11-05T10:00:00+09:00"}}]}))
        rows = google.list_events("2026-10-01", "2026-11-30")
        self.assertEqual(rows[0], {"uid": "e1", "title": "한빛정밀 5차 회의", "date": "2026-10-30", "time": "14:00", "end_date": "2026-10-30",
                                   "end_time": "15:30", "location": "창원공장", "all_day": False, "link": "https://cal/e1"})  # UTC → 서울
        self.assertEqual((rows[1]["date"], rows[1]["end_date"], rows[1]["time"], rows[1]["all_day"]), ("2026-11-02", "2026-11-03", "", True))  # 끝은 마지막 날로
        self.assertEqual((rows[2]["title"], rows[2]["time"]), ("(제목 없음)", "09:00"))
        self.assertEqual(len(rows), 3)  # 취소된 일정은 뺀다
        q1 = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(self.calls[1]["url"]).query))
        q2 = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(self.calls[2]["url"]).query))
        self.assertEqual((q1["timeMin"], q1["timeMax"], q1["singleEvents"], q1["orderBy"]),
                         ("2026-10-01T00:00:00+09:00", "2026-11-30T23:59:59+09:00", "true", "startTime"))
        self.assertEqual(q2["pageToken"], "p2")
        self.assertIn("/calendars/primary/events", self.calls[1]["url"])
        self.assertEqual(self.calls[1]["headers"]["Authorization"], "Bearer at-1")

    def test_insert_event_timed_and_all_day(self):
        self.connect()
        self.replies.append((200, {"id": "new1", "htmlLink": "https://cal/new1"}))
        out = google.insert_event("[회의] 한빛정밀 5차", "2026-10-30", "14:00", duration_min=90, location="창원공장", description="P001")
        self.assertEqual(out, {"id": "new1", "link": "https://cal/new1"})
        body = json.loads(self.calls[-1]["data"].decode("utf-8"))
        self.assertEqual(body["summary"], "[회의] 한빛정밀 5차")
        self.assertEqual(body["start"], {"dateTime": "2026-10-30T14:00:00", "timeZone": "Asia/Seoul"})
        self.assertEqual(body["end"], {"dateTime": "2026-10-30T15:30:00", "timeZone": "Asia/Seoul"})
        self.assertEqual((body["location"], body["description"], self.calls[-1]["method"]), ("창원공장", "P001", "POST"))

        self.replies.append((200, {"id": "new2", "htmlLink": ""}))
        google.insert_event("[기한] 품의서 제출", "2026-10-20")
        body = json.loads(self.calls[-1]["data"].decode("utf-8"))
        self.assertEqual((body["start"], body["end"]), ({"date": "2026-10-20"}, {"date": "2026-10-21"}))  # 종일: 끝은 다음 날
        self.assertNotIn("location", body)

    def test_delete_event_treats_missing_as_done_and_reports_other_errors(self):
        self.connect()
        self.replies.append((410, {"error": {"message": "Resource has been deleted", "status": "GONE"}}))
        google.delete_event("e1")
        self.assertEqual(self.calls[-1]["method"], "DELETE")
        self.replies.append((403, {"error": {"message": "Insufficient Permission", "status": "PERMISSION_DENIED"}}))
        with self.assertRaisesRegex(RuntimeError, "Insufficient Permission"):
            google.delete_event("e2")

    # ---- 드라이브 ----
    def test_ensure_folder_reuses_or_creates(self):
        self.connect()
        self.replies.append((200, {"files": [{"id": "f-old", "name": "Sales.AX 회의록"}]}))
        self.assertEqual(google.ensure_folder("Sales.AX 회의록"), "f-old")
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(self.calls[-1]["url"]).query))["q"]
        self.assertIn("name = 'Sales.AX 회의록'", q)
        self.assertIn(google.FOLDER_MIME, q)
        self.replies += [(200, {"files": []}), (200, {"id": "f-new"})]
        self.assertEqual(google.ensure_folder("김's 폴더"), "f-new")
        self.assertIn("김\\'s 폴더", dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(self.calls[-2]["url"]).query))["q"])  # 따옴표를 막는다
        self.assertEqual(json.loads(self.calls[-1]["data"].decode("utf-8")), {"name": "김's 폴더", "mimeType": google.FOLDER_MIME})

    def test_upload_builds_multipart_related_body(self):
        self.connect()
        self.replies.append((200, {"id": "d1", "webViewLink": "https://drive/d1"}))
        html = "<html><body><h1>회의록</h1></body></html>".encode("utf-8")
        out = google.upload("P001 4차 회의록", html, "text/html", folder_id="f-1", convert_to_doc=True)
        self.assertEqual(out, {"id": "d1", "link": "https://drive/d1"})
        call = self.calls[-1]
        self.assertIn("uploadType=multipart", call["url"])
        ctype = call["headers"]["Content-Type"]
        self.assertTrue(ctype.startswith("multipart/related; boundary="))
        boundary = ctype.split("boundary=")[1].encode()
        parts = call["data"].split(b"--" + boundary)
        self.assertEqual((parts[0], parts[-1]), (b"", b"--\r\n"))
        meta_head, meta_body = parts[1].strip(b"\r\n").split(b"\r\n\r\n", 1)
        self.assertIn(b"application/json", meta_head)
        self.assertEqual(json.loads(meta_body.decode("utf-8")), {"name": "P001 4차 회의록", "parents": ["f-1"], "mimeType": google.DOC_MIME})
        file_head, file_body = parts[2].split(b"\r\n\r\n", 1)
        self.assertIn(b"Content-Type: text/html", file_head)
        self.assertEqual(file_body[:-2], html)  # 내용은 그대로, 뒤의 줄바꿈만 뗀다

        self.replies.append((200, {"id": "d2", "webViewLink": ""}))
        google.upload("명함.png", b"\x89PNG\r\n--data", "image/png")
        meta = json.loads(self.calls[-1]["data"].split(b"\r\n\r\n", 2)[1].split(b"\r\n--")[0].decode("utf-8"))
        self.assertEqual(meta, {"name": "명함.png"})  # 폴더·변환을 주지 않으면 이름만

    def test_api_errors_carry_googles_message(self):
        self.connect()
        self.replies.append((403, {"error": {"code": 403, "message": "Calendar API has not been used in project 1 before or it is disabled.", "status": "PERMISSION_DENIED"}}))
        with self.assertRaisesRegex(RuntimeError, "Calendar API has not been used") as ctx:
            google.list_events("2026-10-01", "2026-10-31")
        self.assertEqual((ctx.exception.code, ctx.exception.error), (403, "PERMISSION_DENIED"))


if __name__ == "__main__":
    unittest.main()
