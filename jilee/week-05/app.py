"""로컬 테스트 앱: python app.py → http://localhost:8765

표준 라이브러리 http.server만 사용한다. 화면에서 딜·회차를 고르고 녹취를 붙여넣어
에이전트를 실행하면 도구 호출 과정과 생성된 회의록을 그대로 보여준다.
"""

from __future__ import annotations

import base64
import html
import hmac
import ipaddress
import json
import os
import re
import secrets
import socket
import sys
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from urllib.parse import parse_qs, quote, urlsplit
from urllib.request import Request, urlopen

from agent import chat, crawl, gcal, google, memory, quality, sheet, store, tools
from agent.llm import LLMClient, RateLimited, load_env, save_env
from agent.loop import run_agent

ROOT = Path(__file__).resolve().parent
load_env()
# 서버에 올릴 때: HOST=0.0.0.0, APP_PASSWORD=(접속 비밀번호). 로컬에서는 둘 다 비워 둔다.
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8765"))
PASSWORD = os.environ.get("APP_PASSWORD", "")
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}

# ---- API 연동 탭: LLM 제공자 설정 조회·저장·연결 테스트 ----
ENV_KEYS = {
    "primary": ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"),
    "fallback": ("LLM_FALLBACK_BASE_URL", "LLM_FALLBACK_API_KEY", "LLM_FALLBACK_MODEL"),
}
PING_TOOL = [{"type": "function", "function": {
    "name": "ping", "description": "연결 확인용 도구. 반드시 한 번 호출한다.",
    "parameters": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}}}]


def provider_view(slot: str) -> dict:
    """화면에 내려주는 설정. 키 자체는 내려보내지 않고 끝 4자리만 알려준다."""
    url, key, model = (os.environ.get(k, "") for k in ENV_KEYS[slot])
    return {"base_url": url, "model": model, "key_set": bool(key), "key_hint": key[-4:] if len(key) >= 12 else ""}


def _key_for(k_url: str, k_key: str, url: str, new_key: str) -> str:
    """새 키가 없으면 저장된 키를 쓰되, 주소가 저장된 것과 다르면 쓰지 않는다(저장된 키가 다른 서버로 나가지 않게)."""
    if new_key:
        return new_key
    if url.rstrip("/") != os.environ.get(k_url, "").rstrip("/"):
        raise ValueError("제공자 주소를 바꿨으면 그 제공자의 API 키를 새로 입력해야 합니다.")
    return os.environ.get(k_key, "")


def save_config(req: dict) -> None:
    updates = {}
    for slot, (k_url, k_key, k_model) in ENV_KEYS.items():
        p = req.get(slot) or {}
        if p.get("base_url", "").strip():
            _key_for(k_url, k_key, p["base_url"].strip(), (p.get("api_key") or "").strip())
        if "base_url" in p:
            updates[k_url] = p["base_url"].strip().rstrip("/")
        if "model" in p:
            updates[k_model] = p["model"].strip()
        if (p.get("api_key") or "").strip():  # 키 칸을 비워 두면 저장된 키를 그대로 둔다
            updates[k_key] = p["api_key"].strip()
    if any("\n" in v or "\r" in v for v in updates.values()):
        raise ValueError("값에 줄바꿈을 넣을 수 없습니다.")
    k_url, _, k_model = ENV_KEYS["primary"]
    if any(k in updates and updates[k] != os.environ.get(k, "") for k in (k_url, k_model)):
        updates["LLM_MAX_INPUT_TOKENS"] = ""  # 1순위가 바뀌면 배워 둔 요청 크기 한도는 맞지 않는다. 다시 배운다
    save_env(updates)


def test_provider(req: dict) -> dict:
    """화면에 적힌 값(비어 있으면 저장된 값)으로 짧은 요청을 한 번 보내 키·모델·도구 호출 지원을 확인한다."""
    k_url, k_key, k_model = ENV_KEYS[req.get("slot", "primary")]
    p = req.get("provider") or {}
    url = (p.get("base_url") or "").strip() or os.environ.get(k_url, "")
    model = (p.get("model") or "").strip() or os.environ.get(k_model, "")
    try:
        key = _key_for(k_url, k_key, url, (p.get("api_key") or "").strip())
    except ValueError as e:
        return {"ok": False, "message": str(e)}
    if not (url and key and model):
        return {"ok": False, "message": "주소·API 키·모델 세 값이 모두 있어야 합니다."}
    client = LLMClient(url, key, model)
    started = time.time()
    try:
        msg = client._post(client.primary, [{"role": "user", "content": "ping 도구를 text='ok'로 한 번 호출해줘."}], PING_TOOL)
    except RateLimited as e:
        return {"ok": False, "message": f"한도 초과(429): {e.detail[:300]}"}
    except Exception as e:
        return {"ok": False, "message": str(e)[:400]}
    u = client.usage_log[-1]
    return {"ok": True, "tool_call": bool(msg.get("tool_calls")), "model": u["model"], "sec": round(time.time() - started, 1),
            "tokens": u["prompt_tokens"] + u["completion_tokens"]}


def sheet_view(customer_id: str, project_id: str) -> dict:
    """결과지 화면에 필요한 것을 한 번에: 고객·프로젝트·우리 쪽 담당·고객 측 담당자(연락처)·결과지·판정 재료."""
    d = store.project_dir(customer_id, project_id)
    st = memory.latest(store.key(customer_id, project_id), d)
    cust, proj = store.load_customer(customer_id), store.load_project(customer_id, project_id)
    st["sheet"].setdefault("deal_hints", {})
    timeline = []  # 회의록이 확정될 때마다 결과지가 얼마나 찼는지(회차별 추이)
    for n in store.meeting_nos(customer_id, project_id):
        snap = memory._read_json(memory.state_path(d, n))
        if snap:
            timeline.append({"no": n, "date": snap["meeting_dates"].get(str(n), ""), **sheet.completeness(snap["sheet"], proj.get("deal"))})
    return {
        "completeness": sheet.completeness(st["sheet"], proj.get("deal")), "timeline": timeline,
        "customer": cust, "project": proj,
        "team": store.team(customer_id, project_id),
        "sheet": st["sheet"], "gate": sheet.gate(st["sheet"]),
        "items": {c: {"group": g, "label": l, "hint": h, "kind": "text" if c in sheet.TEXT_ITEMS else "derived"}
                  for c, (g, l, h) in sheet.ITEMS.items()},
        "gate_codes": sheet.GATE, "response_status": sheet.RESPONSE_STATUS,
        "deal": proj.get("deal") or {}, "deal_fields": store.DEAL_FIELDS, "deal_axes": store.DEAL_AXES, "deal_multi": store.DEAL_MULTI, "deal_events": store.DEAL_EVENTS, "gate_codes": sheet.GATE, "response_status": sheet.RESPONSE_STATUS,
        "extras": sheet.required_extras(proj.get("deal")),
        "meetings": [tools.meeting_view(customer_id, project_id, n) for n in store.meeting_nos(customer_id, project_id)],
        "action_items": st["action_items"], "open_issues": st["open_issues"], "meeting_no": st["meeting_no"],
    }


def projects_view() -> list[dict]:
    """홈의 프로젝트 카드: 고객·프로젝트마다 확정 항목 수와 가장 최근 회의가 서 있는 문."""
    rows = []
    for c in store.list_tree():
        for p in c["projects"]:
            d = store.project_dir(c["customer_id"], p["project_id"])
            st = memory.latest(store.key(c["customer_id"], p["project_id"]), d)
            last = max(p["meetings"] or [0])
            rows.append({"customer_id": c["customer_id"], "customer": c["name"], "project_id": p["project_id"], "project": p["name"],
                         "stage": p["stage"], "meetings": len(p["meetings"]), "gate": sheet.gate(st["sheet"]),
                         "completeness": sheet.completeness(st["sheet"], store.load_project(c["customer_id"], p["project_id"]).get("deal")),
                         "last": tools.meeting_view(c["customer_id"], p["project_id"], last) if last else None,
                         "open_actions": sum(1 for a in st["action_items"] if a["status"] != "완료")})
    return rows


# ---- 캘린더: 우리 일정(회의 · 액션 기한 · 계약 예상) + 구글 캘린더 -------------------
# 구글 쪽은 캘린더 설정의 '비공개 주소(iCal 형식)'를 읽는다(읽기 전용, 로그인 절차 없음).
# 우리 일정은 /calendar.ics 로 내보내고, 구글 캘린더에서 'URL로 추가'하면 구독된다(외부에서 닿는 주소일 때).
_google_cache = {"at": 0.0, "url": "", "rows": [], "error": None}


def google_events() -> tuple[list[dict], str | None]:
    """구글 일정. 화면을 열 때마다 구글에 묻지 않도록 5분 동안 기억한다."""
    url = os.environ.get("GOOGLE_ICAL_URL", "")
    if not url:
        return [], None
    if _google_cache["url"] != url or time.time() - _google_cache["at"] > 300:
        try:
            _google_cache.update(at=time.time(), url=url, rows=gcal.fetch_google(url), error=None)
        except Exception as e:
            _google_cache.update(at=time.time(), url=url, rows=[], error=str(e)[:200])
    return _google_cache["rows"], _google_cache["error"]


def feed_token() -> str:
    """내보내기 주소에 붙는 토큰. 구글은 비밀번호를 넣을 수 없어 주소 자체가 열쇠가 된다. 처음 한 번 만들어 .env에 둔다."""
    if not os.environ.get("CALENDAR_FEED_TOKEN"):
        save_env({"CALENDAR_FEED_TOKEN": secrets.token_urlsafe(18)})
    return os.environ["CALENDAR_FEED_TOKEN"]


def calendar_view() -> dict:
    """구글에 로그인돼 있으면 그 캘린더를 읽고, 아니면 비공개 iCal 주소를 읽는다. 둘 다 없으면 우리 일정만."""
    ours, how, error, rows = gcal.events(), "", None, []
    actions = {}  # 액션 일정에 id와 지금 상태를 붙인다(캘린더에서 바로 수행 기록을 남기게)
    for c in store.list_tree():
        for p in c["projects"]:
            st = memory.latest(store.key(c["customer_id"], p["project_id"]), store.project_dir(c["customer_id"], p["project_id"]))
            actions.update({(c["customer_id"], p["project_id"], a["task"]): a for a in st["action_items"]})
    for e in ours:
        a = actions.get((e.get("customer_id"), e.get("project_id"), e.get("title"))) if e["kind"] == "action" else None
        if a:
            e["action_id"], e["status"] = a["id"], a["status"]
    if google.status()["connected"]:
        how = "login"
        try:
            now = time.time()
            if _google_cache["url"] != "login" or now - _google_cache["at"] > 300:
                day = lambda offset: time.strftime("%Y-%m-%d", time.localtime(now + offset * 86400))
                _google_cache.update(at=now, url="login", rows=google.list_events(day(-60), day(180)), error=None)
            rows = _google_cache["rows"]
        except Exception as e:
            error = str(e)[:200]
    elif os.environ.get("GOOGLE_ICAL_URL"):
        how = "ical"
        rows, error = google_events()
    return {"events": gcal.merge(ours, rows),
            "google": {"connected": bool(how), "how": how, "error": error, "count": len(rows), "account": google.status()["account"]},
            "feed_path": f"/calendar.ics?token={feed_token()}"}


_oauth = {"state": "", "redirect": ""}  # 구글 로그인을 시작할 때 만든 값. 돌아왔을 때 같은지 본다


def import_public_transcript(raw_url: str) -> str:
    """로그인 없이 읽을 수 있는 공개 STT 텍스트/HTML에서 본문을 가져온다."""
    raw_url = (raw_url or "").strip()
    parsed = urlsplit(raw_url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("http 또는 https 공개 링크를 입력하세요.")

    def check_host(host: str) -> None:
        for info in socket.getaddrinfo(host, None):
            addr = ipaddress.ip_address(info[4][0])
            if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved or addr.is_multicast:
                raise ValueError("내부 네트워크 주소는 가져올 수 없습니다.")

    check_host(parsed.hostname)
    request = Request(raw_url, headers={"User-Agent": "INTERX-Meeting-Agent/1.0", "Accept": "text/plain,text/html,application/json"})
    with urlopen(request, timeout=12) as response:
        final = urlsplit(response.geturl())
        if final.hostname:
            check_host(final.hostname)
        data = response.read(3 * 1024 * 1024 + 1)
        if len(data) > 3 * 1024 * 1024:
            raise ValueError("STT 원문은 3MB 이하만 가져올 수 있습니다.")
        content_type = response.headers.get_content_type()
        charset = response.headers.get_content_charset() or "utf-8"
    text = data.decode(charset, errors="replace")
    if content_type == "application/json":
        obj = json.loads(text)
        if isinstance(obj, dict):
            text = next((str(obj[k]) for k in ("transcript", "text", "content", "body") if obj.get(k)), text)
    elif content_type == "text/html" or "<html" in text[:500].lower():
        text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", text)
        text = re.sub(r"(?s)<[^>]+>", "\n", text)
        text = html.unescape(text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) < 30:
        raise ValueError("링크에서 읽을 수 있는 회의 원문을 찾지 못했습니다. 원문 붙여넣기를 이용하세요.")
    return text


def find_photos(customer_id: str, project_id: str, meeting_no: int | None = None) -> dict:
    """결과지에 넣을 사진을 찾는다. 그 회사와 직접 관련된 것만: 회사 기사(제목에 회사 이름, 최근 1년)의 사진과 회사 홈페이지 사진.

    주제가 비슷한 다른 회사의 사진은 자동으로 넣지 않는다. 없으면 비워 둔다.
    """
    from agent import photos
    if meeting_no:
        raise ValueError("회의록의 사진은 그 회의에서 올린 사진이나 직접 고른 사진만 넣습니다.")
    cust, proj = store.load_customer(customer_id), store.load_project(customer_id, project_id)
    items = (cust.get("news") or {}).get("items")
    if items is None:
        items = photos.news(cust["name"])
    rows = photos.company_photos(items)
    rows += photos.homepage_photos(cust.get("web"), 3 - len(rows)) if len(rows) < 3 else []
    proj["참고 사진"] = photos.clean(rows, "자동 수집")
    proj.pop("사진 검색어", None)
    store.save_project(customer_id, project_id, proj)
    return {"photos": proj["참고 사진"], "queries": []}


def post_misc(path: str, req: dict) -> dict:
    """본문이 JSON인 자잘한 쓰기 요청들. 실패는 예외로 올리고 부른 쪽이 400으로 돌려준다."""
    who = req.get("who") or "담당자"
    ids = (req.get("customer_id", ""), req.get("project_id", ""))
    if path == "/api/action":  # 액션아이템 수행 기록
        return {"action": tools.set_action(*ids, req["id"], req["status"], who, req.get("note", ""))}
    if path == "/api/meeting/scope":  # 외부 회의인지 내부 회의인지
        return {"scope": store.set_meeting_scope(*ids, int(req["meeting_no"]), req["scope"])["scope"]}
    if path == "/api/sheet/edit":  # 사람이 결과지 값을 직접 고친다
        tools.edit_sheet(*ids, req["kind"], req.get("key", ""), req.get("fields") or {}, who)
        return sheet_view(*ids)
    if path == "/api/project/decision":  # 추진 전환 판정
        return {"rows": store.add_decision(*ids, req.get("result", ""), req.get("reason", ""), who)}
    if path in ("/api/project/photos", "/api/meeting/photos") and req.get("action") == "find":
        return find_photos(*ids, int(req["meeting_no"]) if path == "/api/meeting/photos" else None)
    if path == "/api/meeting/photos":  # 회의마다 따로 두는 참고 사진
        from agent import photos
        m = store.load_meeting(*ids, int(req["meeting_no"]))
        m["참고 사진"] = photos.clean(req.get("photos"), who)
        store.save_meeting(*ids, int(req["meeting_no"]), m)
        return {"photos": m["참고 사진"]}
    if path == "/api/project/photos":  # 참고 사진. 사람이 골라 넣는다
        from agent import photos
        proj = store.load_project(*ids)
        proj["참고 사진"] = photos.clean(req.get("photos"), who)
        store.save_project(*ids, proj)
        return {"photos": proj["참고 사진"]}
    if path == "/api/project/inquiry":  # 고객이 보낸 문의 글. 사람이 붙여넣는다
        proj = store.load_project(*ids)
        proj["문의 원문"] = (req.get("text") or "").strip()
        store.save_project(*ids, proj)
        return {"text": proj["문의 원문"]}
    if path in ("/api/customer", "/api/customer/enrich"):
        cust = store.load_customer(req["customer_id"])
        if "homepage" in req:
            cust["homepage"] = (req["homepage"] or "").strip()
        if "news_keep" in req and cust.get("news"):  # 관계없는 기사를 사람이 지운다
            cust["news"]["items"] = [n for n in cust["news"]["items"] if n["link"] in set(req["news_keep"])]
        notes = []
        if path == "/api/customer/enrich":  # 밖에서 가져올 수 있는 것을 한 번에: 홈페이지 소개 · 뉴스 · 참고 사진. 출처와 함께 보이고 확인 전 값이다
            from agent import photos
            if cust.get("homepage"):
                try:
                    cust["web"] = {**crawl.enrich(cust), "fetched_by": who}
                    notes.append("홈페이지 소개")
                except Exception as e:
                    notes.append(f"홈페이지 실패({e})")
            try:
                hint = (re.findall(r"[가-힣A-Za-z]{2,}", cust.get("업종", "")) or [""])[0]
                found = photos.news(cust["name"], hint)
                cust["news"] = {"items": found, "fetched_at": time.strftime("%Y-%m-%d %H:%M"), "fetched_by": who}
                notes.append(f"뉴스 {len(found)}건")
            except Exception as e:
                notes.append(f"뉴스 실패({e})")
            if req.get("project_id"):
                proj = store.load_project(*ids)
                if not [r for r in proj.get("참고 사진", []) if r.get("added_by") != "자동 수집"]:  # 사람이 고른 사진이 있으면 건드리지 않는다
                    try:
                        notes.append(f"회사 사진 {len(find_photos(*ids)['photos'])}장")
                    except Exception as e:
                        notes.append(f"사진 실패({e})")
        store.save_customer(req["customer_id"], cust)
        return {"homepage": cust.get("homepage", ""), "web": cust.get("web"), "news": cust.get("news"), "notes": notes}
    if path == "/api/google/config":  # 구글 클라우드에서 만든 OAuth 클라이언트. 보안 비밀은 비워 두면 그대로 둔다
        updates = {"GOOGLE_CLIENT_ID": (req.get("client_id") or "").strip()}
        if (req.get("client_secret") or "").strip():
            updates["GOOGLE_CLIENT_SECRET"] = req["client_secret"].strip()
        save_env(updates)
        return {"google": google.status()}
    if path == "/api/google/disconnect":
        google.disconnect()
        _google_cache["at"] = 0.0
        return {"google": google.status()}
    if path == "/api/google/calendar":  # 회의를 구글 캘린더에 올린다. 같은 회의를 두 번 올리지 않는다
        n = int(req["meeting_no"])
        m = store.load_meeting(*ids, n)
        if m.get("google_event"):
            return {"link": m["google_event"].get("link", ""), "already": True}
        if not m.get("date"):
            raise ValueError("날짜가 없는 회의는 올릴 수 없습니다.")
        cust, proj = store.load_customer(ids[0]), store.load_project(*ids)
        m["google_event"] = google.insert_event(f"[{m['scope']} 회의] {cust['name']} {proj['name']} {n}차", m["date"], m.get("time", ""),
                                                location=m.get("place", ""), description=m.get("type", ""))
        store.save_meeting(*ids, n, m)
        _google_cache["at"] = 0.0
        return {"link": m["google_event"].get("link", "")}
    if path == "/api/google/drive":  # 화면의 문서를 구글 문서로 올린다(이 앱이 만든 폴더에)
        folder = google.ensure_folder("Sales.AX 회의 문서")
        return google.upload(req["name"], req["html"].encode("utf-8"), "text/html", folder, convert_to_doc=True)
    raise KeyError(path)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # 요청 로그를 조용히
        pass

    def _authorized(self) -> bool:
        """APP_PASSWORD가 있으면 모든 요청에 HTTP 기본 인증을 요구한다(아이디는 아무 값)."""
        if not PASSWORD:
            return True
        try:
            scheme, token = self.headers.get("Authorization", "").split(" ", 1)
            given = base64.b64decode(token).decode("utf-8").split(":", 1)[1]
        except Exception:
            scheme, given = "", ""
        if scheme == "Basic" and hmac.compare_digest(given.encode("utf-8"), PASSWORD.encode("utf-8")):
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="meeting-agent"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    # ---- 응답 도우미 ----
    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, status: int = 200) -> None:
        self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    # ---- 라우팅 ----
    def do_GET(self):
        url = urlsplit(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        if url.path == "/calendar.ics":  # 구글 캘린더가 구독하는 주소. 비밀번호 대신 주소의 토큰으로 막는다
            if not hmac.compare_digest(q.get("token", ""), feed_token()):
                self._send(403, b"forbidden", "text/plain")
            else:
                self._send(200, gcal.feed_ics().encode("utf-8"), "text/calendar; charset=utf-8")
            return
        if not self._authorized():
            return
        ids = (q.get("customer_id", ""), q.get("project_id", ""))
        try:
            if url.path == "/":
                self._send(200, (ROOT / "static" / "index.html").read_bytes(), "text/html; charset=utf-8")
            elif url.path == "/static/interx-logo-white.png" and (ROOT / "static" / "interx-logo-white.png").exists():  # 로고 그림은 저장소에 없다. 없으면 화면이 글자로 보인다
                self._send(200, (ROOT / "static" / "interx-logo-white.png").read_bytes(), "image/png")
            elif url.path == "/static/meeting-agent-bg-v1.png":
                self._send(200, (ROOT / "static" / "meeting-agent-bg-v1.png").read_bytes(), "image/png")
            elif url.path in ("/static/assistant-hero-3d-v1.png", "/static/home-hero-3d-v1.png", "/static/meetings-hero-3d-v1.png", "/static/graph-hero-3d-v1.png"):
                self._send(200, (ROOT / "static" / url.path.rsplit("/", 1)[1]).read_bytes(), "image/png")
            elif url.path in ("/static/graph.js", "/static/charts.js"):
                self._send(200, (ROOT / "static" / url.path.rsplit("/", 1)[1]).read_bytes(), "text/javascript; charset=utf-8")
            elif url.path == "/api/meeting/agenda":  # 지난 회의록에서 뽑은 다음 회의 안건 초안
                self._json(tools.draft_agenda(*ids) if q.get("ai") else tools.suggest_agenda(*ids))
            elif url.path == "/api/photos/search":  # 참고 사진 찾기(위키미디어 공용)
                from agent import photos
                term = q.get("q", "")  # 한글이면 최근 1년 기사에 실린 사진, 영어면 위키미디어 공용
                self._json(photos.news_photos(term) if re.search(r"[가-힣]", term) else photos.search(term))
            elif url.path == "/api/photos/page":    # 사람이 준 주소의 페이지에 실린 사진
                from agent import photos
                self._json(photos.from_page(q.get("url", "")))
            elif url.path == "/api/google/status":
                self._json(google.status())
            elif url.path == "/oauth/google/start":  # 구글 로그인으로 보낸다. 돌아올 주소는 지금 접속한 주소 그대로
                proto = self.headers.get("X-Forwarded-Proto", "http")
                _oauth.update(state=secrets.token_urlsafe(16), redirect=f"{proto}://{self.headers.get('Host')}/oauth/google/callback")
                self.send_response(302)
                self.send_header("Location", google.auth_url(_oauth["redirect"], _oauth["state"]))
                self.end_headers()
            elif url.path == "/oauth/google/callback":
                note = "denied"
                if q.get("code") and _oauth["state"] and hmac.compare_digest(q.get("state", ""), _oauth["state"]):
                    try:
                        google.exchange(q["code"], _oauth["redirect"])
                        note = "ok"
                    except Exception as e:
                        note = "error:" + quote(str(e)[:160])
                _oauth["state"] = ""
                _google_cache["at"] = 0.0
                self.send_response(302)
                self.send_header("Location", f"/?google={note}#settings")
                self.end_headers()
            elif url.path == "/api/graph":  # 노드 그래프: 일의 흐름(회의 → 회의록 → 결과지)과 사람·액션·요구사항의 관계
                from agent import flow
                self._json(flow.graph(*ids))
            elif url.path == "/api/files":
                from agent import files
                n = q.get("meeting_no", "all")
                self._json(files.listing(*ids, "all" if n == "all" else None if n in ("", "project") else int(n)))
            elif url.path == "/api/file":  # 첨부파일 내려받기
                from agent import files
                n = q.get("meeting_no", "")
                p = files.path(*ids, None if n in ("", "project") else int(n), q["name"])
                self.send_response(200)
                self.send_header("Content-Type", files.content_type(p.name))
                self.send_header("Content-Length", str(p.stat().st_size))
                self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(p.name))
                self.end_headers()
                self.wfile.write(p.read_bytes())
            elif url.path == "/api/minutes/diff":
                from agent import edit
                self._json({"diff": edit.diff_from_agent(*ids, int(q["meeting_no"]))})
            elif url.path == "/api/tree":  # 고객 → 프로젝트 → 회차
                self._json(store.list_tree())
            elif url.path == "/api/org":
                self._json(store.load_org())
            elif url.path == "/api/config":
                self._json({"primary": provider_view("primary"), "fallback": provider_view("fallback"),
                            "masking": os.environ.get("MASKING", "on").lower() != "off",
                            "max_input": int(os.environ.get("LLM_MAX_INPUT_TOKENS") or 0)})
            elif url.path == "/api/sheet":
                self._json(sheet_view(*ids))
            elif url.path == "/api/meetings":  # 전체 회의 목록(고객·프로젝트를 가로질러)과 게이트 상태
                self._json(tools.all_meetings())
            elif url.path == "/api/projects":
                self._json(projects_view())
            elif url.path == "/api/calendar":
                self._json(calendar_view())
            elif url.path == "/api/calendar/config":
                u = os.environ.get("GOOGLE_ICAL_URL", "")
                self._json({"connected": bool(u), "hint": u[-6:] if u else "", "feed_path": f"/calendar.ics?token={feed_token()}"})
            elif url.path == "/api/meeting":
                self._json(tools.meeting_view(*ids, int(q["meeting_no"])))
            elif url.path == "/api/quality":  # 회의 품질과 회의록 품질(코드로 잰 것). 확정 전에 무엇을 다시 볼지 알려 준다
                self._json(quality.report(*ids, int(q["meeting_no"])))
            elif url.path == "/api/transcript":  # 화면에는 항상 원문을 준다(구간 정리는 에이전트가 읽을 때만)
                p = store.transcript_path(*ids, int(q["meeting_no"]))
                self._json({"text": p.read_text(encoding="utf-8")} if p.exists() else {"error": "저장된 녹취가 없습니다."}, 200 if p.exists() else 404)
            elif url.path == "/api/minutes":
                p = store.minutes_path(*ids, int(q["meeting_no"]))
                self._json({"markdown": p.read_text(encoding="utf-8")} if p.exists() else {"error": "아직 생성된 회의록이 없습니다."}, 200 if p.exists() else 404)
            else:
                self._send(404, b"not found", "text/plain")
        except (FileNotFoundError, ValueError, KeyError) as e:
            self._json({"error": str(e)}, 404)

    def do_POST(self):
        if not self._authorized():
            return
        url = urlsplit(self.path)
        length = int(self.headers.get("Content-Length", 0))
        if url.path == "/api/file":  # 첨부파일 올리기: 본문이 파일 그 자체다(이름과 붙일 곳은 주소에)
            try:
                from agent import files
                q = {k: v[0] for k, v in parse_qs(url.query).items()}
                if length > 20 * 1024 * 1024:
                    raise ValueError("한 파일은 20MB까지 올릴 수 있습니다.")
                n = q.get("meeting_no", "")
                saved = files.save(q["customer_id"], q["project_id"], None if n in ("", "project") else int(n), q["name"],
                                   self.rfile.read(length), q.get("who") or "담당자", q.get("note", ""))
                self._json({"ok": True, "file": saved})
            except Exception as e:
                self._json({"ok": False, "message": str(e)[:300]}, 400)
            return
        misc = ("/api/meeting/photos", "/api/project/photos", "/api/sheet/edit", "/api/project/decision", "/api/action", "/api/meeting/scope", "/api/project/inquiry", "/api/customer", "/api/customer/enrich",
                "/api/google/config", "/api/google/disconnect", "/api/google/calendar", "/api/google/drive")
        if self.path not in misc + ("/api/run", "/api/transcript/import", "/api/config", "/api/config/test", "/api/sheet/confirm", "/api/minutes/edit", "/api/file/delete",
                                    "/api/minutes/confirm", "/api/meeting", "/api/project/deal", "/api/chat", "/api/calendar/config"):
            self._send(404, b"not found", "text/plain")
            return
        req = json.loads(self.rfile.read(length).decode("utf-8"))
        if self.path == "/api/transcript/import":
            try:
                self._json({"ok": True, "text": import_public_transcript(req.get("url", ""))})
            except Exception as e:
                self._json({"ok": False, "message": str(e)[:300]}, 400)
            return
        if self.path in misc:
            try:
                self._json({"ok": True, **post_misc(self.path, req)})
            except Exception as e:
                self._json({"ok": False, "message": str(e)[:300]}, 400)
            return
        if self.path in ("/api/minutes/edit", "/api/file/delete"):
            try:
                from agent import edit, files
                ids = (req["customer_id"], req["project_id"])
                if self.path == "/api/minutes/edit":  # 담당자가 회의록 초안을 직접 고친다(확정 전에만)
                    self._json(edit.save_minutes_edit(*ids, int(req["meeting_no"]), req.get("markdown", ""), req.get("who") or "담당자"))
                else:
                    n = req.get("meeting_no")
                    files.remove(*ids, None if n in (None, "", "project") else int(n), req["name"], req.get("who") or "담당자")
                    self._json({"ok": True})
            except Exception as e:
                self._json({"ok": False, "message": str(e)[:300]}, 400)
            return
        if self.path == "/api/chat":  # 전역 채팅: 읽기 전용 도구만 쓴다. 바꾸는 일은 문을 거쳐야 한다
            try:
                self._json({"ok": True, **chat.answer(req.get("messages") or [], req.get("customer_id") or None,
                                                      req.get("project_id") or None, req.get("meeting_no") or None)})
            except Exception as e:
                self._json({"ok": False, "message": str(e)[:400]}, 400)
            return
        if self.path == "/api/calendar/config":  # 구글 캘린더의 비공개 iCal 주소를 저장한다. 저장 전에 실제로 읽어 본다
            try:
                url = (req.get("url") or "").strip()
                count = len(gcal.fetch_google(url)) if url else 0
                save_env({"GOOGLE_ICAL_URL": url})
                _google_cache["at"] = 0.0
                self._json({"ok": True, "connected": bool(url), "count": count})
            except Exception as e:
                self._json({"ok": False, "message": str(e)[:300]}, 400)
            return
        if self.path in ("/api/minutes/confirm", "/api/meeting", "/api/project/deal"):
            try:
                ids = (req["customer_id"], req["project_id"])
                if self.path == "/api/minutes/confirm":  # 두 번째 게이트: 담당자가 회의록을 확정한다
                    out = {"gate": tools.confirm_minutes(*ids, int(req["meeting_no"]), req.get("who") or "담당자")}
                elif self.path == "/api/meeting":        # 다가올 회의 등록
                    for c in req.get("new_contacts") or []:  # 등록 창에서 새로 적은 사람은 담당자 목록에도 남긴다
                        if str(c.get("name", "")).strip():
                            store.upsert_contact(ids[0], {"name": str(c["name"]).strip()[:20], "title": str(c.get("title", "")).strip()[:30]}, "회의 등록")
                    for m in req.get("new_members") or []:
                        if str(m.get("name", "")).strip():
                            store.add_member(str(m["name"]).strip()[:20], str(m.get("org", ""))[:30])
                    out = {"meeting_no": store.register_meeting(*ids, req.get("date", ""), req.get("time", ""), req.get("type", ""), req.get("place", ""),
                                                                req.get("scope") or "외부", req.get("attendees"), req.get("agenda", ""), req.get("links"))}
                else:                                     # 거래 조건은 사람이 정한다
                    out = {"deal": store.set_deal(*ids, req.get("field", ""), req.get("value", ""))}
                self._json({"ok": True, **out})
            except Exception as e:
                self._json({"ok": False, "message": str(e)}, 400)
            return
        if self.path == "/api/sheet/confirm":  # 사람이 AI 초안을 확인한다. 확인된 것만 확정 항목으로 센다
            try:
                n = tools.confirm_sheet(req["customer_id"], req["project_id"], req["kind"], req.get("target", ""), req.get("who") or "담당자")
                self._json({"ok": True, "confirmed": n, **sheet_view(req["customer_id"], req["project_id"])})
            except Exception as e:
                self._json({"ok": False, "message": str(e)}, 400)
            return
        if self.path == "/api/config":
            try:
                save_config(req)
                self._json({"ok": True, "primary": provider_view("primary"), "fallback": provider_view("fallback")})
            except Exception as e:
                self._json({"ok": False, "message": str(e)}, 400)
            return
        if self.path == "/api/config/test":
            self._json(test_provider(req))
            return
        try:
            customer_id, project_id, meeting_no = req["customer_id"], req["project_id"], int(req["meeting_no"])
            # 화면에서 수정한 녹취가 있으면 파일에 반영한 뒤 실행한다(에이전트는 항상 파일을 읽는다)
            if req.get("transcript") is not None:
                store.transcript_path(customer_id, project_id, meeting_no).write_text(req["transcript"], encoding="utf-8")
        except (FileNotFoundError, ValueError, KeyError) as e:
            self._json({"error": str(e)}, 400)
            return

        # 진행 상황을 단계마다 흘려보낸다(Server-Sent Events). 화면은 이 줄을 받는 즉시 그린다.
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        def emit(obj: dict) -> None:
            self.wfile.write(("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode("utf-8"))
            self.wfile.flush()

        emit({"type": "start", "message": "에이전트 시작 — 모델에 첫 요청을 보냈습니다"})
        try:
            r = run_agent(customer_id, project_id, meeting_no, on_step=lambda e: emit({"type": "step", "entry": e}), options=req.get("options"))
        except Exception as e:
            emit({"type": "error", "message": str(e)})
            return
        try:  # 회의록 확정 단계는 없다. 만들어지면 바로 프로젝트 기억과 결과지에 반영한다
            tools.confirm_minutes(customer_id, project_id, meeting_no, "자동 반영")
        except FileNotFoundError:
            pass
        p = store.minutes_path(customer_id, project_id, meeting_no)
        emit({
            "type": "done",
            "final_answer": r.final_answer,
            "saved_path": r.saved_path,
            "markdown": p.read_text(encoding="utf-8") if p.exists() else "",
        })


def main() -> None:
    local = HOST in LOCAL_HOSTS
    if not local and not PASSWORD:
        # 외부에서 접속할 수 있게 열면 누구나 API 키를 바꾸고 무료 한도를 쓸 수 있다
        sys.exit("외부 접속(HOST)을 열려면 .env에 APP_PASSWORD를 함께 설정해야 합니다.")
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    url = f"http://localhost:{PORT}"
    print(f"회의록 에이전트 {'로컬 앱' if local else '서버'}: {url if local else f'{HOST}:{PORT}'}  (종료: Ctrl+C)")
    if local and "--no-browser" not in sys.argv:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
