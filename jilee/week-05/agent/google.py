"""구글 로그인(OAuth) 방식 연동: 캘린더 읽기·쓰기와 드라이브 올리기 (표준 라이브러리만 사용).

iCal 주소 방식(gcal.fetch_google)은 읽기만 되고 드라이브가 없다. 여기서는 사용자가 구글 계정으로 한 번 로그인해
받은 리프레시 토큰으로, 캘린더에 일정을 직접 써넣고 회의록·결과지를 드라이브에 올린다.

  설정(.env)  GOOGLE_CLIENT_ID · GOOGLE_CLIENT_SECRET   구글 클라우드 콘솔에서 만든 OAuth 클라이언트(사람이 넣는다)
              GOOGLE_REFRESH_TOKEN · GOOGLE_ACCOUNT      로그인하면 이 모듈이 적는다
  범위        openid email · calendar.events(일정 읽기·쓰기) · drive.file(이 앱이 만든 파일만. 사용자의 다른 파일은 못 본다)
  흐름        auth_url() → 구글 동의 화면 → 리디렉션 주소로 code → exchange() → 이후 access_token()이 알아서 갱신

모든 HTTP는 _request 하나를 거친다. 테스트는 이 함수만 가로채면 실제 구글에 닿지 않는다.
토큰과 클라이언트 비밀값은 어떤 함수도 돌려주지 않는다(status()는 연결 여부와 계정 메일만 알려준다).
"""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import date, datetime, timedelta, timezone

from . import llm

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
CALENDAR_API = "https://www.googleapis.com/calendar/v3"
DRIVE_API = "https://www.googleapis.com/drive/v3"
DRIVE_UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
SCOPES = ["openid", "email", "https://www.googleapis.com/auth/calendar.events", "https://www.googleapis.com/auth/drive.file"]
FOLDER_MIME = "application/vnd.google-apps.folder"
DOC_MIME = "application/vnd.google-apps.document"
KST = timezone(timedelta(hours=9))

_token = {"value": "", "until": 0.0}  # 액세스 토큰은 메모리에만 둔다(한 시간짜리라 저장할 까닭이 없다)


class GoogleError(RuntimeError):
    """구글이 돌려준 오류. code는 HTTP 상태, error는 구글의 오류 이름(invalid_grant 등)."""

    def __init__(self, message: str, code: int = 0, error: str = ""):
        super().__init__(message)
        self.code, self.error = code, error


# ---- HTTP (여기 한 곳만 바깥으로 나간다) ---------------------------------------

def _request(method: str, url: str, *, headers: dict | None = None, data: bytes | None = None, timeout: int = 20) -> tuple[int, bytes]:
    """요청을 보내고 (상태 코드, 본문)을 돌려준다. 4xx·5xx도 예외로 올리지 않고 그대로 돌려준다(판단은 _call이 한다)."""
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _call(method: str, url: str, *, headers: dict | None = None, data: bytes | None = None, ok_codes: tuple = ()) -> dict:
    """_request를 부르고 JSON으로 푼다. 4xx·5xx는 구글의 오류 메시지를 담아 올린다."""
    status, body = _request(method, url, headers=headers, data=data)
    try:
        parsed = json.loads(body.decode("utf-8")) if body else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        parsed = {}
    if status >= 400 and status not in ok_codes:
        err = parsed.get("error") if isinstance(parsed, dict) else None
        if isinstance(err, dict):  # API 오류: {"error": {"message", "status"}}
            name, message = err.get("status", ""), err.get("message", "")
        else:                      # 로그인 오류: {"error": "invalid_grant", "error_description": "..."}
            name, message = str(err or ""), parsed.get("error_description", "") if isinstance(parsed, dict) else ""
        raise GoogleError(f"구글 요청 실패 ({status}{' ' + name if name else ''}): {message or body[:200].decode('utf-8', 'replace')}", status, name)
    return parsed if isinstance(parsed, dict) else {}


def _form(fields: dict) -> bytes:
    return urllib.parse.urlencode(fields).encode("utf-8")


def _api(method: str, url: str, body: dict | None = None, *, ok_codes: tuple = ()) -> dict:
    """로그인한 계정으로 구글 API를 부른다."""
    headers = {"Authorization": f"Bearer {access_token()}"}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json; charset=utf-8"
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    return _call(method, url, headers=headers, data=data, ok_codes=ok_codes)


# ---- 로그인 -------------------------------------------------------------------

def _env(key: str) -> str:
    return os.environ.get(key, "").strip()


def status() -> dict:
    """화면에 알려줄 것: 클라이언트가 설정됐는지, 로그인됐는지, 어느 계정인지. 비밀값은 돌려주지 않는다."""
    return {"configured": bool(_env("GOOGLE_CLIENT_ID") and _env("GOOGLE_CLIENT_SECRET")),
            "connected": bool(_env("GOOGLE_REFRESH_TOKEN")), "account": _env("GOOGLE_ACCOUNT")}


def _need_client() -> tuple[str, str]:
    cid, secret = _env("GOOGLE_CLIENT_ID"), _env("GOOGLE_CLIENT_SECRET")
    if not (cid and secret):
        raise RuntimeError("구글 OAuth 클라이언트가 설정되지 않았습니다. .env에 GOOGLE_CLIENT_ID와 GOOGLE_CLIENT_SECRET을 넣으세요.")
    return cid, secret


def auth_url(redirect_uri: str, state: str) -> str:
    """구글 동의 화면의 주소. state는 돌아왔을 때 우리가 보낸 요청이 맞는지 맞춰 보는 값이다(부르는 쪽이 만들고 확인한다)."""
    cid, _ = _need_client()
    return AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": cid, "redirect_uri": redirect_uri, "response_type": "code", "scope": " ".join(SCOPES),
        "access_type": "offline",           # 리프레시 토큰을 받는다
        "prompt": "consent",                # 이미 동의한 계정이어도 동의 화면을 다시 띄워야 리프레시 토큰이 온다
        "include_granted_scopes": "true", "state": state})


def _email_of(id_token: str) -> str:
    """id_token의 가운데 토막(payload)에서 메일을 읽는다.
    서명은 검증하지 않는다: 방금 구글의 토큰 주소에서 TLS로 직접 받은 값이라 그대로 믿는다."""
    try:
        payload = id_token.split(".")[1]
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)).decode("utf-8"))
        return str(data.get("email", ""))
    except (IndexError, ValueError, UnicodeDecodeError):
        return ""


def exchange(code: str, redirect_uri: str) -> dict:
    """동의 화면에서 돌아온 code를 토큰으로 바꾸고, 리프레시 토큰과 계정 메일을 .env에 적는다."""
    cid, secret = _need_client()
    tok = _call("POST", TOKEN_URL, headers={"Content-Type": "application/x-www-form-urlencoded"},
                data=_form({"code": code, "client_id": cid, "client_secret": secret, "redirect_uri": redirect_uri,
                            "grant_type": "authorization_code"}))
    if not tok.get("refresh_token"):
        raise RuntimeError("구글이 리프레시 토큰을 주지 않았습니다. 이미 동의한 계정이라 그럴 수 있습니다. "
                           "구글 계정의 앱 권한(myaccount.google.com/permissions)에서 이 앱을 지운 뒤 다시 로그인하세요.")
    account = _email_of(tok.get("id_token", ""))
    llm.save_env({"GOOGLE_REFRESH_TOKEN": tok["refresh_token"], "GOOGLE_ACCOUNT": account})
    _token.update(value=tok.get("access_token", ""), until=time.time() + int(tok.get("expires_in", 0)))
    return {"connected": True, "account": account}


def _forget() -> None:
    _token.update(value="", until=0.0)
    llm.save_env({"GOOGLE_REFRESH_TOKEN": "", "GOOGLE_ACCOUNT": ""})


def access_token() -> str:
    """API를 부를 때 쓰는 액세스 토큰. 만료 60초 전까지는 기억해 둔 것을 쓰고, 그 뒤에는 리프레시 토큰으로 새로 받는다."""
    if _token["value"] and time.time() < _token["until"] - 60:
        return _token["value"]
    refresh = _env("GOOGLE_REFRESH_TOKEN")
    if not refresh:
        raise RuntimeError("구글 계정이 연결되지 않았습니다. 연동 화면에서 구글로 로그인하세요.")
    cid, secret = _need_client()
    try:
        tok = _call("POST", TOKEN_URL, headers={"Content-Type": "application/x-www-form-urlencoded"},
                    data=_form({"client_id": cid, "client_secret": secret, "refresh_token": refresh, "grant_type": "refresh_token"}))
    except GoogleError as e:
        if e.error == "invalid_grant":  # 사용자가 권한을 지웠거나 토큰이 만료됐다. 남겨 두면 매번 실패하므로 지운다
            _forget()
            raise RuntimeError("구글 연결이 끊겼습니다. 연동 화면에서 다시 로그인하세요.") from e
        raise
    _token.update(value=tok["access_token"], until=time.time() + int(tok.get("expires_in", 3600)))
    return _token["value"]


def disconnect() -> None:
    """연결을 끊는다. 구글에 토큰 폐기를 알리고(실패해도 계속), 저장해 둔 값을 지운다."""
    refresh = _env("GOOGLE_REFRESH_TOKEN")
    if refresh:
        try:
            _request("POST", REVOKE_URL, headers={"Content-Type": "application/x-www-form-urlencoded"}, data=_form({"token": refresh}))
        except Exception:  # 네트워크가 끊겨 있어도 우리 쪽 연결은 끊는다
            pass
    _forget()


# ---- 캘린더 -------------------------------------------------------------------

def _rfc3339(value: str, end: bool = False) -> str:
    """'2026-10-01' 같은 날짜만 오면 서울 시각의 하루 시작(끝)으로 바꾼다. 이미 시각이 있으면 그대로 둔다."""
    if len(value) == 10:
        return f"{value}T{'23:59:59' if end else '00:00:00'}+09:00"
    return value


def _seoul(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=KST)
    try:
        from zoneinfo import ZoneInfo
        return dt.astimezone(ZoneInfo("Asia/Seoul"))
    except Exception:  # 이 PC에 시간대 정보가 없으면 +9시간으로 셈한다
        return dt.astimezone(KST)


def _event(item: dict) -> dict:
    """구글 일정 하나를 gcal.parse_ics와 같은 모양으로. 눌러서 열 주소(link)를 더한다."""
    start, end = item.get("start") or {}, item.get("end") or {}
    if "date" in start:  # 종일 일정. 끝은 '다음 날'로 오므로 마지막 날로 고친다
        d, t, all_day = start["date"], "", True
        end_d = (date.fromisoformat(end["date"]) - timedelta(days=1)).isoformat() if end.get("date") else d
        end_d, end_t = max(end_d, d), ""
    else:
        s = _seoul(start.get("dateTime", ""))
        e = _seoul(end["dateTime"]) if end.get("dateTime") else s
        d, t, all_day = s.strftime("%Y-%m-%d"), s.strftime("%H:%M"), False
        end_d, end_t = e.strftime("%Y-%m-%d"), e.strftime("%H:%M")
    return {"uid": item.get("id", ""), "title": item.get("summary", "") or "(제목 없음)", "date": d, "time": t,
            "end_date": end_d, "end_time": end_t, "location": item.get("location", ""), "all_day": all_day,
            "link": item.get("htmlLink", "")}


def _calendar(calendar_id: str) -> str:
    return f"{CALENDAR_API}/calendars/{urllib.parse.quote(calendar_id, safe='')}/events"


def list_events(time_min: str, time_max: str, calendar_id: str = "primary") -> list[dict]:
    """기간 안의 일정. 반복 일정은 회차마다 풀어서(singleEvents) 시작 순으로 받는다. 취소된 일정은 뺀다."""
    out, page = [], ""
    while True:
        q = {"timeMin": _rfc3339(time_min), "timeMax": _rfc3339(time_max, end=True), "singleEvents": "true",
             "orderBy": "startTime", "maxResults": "250"}
        if page:
            q["pageToken"] = page
        data = _api("GET", _calendar(calendar_id) + "?" + urllib.parse.urlencode(q))
        out += [_event(i) for i in data.get("items", []) if i.get("status") != "cancelled" and (i.get("start") or {})]
        page = data.get("nextPageToken", "")
        if not page:
            return out


def insert_event(title: str, date: str, time: str = "", duration_min: int = 60, location: str = "", description: str = "",
                 calendar_id: str = "primary") -> dict:
    """일정을 써넣는다. time이 없으면 종일 일정이다. 시각은 서울 기준."""
    if time:
        start = datetime.fromisoformat(f"{date}T{time}:00" if len(time) == 5 else f"{date}T{time}")
        when = {"start": {"dateTime": start.isoformat(), "timeZone": "Asia/Seoul"},
                "end": {"dateTime": (start + timedelta(minutes=duration_min)).isoformat(), "timeZone": "Asia/Seoul"}}
    else:
        day = datetime.fromisoformat(date).date()
        when = {"start": {"date": day.isoformat()}, "end": {"date": (day + timedelta(days=1)).isoformat()}}  # 끝은 다음 날로 적는다
    body = {"summary": title, **when}
    if location:
        body["location"] = location
    if description:
        body["description"] = description
    made = _api("POST", _calendar(calendar_id), body)
    return {"id": made.get("id", ""), "link": made.get("htmlLink", "")}


def delete_event(event_id: str, calendar_id: str = "primary") -> None:
    """일정을 지운다. 이미 없는 일정(404·410)은 지워진 것으로 본다."""
    _api("DELETE", f"{_calendar(calendar_id)}/{urllib.parse.quote(event_id, safe='')}", ok_codes=(404, 410))


# ---- 드라이브 -----------------------------------------------------------------

def ensure_folder(name: str) -> str:
    """이 앱이 만든 같은 이름의 폴더가 있으면 그 id, 없으면 만들어 그 id. (drive.file 범위라 앱이 만든 것만 보인다)"""
    safe = name.replace("\\", "\\\\").replace("'", "\\'")
    q = f"name = '{safe}' and mimeType = '{FOLDER_MIME}' and trashed = false"
    found = _api("GET", f"{DRIVE_API}/files?" + urllib.parse.urlencode({"q": q, "fields": "files(id,name)", "spaces": "drive"}))
    if found.get("files"):
        return found["files"][0]["id"]
    return _api("POST", f"{DRIVE_API}/files?fields=id", {"name": name, "mimeType": FOLDER_MIME})["id"]


def upload(name: str, data: bytes, mime: str, folder_id: str | None = None, convert_to_doc: bool = False) -> dict:
    """파일을 올린다. convert_to_doc이면 HTML을 구글 문서로 바꿔 올린다(회의록·결과지를 드라이브에서 바로 고칠 수 있게)."""
    meta: dict = {"name": name}
    if folder_id:
        meta["parents"] = [folder_id]
    if convert_to_doc:
        meta["mimeType"] = DOC_MIME
    boundary = "salesax" + uuid.uuid4().hex
    body = b"".join([
        f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode("utf-8"),
        json.dumps(meta, ensure_ascii=False).encode("utf-8"),
        f"\r\n--{boundary}\r\nContent-Type: {mime}\r\n\r\n".encode("utf-8"),
        data,
        f"\r\n--{boundary}--\r\n".encode("utf-8"),
    ])
    made = _call("POST", DRIVE_UPLOAD + "?uploadType=multipart&fields=id,webViewLink",
                 headers={"Authorization": f"Bearer {access_token()}", "Content-Type": f"multipart/related; boundary={boundary}"}, data=body)
    return {"id": made.get("id", ""), "link": made.get("webViewLink", "")}
