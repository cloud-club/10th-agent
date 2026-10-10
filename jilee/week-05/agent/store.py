"""고객 → 프로젝트 저장 구조와 기준 정보(고객 측 담당자 연락처 · 우리 조직).

  customers/{고객코드}/customer.json              고객 기본정보 · 프로필 · 고객 측 담당자(연락처)
  customers/{고객코드}/projects/{프로젝트코드}/     project.json · transcript_NN.txt · minutes_NN.md · state/
  org.json                                        우리 조직 구성원(사번 · 조직 · 연락처)

한 고객 아래에 프로젝트가 여러 건 달린다. 고객 측 담당자와 우리 조직 구성원은 한 곳에만 두고,
프로젝트와 결과지는 이름·id(contact_id · member_id)로 가리킨다. CRM·인사 시스템과 맞출 때는
이 파일의 load_* / save_* 함수만 그쪽 API 호출로 바꾼다.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CUSTOMERS_DIR = ROOT / "customers"
ORG_FILE = ROOT / "org.json"
PROFILE_FIELDS = ["생산품", "주요 설비", "기존 시스템"]  # 고객 단위로 쌓는 사실(프로젝트가 달라도 같다)
# 프로젝트의 거래 조건. 계약 사실이라 값은 사람이 정한다(에이전트는 회의에서 단서가 나오면 제안만).
# 사내 분류가 없어 법령·계약예규의 분류에 맞췄다(조사 2026-10-10, 근거는 줄 끝).
#   분류 축       : 축마다 하나만 고른다. 한 축 안의 값은 서로 겹치지 않는다
#   여러 개 고르는 축 : 한 계약에 섞이는 것(계약 목적물)
#   조건부로 벌어지는 일 : 해당하는 건에서만 생긴다. 서로 독립이라 여럿이 함께 켜질 수 있다
DEAL_AXES = {
    "사업 유형": ["민간 자체 재원",                    # 법정 분류 없음(사인 간 계약)
              "정부지원사업(보조금 + 자부담)",     # 보조금법 제2조 · 스마트제조혁신법 제2조 7호(공급기업)
              "국가연구개발과제",                  # 국가연구개발혁신법 제2조 1호(계약이 아니라 협약)
              "공공조달",                          # 국가계약법 제7조 · SW진흥법 제51조①
              "그 밖"],
    "계약 방법": ["수의계약(직접 협의)",               # 국가계약법 제7조① 단서
              "경쟁입찰",                          # 국가계약법 제7조①(일반 · 제한 · 지명). 가격 중심
              "협상에 의한 계약(제안서 평가)",     # 국가계약법 시행령 제43조
              "과제 공모(협약)",                   # 국가연구개발혁신법
              "그 밖"],
    "계약 지위": ["원도급", "하도급", "재하도급"],     # SW진흥법 제2조 14 · 15호, 제51조③④
    "공동계약 방식": ["단독",
                "공동이행(연대 책임)",             # (계약예규) 공동계약운용요령 제2조의2 1호
                "분담이행(맡은 부분만 책임)",      # 같은 조 2호. 3호 주계약자관리방식은 건설공사 전용이라 뺌
                "그 밖"],
}
DEAL_MULTI = {
    "계약 목적물": ["용역(개발·구축)",                 # 하도급법 제2조⑪ 용역위탁
               "상용SW(라이선스)",                 # SW진흥법 제2조 11호 · 제54조②
               "물품(HW·설비)",                    # SW진흥법 제51조① · 국가계약법 시행령 제16조
               "설치 공사",                        # 국가계약법 제25조①
               "유지관리"],                        # 법으로는 용역. 계약을 따로 맺는 일이 많아 뗌
}
EVENT_STATES = ["해당 없음", "필요", "완료"]
DEAL_EVENTS = {  # 이름: 근거
    "비밀유지계약 체결": "상생협력법 제21조의2 (SOP 04-13)",
    "공동수급협정서 체결": "공동계약운용요령 제2조 4호 (SOP 04-14)",
    "입찰·제안서 제출": "국가계약법 제7조 · 시행령 제43조 (SOP 04-15)",
    "시연·실증": "SOP 04-16",
    "거래상대방 확인": "국가계약법 제27조 (SOP 04-17)",
    "하도급 사전 승인": "SW진흥법 제51조⑤",
    "파트너사 하도급 계약": "하도급법 제2조 · SW진흥법 제51조①",
}
DEAL_FIELDS = {**DEAL_AXES, **DEAL_MULTI, **{name: EVENT_STATES for name in DEAL_EVENTS}}

_UTTER = re.compile(r"^([가-힣]{2,4})\s*:\s*(.+)$")


def _code(code, what: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", str(code or "")):
        raise ValueError(f"잘못된 {what}: {code}")
    return str(code)


def _read(path: Path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# ---- 경로 --------------------------------------------------------------------

def customer_dir(customer_id: str) -> Path:
    d = CUSTOMERS_DIR / _code(customer_id, "고객코드")
    if not d.is_dir():
        raise FileNotFoundError(f"고객 폴더가 없습니다: {customer_id}")
    return d


def project_dir(customer_id: str, project_id: str) -> Path:
    d = customer_dir(customer_id) / "projects" / _code(project_id, "프로젝트코드")
    if not d.is_dir():
        raise FileNotFoundError(f"프로젝트 폴더가 없습니다: {customer_id}/{project_id}")
    return d


def key(customer_id: str, project_id: str) -> str:
    """검색 색인·기억 파일에서 프로젝트를 가리키는 이름. 프로젝트코드는 고객 안에서만 유일하다."""
    return f"{customer_id}-{project_id}"


def transcript_path(customer_id: str, project_id: str, meeting_no: int) -> Path:
    return project_dir(customer_id, project_id) / f"transcript_{int(meeting_no):02d}.txt"


def minutes_path(customer_id: str, project_id: str, meeting_no: int) -> Path:
    return project_dir(customer_id, project_id) / f"minutes_{int(meeting_no):02d}.md"


# ---- 기준 정보 ----------------------------------------------------------------

def load_customer(customer_id: str) -> dict:
    data = _read(customer_dir(customer_id) / "customer.json", {})
    data.setdefault("customer_id", customer_id)
    data.setdefault("name", customer_id)
    data.setdefault("profile", {})
    data.setdefault("contacts", [])
    return data


def save_customer(customer_id: str, data: dict) -> None:
    _write(customer_dir(customer_id) / "customer.json", data)


def load_project(customer_id: str, project_id: str) -> dict:
    data = _read(project_dir(customer_id, project_id) / "project.json", {})
    data.setdefault("project_id", project_id)
    data.setdefault("name", project_id)
    data.setdefault("team", [])
    return data


def load_org() -> dict:
    return _read(ORG_FILE, {"members": []})


def add_member(name: str, org: str = "") -> dict:
    """조직 정보에 우리 쪽 사람을 더한다(같은 이름이 있으면 그대로 둔다)."""
    data = load_org()
    hit = next((m for m in data.setdefault("members", []) if m.get("name") == name), None)
    if hit is None:
        hit = {"id": f"E{len(data['members']) + 1:03d}", "name": name, "org": org.strip(), "position": "", "email": "", "phone": ""}
        data["members"].append(hit)
        _write(ORG_FILE, data)
    return hit


def team(customer_id: str, project_id: str) -> list[dict]:
    """프로젝트에 붙은 우리 쪽 사람. 조직 정보(org.json)의 사번으로 이름·조직·연락처를 끌어온다."""
    members = {m["id"]: m for m in load_org().get("members", [])}
    out = []
    for t in load_project(customer_id, project_id)["team"]:
        m = members.get(t.get("member_id"))
        out.append({**(m or {"name": t.get("name", ""), "unlinked": True}), "role": t.get("role", "")})
    return out


def list_tree() -> list[dict]:
    """고객 → 프로젝트 → 회차 목록(화면의 고르기 칸)."""
    tree = []
    if not CUSTOMERS_DIR.is_dir():
        return tree
    for c in sorted(p for p in CUSTOMERS_DIR.iterdir() if p.is_dir()):
        projects = []
        for p in sorted(q for q in (c / "projects").glob("*") if q.is_dir()):
            info = _read(p / "project.json", {})
            nos = lambda pattern: sorted(int(f.stem.split("_")[1]) for f in p.glob(pattern))
            projects.append({"project_id": p.name, "name": info.get("name", p.name), "stage": info.get("stage", ""),
                             "transcripts": nos("transcript_*.txt"), "minutes": nos("minutes_*.md"),
                             "meetings": sorted(set(nos("transcript_*.txt")) | set(nos("meeting_*.json")))})
        tree.append({"customer_id": c.name, "name": _read(c / "customer.json", {}).get("name", c.name), "projects": projects})
    return tree


def upsert_contact(customer_id: str, person: dict, source: str) -> dict:
    """고객 측 담당자를 이름으로 찾아 고치거나 새로 등록한다. 비어 있는 값으로는 덮어쓰지 않는다."""
    cust = load_customer(customer_id)
    hit = next((c for c in cust["contacts"] if c.get("name") == person["name"]), None)
    if hit is None:
        hit = {"id": f"CT{len(cust['contacts']) + 1:03d}", "name": person["name"], "source": source}
        cust["contacts"].append(hit)
    for f in ("dept", "title", "phone", "email"):
        if (person.get(f) or "").strip():
            hit[f] = person[f].strip()
    save_customer(customer_id, cust)
    return hit


def known_names(customer_id: str, project_id: str) -> list[str]:
    names = [c["name"] for c in load_customer(customer_id)["contacts"] if c.get("name")]
    names += [m["name"] for m in load_org().get("members", []) if m.get("name")]
    names += [t["name"] for t in team(customer_id, project_id) if t.get("name")]
    return sorted(set(names))


# ---- 에이전트에게 보여줄 글 ----------------------------------------------------

def render_customer(customer_id: str, project_id: str) -> str:
    """고객·프로젝트·담당자 요약. 전화번호와 메일 주소는 외부 LLM으로 보낼 까닭이 없어 넣지 않는다."""
    cust, proj = load_customer(customer_id), load_project(customer_id, project_id)
    person = lambda c: f"{c['name']}({' '.join(x for x in (c.get('dept'), c.get('title')) if x) or '직위 미확인'})"
    lines = [f"## 고객 — {cust['name']} ({customer_id})"]
    lines += [f"- {k}: {cust[k]}" for k in ("업종", "규모", "소재지") if cust.get(k)]
    lines.append("- 고객 프로필: " + " · ".join(
        f"{f} = {(cust['profile'].get(f) or {}).get('value') or '미확보'}" for f in PROFILE_FIELDS))
    lines.append("- 등록된 고객 측 담당자: " + (", ".join(person(c) for c in cust["contacts"]) or "없음"))
    others = [p for c in list_tree() if c["customer_id"] == customer_id for p in c["projects"] if p["project_id"] != project_id]
    if others:
        lines.append("- 이 고객의 다른 프로젝트: " + ", ".join(f"{p['project_id']} {p['name']}({p['stage'] or '단계 미정'})" for p in others))
    lines += ["", f"## 프로젝트 — {proj['name']} ({project_id})"]
    lines += [f"- {k}: {proj[k]}" for k in ("stage", "유입 경로", "관심 주제", "문의 원문") if proj.get(k)]
    lines.append("- 거래 조건(사람이 정함): " + " · ".join(f"{f} = {deal_text((proj.get('deal') or {}).get(f))}" for f in DEAL_FIELDS))
    mine = team(customer_id, project_id)
    lines.append("- 당사 담당: " + (", ".join(f"{t['name']}({' · '.join(x for x in (t.get('org'), t.get('role')) if x)})" for t in mine) or "미배정"))
    return "\n".join(lines).replace("- stage:", "- 단계:")


def numbered(text: str) -> str:
    """발언 줄 앞에 #번호를 붙인다. 에이전트가 근거를 번호로 가리키게 하려는 것이며, 번호는 utterances()의 pos와 같다."""
    out, pos = [], 0
    for line in text.splitlines():
        m = _UTTER.match(line.strip().lstrip("- "))
        if m and m.group(1) != "참석":
            pos += 1
            out.append(f"#{pos} {line.strip()}")
        else:
            out.append(line)
    return "\n".join(out)


def utterances(text: str) -> list[dict]:
    """녹취를 '이름: 발언' 줄 단위로. 결과지 근거를 원문 발화에 맞춰 붙일 때 쓴다."""
    out = []
    for line in text.splitlines():
        m = _UTTER.match(line.strip().lstrip("- "))
        if m and m.group(1) != "참석":
            out.append({"pos": len(out) + 1, "speaker": m.group(1), "text": m.group(2).strip()})
    return out


# ---- 회의 (게이트의 첫 문) -----------------------------------------------------

SCOPES = ("외부", "내부")  # 외부: 고객과의 회의 · 내부: 우리끼리의 회의(고객이 말한 사실을 새로 채우지 않는다)
_HEAD = re.compile(r"^\[(\d{4}-\d{2}-\d{2})(?:\s+(\d{1,2}:\d{2}))?\s*(?:/\s*([^/\]]+?))?\s*(?:/\s*([^\]]+?))?\s*\]")


def meeting_path(customer_id: str, project_id: str, meeting_no: int) -> Path:
    return project_dir(customer_id, project_id) / f"meeting_{int(meeting_no):02d}.json"


def meeting_nos(customer_id: str, project_id: str) -> list[int]:
    d = project_dir(customer_id, project_id)
    return sorted({int(f.stem.split("_")[1]) for pattern in ("transcript_*.txt", "meeting_*.json") for f in d.glob(pattern)})


def load_meeting(customer_id: str, project_id: str, meeting_no: int) -> dict:
    """회의 한 건. 등록된 기록이 없으면 녹취 머리말(일시 / 방식 / 장소, 참석 줄)에서 읽어 만든다."""
    m = _read(meeting_path(customer_id, project_id, meeting_no), {})
    m.setdefault("no", int(meeting_no))
    tp = transcript_path(customer_id, project_id, meeting_no)
    lines = tp.read_text(encoding="utf-8").splitlines()[:3] if tp.exists() else []
    head = next((h for h in map(_HEAD.match, lines) if h), None)
    for field, value in zip(("date", "time", "type", "place"), head.groups() if head else ("", "", "", "")):
        m.setdefault(field, (value or "").strip())
    if "attendees" not in m:
        row = next((l.split(":", 1)[1] for l in lines if l.startswith("참석") and ":" in l), "")
        parts = [p.split() for p in re.split(r"[,/·]", re.sub(r"\((고객|당사)\)", " ", row))]
        m["attendees"] = [p[0] for p in parts if p and re.fullmatch(r"[가-힣]{2,4}", p[0])]  # 각 사람의 첫 낱말이 이름이다
    if "scope" not in m:  # 외부(고객과의 회의) · 내부(우리끼리의 회의). 참석 줄이 있는데 (고객)이 없으면 내부로 본다
        named = [l for l in lines if l.startswith("참석")]
        m["scope"] = "내부" if named and not any("(고객)" in l for l in named) else "외부"
    m.setdefault("minutes", {"status": ""})
    m.setdefault("changes", [])
    return m


def save_meeting(customer_id: str, project_id: str, meeting_no: int, data: dict) -> None:
    _write(meeting_path(customer_id, project_id, meeting_no), data)


def register_meeting(customer_id: str, project_id: str, date: str, time: str = "", kind: str = "", place: str = "",
                     scope: str = "외부", attendees: list | None = None, agenda: str = "", links: list | None = None) -> int:
    """다가올 회의를 등록한다(녹취는 나중에). 참석자 · 안건 · 참고 링크를 함께 받는다. 새 회차 번호를 돌려준다."""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date or ""):
        raise ValueError("날짜는 2026-10-30 형식으로 적습니다.")
    names = list(dict.fromkeys(str(a).strip()[:20] for a in (attendees or []) if str(a).strip()))[:30]
    urls = [str(u).strip() for u in (links or []) if str(u).strip()]
    if any(not re.match(r"https?://", u) for u in urls):
        raise ValueError("참고 링크는 http:// 또는 https:// 로 시작해야 합니다.")
    n = max(meeting_nos(customer_id, project_id) or [0]) + 1
    save_meeting(customer_id, project_id, n, {"no": n, "date": date, "time": time.strip(), "type": kind.strip(), "place": place.strip(),
                                                "attendees": names, "agenda": (agenda or "").strip()[:2000], "links": urls[:10],
                                                "scope": scope if scope in SCOPES else "외부",
                                                "minutes": {"status": ""}, "changes": []})
    return n


def save_project(customer_id: str, project_id: str, proj: dict) -> None:
    _write(project_dir(customer_id, project_id) / "project.json", proj)


DECISIONS = ["전환", "보류", "부적합"]  # 화면설계 「추진 전환」의 판정. 덮어쓰지 않고 회차로 쌓는다


def add_decision(customer_id: str, project_id: str, result: str, reason: str, who: str) -> list[dict]:
    """추진 전환 판정을 사람이 남긴다. 보류 · 부적합은 사유가 있어야 한다."""
    reason = (reason or "").strip()
    if result not in DECISIONS:
        raise ValueError(f"판정은 {' · '.join(DECISIONS)} 중 하나입니다.")
    if result != "전환" and not reason:
        raise ValueError(f"{result} 판정에는 사유가 필요합니다.")
    proj = load_project(customer_id, project_id)
    rows = proj.setdefault("판정", [])
    rows.append({"round": len(rows) + 1, "result": result, "reason": reason, "by": who, "at": time.strftime("%Y-%m-%d %H:%M")})
    save_project(customer_id, project_id, proj)
    return rows


def deal_text(value) -> str:
    return (" + ".join(value) if isinstance(value, list) else value) or "미정"


def set_deal(customer_id: str, project_id: str, field: str, value) -> dict:
    """거래 조건을 사람이 정한다. 빈 값이면 미정으로 되돌린다. 여러 개 고르는 축은 목록으로 받는다."""
    if field in DEAL_MULTI:
        value = [value] if isinstance(value, str) and value else list(value or [])
        if any(v not in DEAL_MULTI[field] for v in value):
            raise ValueError(f"거래 조건 '{field}'에 쓸 수 없는 값입니다: {value}")
        value = [v for v in DEAL_MULTI[field] if v in value]
    elif field not in DEAL_FIELDS or not isinstance(value, str) or (value and value not in DEAL_FIELDS[field]):
        raise ValueError(f"거래 조건 '{field}'에 쓸 수 없는 값입니다: {value}")
    proj = load_project(customer_id, project_id)
    proj.setdefault("deal", {})[field] = value
    _write(project_dir(customer_id, project_id) / "project.json", proj)
    return proj["deal"]


def set_meeting_scope(customer_id: str, project_id: str, meeting_no: int, scope: str) -> dict:
    """회의가 외부인지 내부인지를 사람이 정한다(녹취에서 읽은 값을 덮는다)."""
    if scope not in SCOPES:
        raise ValueError(f"회의 유형은 {' · '.join(SCOPES)} 중 하나입니다.")
    m = load_meeting(customer_id, project_id, meeting_no)
    m["scope"] = scope
    save_meeting(customer_id, project_id, meeting_no, m)
    return m
