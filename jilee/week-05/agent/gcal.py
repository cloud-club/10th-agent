"""캘린더: 우리 쪽 일정(회의 · 액션아이템 기한 · 계약 예상시기)을 모으고, 구글 캘린더와 iCal(.ics)로 주고받는다.

  내보내기  feed_ics()      우리 일정을 .ics로. 구글 캘린더의 "URL로 추가"에 이 주소를 넣으면 구독된다
  가져오기  fetch_google()  구글 캘린더의 "비공개 iCal 주소"를 읽어 일정 목록으로
  합치기    merge()         구글 일정을 우리 일정 옆에 붙이고, 같은 날짜·시각의 회의는 이미 등록된 것으로 표시

OAuth 없이 iCal 주소만 쓴다. 그래서 읽기는 되지만 구글 캘린더에 일정을 써넣지는 못한다(구독으로 대신한다).
시각은 모두 서울 시간이다. 서울은 서머타임이 없어 고정 +9시간으로 계산한다.
"""

from __future__ import annotations

import hashlib
import re
import urllib.request
from datetime import date, datetime, timedelta, timezone

from . import dates, memory, store, tools

KST = timezone(timedelta(hours=9))
GOOGLE = "https://calendar.google.com/"  # 이 주소로 시작하는 것만 받는다(임의 주소로 요청을 보내지 않게)
LABEL = {"meeting": "회의", "action": "기한", "contract": "계약 예상"}


# ---- iCal 읽기 ---------------------------------------------------------------

def _unfold(text: str) -> list[str]:
    """75자에서 접힌 줄(다음 줄이 공백으로 시작)을 한 줄로 편다."""
    out: list[str] = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line[:1] in (" ", "\t") and out:
            out[-1] += line[1:]
        else:
            out.append(line)
    return out


def _split(line: str) -> tuple[str, dict, str]:
    """'이름;매개변수=값:내용' → (이름, 매개변수, 내용). 따옴표 안의 쌍점은 구분자가 아니다."""
    quoted = False
    for i, ch in enumerate(line):
        if ch == '"':
            quoted = not quoted
        elif ch == ":" and not quoted:
            break
    else:
        return "", {}, ""
    name, *rest = line[:i].split(";")
    params = {k.upper(): v.strip('"') for k, _, v in (p.partition("=") for p in rest)}
    return name.upper(), params, line[i + 1:]


def _unescape(s: str) -> str:
    return re.sub(r"\\([\\;,nN])", lambda m: "\n" if m.group(1) in "nN" else m.group(1), s)


def _when(params: dict, value: str) -> tuple[str, str, bool]:
    """DTSTART·DTEND 값을 (날짜, 시각, 종일인가)로. 시각은 서울 시간으로 맞춘다."""
    v = value.strip()
    if re.fullmatch(r"\d{8}", v):
        return f"{v[:4]}-{v[4:6]}-{v[6:8]}", "", True
    m = re.fullmatch(r"(\d{8})T(\d{2})(\d{2})(\d{2})?(Z?)", v)
    if not m:
        return "", "", False
    dt = datetime.strptime(m.group(1) + m.group(2) + m.group(3), "%Y%m%d%H%M")
    if m.group(5):  # UTC
        dt = dt.replace(tzinfo=timezone.utc).astimezone(KST)
    elif params.get("TZID") and params["TZID"] != "Asia/Seoul":
        try:
            from zoneinfo import ZoneInfo
            dt = dt.replace(tzinfo=ZoneInfo(params["TZID"])).astimezone(KST)
        except Exception:  # 이 PC에 시간대 정보가 없으면 적힌 시각을 그대로 둔다
            pass
    return dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M"), False


def parse_ics(text: str) -> list[dict]:
    """.ics 글에서 일정을 읽는다. 반복 일정(RRULE)은 첫 회만 넣고 recurring=True를 붙인다."""
    events, cur, nested = [], None, 0
    for line in _unfold(text):
        name, params, value = _split(line)
        kind = value.strip().upper()
        if name == "BEGIN" and kind == "VEVENT":
            cur, nested = {}, 0
        elif name == "BEGIN" and cur is not None:
            nested += 1  # 일정 안의 알림(VALARM) 등은 건너뛴다
        elif name == "END" and kind == "VEVENT":
            if cur is not None and "DTSTART" in cur and "RECURRENCE-ID" not in cur:  # 반복 일정의 개별 수정분은 뺀다
                ev = _event(cur)
                if ev:
                    events.append(ev)
            cur = None
        elif name == "END" and cur is not None:
            nested = max(0, nested - 1)
        elif cur is not None and name and not nested:
            cur.setdefault(name, (params, value))
    return sorted(events, key=lambda e: (e["date"], e["time"]))


def _event(cur: dict) -> dict | None:
    d, t, all_day = _when(*cur["DTSTART"])
    if not d:
        return None
    end_d, end_t = d, t
    if "DTEND" in cur:
        ed, et, _ = _when(*cur["DTEND"])
        if ed and all_day:  # 종일 일정의 끝은 '다음 날'로 적혀 있다. 마지막 날로 고친다
            ed = max(d, (date.fromisoformat(ed) - timedelta(days=1)).isoformat())
        end_d, end_t = ed or d, et if ed else t
    text = lambda key: _unescape(cur[key][1]).strip() if key in cur else ""
    ev = {"uid": text("UID"), "title": text("SUMMARY"), "date": d, "time": t, "end_date": end_d, "end_time": end_t,
          "location": text("LOCATION"), "all_day": all_day}
    if "RRULE" in cur:
        ev["recurring"] = True
    return ev


class _OnlyGoogle(urllib.request.HTTPRedirectHandler):
    """넘겨주기(redirect)로 다른 곳에 가지 않게 한다."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.startswith(GOOGLE):
            raise ValueError("구글 캘린더가 아닌 주소로 넘어가려 해서 멈췄습니다.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_google(url: str, timeout: int = 15) -> list[dict]:
    """구글 캘린더의 iCal 주소를 읽는다. 주소는 구글 캘린더 설정의 '비공개 iCal 주소'에서 복사한다."""
    url = (url or "").strip()
    if url.startswith("webcal://"):
        url = "https://" + url[len("webcal://"):]
    if not url.startswith(GOOGLE):
        raise ValueError(f"구글 캘린더의 iCal 주소({GOOGLE}…)만 넣을 수 있습니다.")
    req = urllib.request.Request(url, headers={"User-Agent": "meeting-agent/0.1"})
    with urllib.request.build_opener(_OnlyGoogle).open(req, timeout=timeout) as resp:
        return parse_ics(resp.read().decode("utf-8", errors="replace"))


# ---- 우리 쪽 일정 --------------------------------------------------------------

def _row(kind: str, when: dict, c: dict, p: dict, **kw) -> dict:
    return {"date": when["date"], "time": kw.pop("time", ""), "kind": kind, "approx": when["approx"], "raw": when["raw"],
            "customer_id": c["customer_id"], "customer": c["name"], "project_id": p["project_id"], "project": p["name"], **kw}


def events() -> list[dict]:
    """회의, 끝나지 않은 액션아이템의 기한, 계약 예상시기를 한 목록으로. 날짜를 못 읽은 기한은 date=None으로 뒤에 둔다."""
    rows = []
    for m in tools.all_meetings():
        if m["date"]:
            rows.append({"date": m["date"], "time": m["time"], "kind": "meeting", "approx": False, "raw": "",
                         "title": f"{m['no']}차 회의" + (f" · {m['type']}" if m["type"] else ""),
                         "customer_id": m["customer_id"], "customer": m["customer"], "project_id": m["project_id"], "project": m["project"],
                         "no": m["no"], "state": m["gate"]["state"], "detail": m["place"]})
    for c in store.list_tree():
        for p in c["projects"]:
            cid, pid = c["customer_id"], p["project_id"]
            st = memory.latest(store.key(cid, pid), store.project_dir(cid, pid))  # 회의록이 확정된 것만

            def day_of(n) -> date | None:  # 그 회차가 열린 날. 말로 한 기한은 이 날을 기준으로 푼다
                text = st["meeting_dates"].get(str(n)) or store.load_meeting(cid, pid, n)["date"] if n else ""
                try:
                    return date.fromisoformat(text)
                except (TypeError, ValueError):
                    return None

            for a in st["action_items"]:
                if a["status"] == "완료":
                    continue
                base = day_of(a["meeting"])
                when = dates.resolve(a["due"], base) if base else {"date": None, "approx": False, "raw": a["due"]}
                rows.append(_row("action", when, c, p, title=a["task"], no=a["meeting"], state=a["status"], detail=a["owner"]))
            b4 = st["sheet"]["items"].get("B4")
            base = day_of(st["meeting_no"])
            if b4 and base:
                when = dates.resolve(b4["value"], base)
                if when["date"]:
                    rows.append(_row("contract", when, c, p, title=b4["value"], no=b4.get("updated"), state=b4.get("status", ""), detail=""))
    return sorted(rows, key=lambda r: (r["date"] is None, r["date"] or "", r["time"] or ""))


def _clock(text: str) -> str:
    """'9:00' → '09:00'. 시각이 아니면 빈 글."""
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", text or "")
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m and int(m.group(1)) < 24 and int(m.group(2)) < 60 else ""


def merge(ours: list[dict], google: list[dict]) -> list[dict]:
    """구글 일정을 kind="google"로 붙인다. 우리 회의와 날짜·시각이 같으면 matched에 그 회의를 적는다(이미 등록된 회의로 본다).

    종일 일정은 시각이 없어 맞춰 보지 않는다(공휴일 같은 것이 회의로 잘못 묶이지 않게).
    """
    slots = {(r["date"], _clock(r["time"])): r for r in ours if r["kind"] == "meeting" and _clock(r["time"])}
    out = list(ours)
    for g in google:
        row = {"date": g["date"], "time": g["time"], "kind": "google", "title": g["title"], "approx": False, "raw": "",
               "customer_id": "", "customer": "", "project_id": "", "project": "", "no": None, "state": "",
               "detail": g.get("location", ""), "uid": g.get("uid", ""), "all_day": g.get("all_day", False),
               "end_date": g.get("end_date", g["date"]), "end_time": g.get("end_time", "")}
        if g.get("recurring"):
            row["recurring"] = True
        hit = slots.get((g["date"], _clock(g["time"])))
        if hit:
            row["matched"] = {"customer_id": hit["customer_id"], "project_id": hit["project_id"], "no": hit["no"]}
        out.append(row)
    return sorted(out, key=lambda r: (r["date"] is None, r["date"] or "", r["time"] or ""))


# ---- iCal 쓰기 ---------------------------------------------------------------

def _escape(s: str) -> str:
    return (s or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\r", "").replace("\n", "\\n")


def _fold(line: str) -> str:
    """한 줄을 75바이트에서 접는다. 한글이 가운데서 잘리지 않게 글자 단위로 센다."""
    out, cur = [], ""
    for ch in line:
        if len((cur + ch).encode("utf-8")) > (74 if out else 75):  # 이어지는 줄은 앞 공백 한 칸만큼 덜 쓴다
            out.append(cur)
            cur = ch
        else:
            cur += ch
    return "\r\n ".join(out + [cur])


def _uid(r: dict) -> str:
    """다시 내보내도 같은 일정은 같은 UID를 갖는다(구글 캘린더가 중복으로 넣지 않게)."""
    tail = r["no"] if r["kind"] == "meeting" else hashlib.sha1(f"{r.get('no')}|{r['title']}".encode("utf-8")).hexdigest()[:10]
    return f"{r['kind']}-{r['customer_id']}-{r['project_id']}-{tail}@sales-ax"


def feed_ics(rows: list[dict] | None = None) -> str:
    """우리 일정을 .ics로. 날짜를 못 읽은 것과 구글에서 온 것은 뺀다. 시각이 있으면 한 시간짜리, 없으면 종일 일정이다."""
    rows = events() if rows is None else rows
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//sales-ax//meeting-agent//KO", "CALSCALE:GREGORIAN",
             "X-WR-CALNAME:Sales AX 회의·기한", "X-WR-TIMEZONE:Asia/Seoul"]
    for r in rows:
        if not r.get("date") or r["kind"] not in LABEL:
            continue
        day, clock = date.fromisoformat(r["date"]), _clock(r.get("time", ""))
        who = f"{r['customer']} {r['project']}".strip()
        summary = (f"[회의] {who} {r['no']}차" if r["kind"] == "meeting"
                   else f"[{LABEL[r['kind']]}] {who} — {r['title']}") + (" (어림)" if r.get("approx") else "")
        note = " · ".join(x for x in (r.get("state"), r.get("detail") if r["kind"] != "meeting" else "",
                                      f"원문: {r['raw']}" if r.get("raw") else "") if x)
        lines += ["BEGIN:VEVENT", f"UID:{_uid(r)}", f"DTSTAMP:{day.strftime('%Y%m%d')}T000000Z"]
        if clock:  # 서울 시각을 UTC로 바꿔 적는다(시간대 정의 없이도 어느 캘린더에서나 같은 시각으로 읽힌다)
            start = datetime.fromisoformat(f"{r['date']}T{clock}").replace(tzinfo=KST).astimezone(timezone.utc)
            lines += [f"DTSTART:{start.strftime('%Y%m%dT%H%M%SZ')}", f"DTEND:{(start + timedelta(hours=1)).strftime('%Y%m%dT%H%M%SZ')}"]
        else:
            lines += [f"DTSTART;VALUE=DATE:{day.strftime('%Y%m%d')}", f"DTEND;VALUE=DATE:{(day + timedelta(days=1)).strftime('%Y%m%d')}"]
        lines.append(f"SUMMARY:{_escape(summary)}")
        if r["kind"] == "meeting" and r.get("detail"):
            lines.append(f"LOCATION:{_escape(r['detail'])}")
        if note:
            lines.append(f"DESCRIPTION:{_escape(note)}")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(l) for l in lines) + "\r\n"
