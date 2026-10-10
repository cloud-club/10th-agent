"""리드 결과지: 회의를 거듭하며 채워지는 고객 니즈 확인 항목과 추진 전환 판정의 재료.

화면설계 「리드 결과지 · 고객 니즈 확인」의 세 묶음을 그대로 따른다.
  고객 상황 파악   S1 현행 운영 방식 · S2 주요 이슈 · S3 비즈니스 목표(표) · S4 핵심 요구사항(표)
  의사결정 조직    D1 최종 결재자 · D2 계약 담당자  (이해관계자 목록의 역할에서 읽는다)
  구매 가능성 판단 B1 예산 상태 · B2 사업예산·예상 수주금액 · B3 긴급도·착수 희망시기 · B4 계약 예상시기
  참고             X1 경쟁 상황 (확정 항목 10개에는 넣지 않는다)

원칙 두 가지
  1. AI가 채운 값은 '초안'이다. 사람이 확인하기 전에는 확정 항목으로 세지 않는다.
  2. 모든 값에는 근거 발화가 붙는다. 모델이 적어 낸 인용을 그대로 믿지 않고, 이번 회의 녹취의
     실제 발언과 맞춰 본 뒤 맞는 발언을 근거로 저장한다. 맞는 발언이 없으면 받지 않는다.
"""

from __future__ import annotations

import copy
import difflib
import re
import time

from .masking import TITLE_WORDS
from .store import DEAL_FIELDS

DRAFT, CONFIRMED, MISSING = "초안", "확인", "미확보"
GROUND_MIN = 0.6  # 인용의 2글자 조각 중 이 비율 이상이 한 발언에 들어 있어야 근거로 받는다

ITEMS = {  # 코드: (묶음, 이름, 무엇을 적는가)
    "S1": ("고객 상황 파악", "현행 운영 방식", "고객이 지금 어떻게 운영하고 있는가"),
    "S2": ("고객 상황 파악", "주요 이슈", "고객이 불편을 느끼는 지점"),
    "S3": ("고객 상황 파악", "비즈니스 목표", "풀리면 무엇이 좋아지는가 — 현재 수준과 희망 수준"),
    "S4": ("고객 상황 파악", "핵심 요구사항", "요구사항과 고객배경, 당사 대응, 확인 상태"),
    "D1": ("의사결정 조직", "최종 결재자", "누가 최종으로 결정하는가"),
    "D2": ("의사결정 조직", "계약 담당자", "계약·구매 실무를 누가 맡는가"),
    "B1": ("구매 가능성 판단", "예산 상태", "확보·미확보·확인 불가와 그 출처(확인 경로)"),
    "B2": ("구매 가능성 판단", "사업예산·예상 수주금액", "고객의 총 사업예산과 우리 몫의 금액"),
    "B3": ("구매 가능성 판단", "긴급도·착수 희망시기", "얼마나 급한가와 그 근거가 되는 고객 일정"),
    "B4": ("구매 가능성 판단", "계약 예상시기", "계약을 언제쯤 맺을 수 있는가"),
    "X1": ("구매 가능성 판단", "경쟁 상황", "경쟁사·기존 공급사와 확인 경로"),
    # 거래 조건에 따라 더 확인해야 하는 것. 해당하는 프로젝트에만 나온다(required_extras)
    "C1": ("거래 조건 확인", "재원·자부담 조달", "정부지원금과 자부담의 몫, 고객이 자부담을 댈 수 있는가"),
    "C2": ("거래 조건 확인", "원청·주관기관의 결정 구조", "원청이나 주관기관에서 누가 결정하고 우리는 어느 단계인가"),
    "C3": ("거래 조건 확인", "물품(HW) 범위", "어떤 하드웨어를 누가 공급하고 설치하는가"),
    "C4": ("거래 조건 확인", "비밀유지 체결", "누구와 언제까지 맺는가, 그 전에 받으면 안 되는 자료"),
    "C5": ("거래 조건 확인", "공동수급 역할", "대표자인가 구성원인가, 출자비율이나 분담내용"),
    "C6": ("거래 조건 확인", "시연 검증 기준", "무엇을 보여 주고 어떤 결과면 통과인가"),
    "C7": ("거래 조건 확인", "입찰 일정·자격", "공고 일정과 참가 자격, 제출 서류"),
    "C8": ("거래 조건 확인", "하도급 승인·비율", "발주기관의 사전 승인을 받았는가, 하도급 비율이 한도 안인가"),
    "C9": ("거래 조건 확인", "상용SW 직접구매", "발주기관이 상용SW를 따로 사는가"),
}
TEXT_ITEMS = ["S1", "S2", "B1", "B2", "B3", "B4", "X1", "C1", "C2", "C3", "C4", "C5", "C6", "C7", "C8", "C9"]  # 글 한 덩어리로 적는 항목
GATE = ["S1", "S2", "S3", "S4", "D1", "D2", "B1", "B2", "B3", "B4"]  # 추진 전환 판정에 세는 확정 항목 10개
ROLES = ["의사결정자", "영향자", "구매", "기술 평가자", "챔피언", "사용자", "게이트키퍼", "법무·계약"]
ROLE_OF = {"D1": {"의사결정자"}, "D2": {"법무·계약", "구매"}}
RESPONSE_STATUS = ["합의됨", "검토 중", "확인 필요"]


def empty() -> dict:
    return {"summary": "", "items": {}, "goals": [], "requirements": [], "stakeholders": [], "next_questions": [], "deal_hints": {}}


# ---- 근거 맞추기 ---------------------------------------------------------------

def _grams(text: str) -> set[str]:
    from .retrieval import tokenize  # retrieval이 memory를 거쳐 이 모듈을 부르므로 쓰는 시점에 가져온다
    return set(tokenize(text))


def ground(quote: str, utts: list[dict]) -> tuple[dict | None, list[dict]]:
    """인용에 가장 가까운 실제 발언을 찾는다. (근거로 받을 발언 | None, 가까운 후보 두 개)."""
    q = _grams(quote or "")
    if len(q) < 3:
        return None, []
    scored = sorted(((len(q & u["grams"]) / len(q), u) for u in utts), key=lambda x: -x[0])
    hit = scored[0][1] if scored and scored[0][0] >= GROUND_MIN else None
    return hit, [u for s, u in scored[:2] if s >= 0.25]


class Grounder:
    """한 번의 갱신에서 인용을 발언에 맞추고, 못 맞춘 것은 사유를 모은다."""

    def __init__(self, utts: list[dict], meeting_no: int):
        self.utts = [{**u, "grams": _grams(u["text"])} for u in utts]
        self.meeting_no, self.errors = meeting_no, []

    def find(self, where: str, quote: str) -> dict | None:
        if not (quote or "").strip():
            self.errors.append(f"{where}: 근거(evidence)가 비어 있다. 이번 녹취에서 그 내용을 말한 발언의 번호(#12)를 적는다.")
            return None
        ref = re.match(r"\s*#\s*(\d{1,4})", quote)
        if ref:  # 발언 번호로 가리킨 근거
            hit = next((u for u in self.utts if u["pos"] == int(ref.group(1))), None)
            if hit is None:
                self.errors.append(f"{where}: 근거 번호 #{ref.group(1)}은(는) 이번 녹취에 없다. 발언 번호는 #1~#{len(self.utts)}이다.")
                return None
            near = []
        else:
            hit, near = ground(quote, self.utts)
        if hit is None:
            hint = " / ".join(f"#{u['pos']} {u['speaker']}: {u['text'][:70]}" for u in near) or "비슷한 발언 없음"
            self.errors.append(f"{where}: 근거 '{quote[:40]}'을(를) 이번 녹취에서 찾지 못했다. 발언 번호(#12)로 적는다. 가까운 발언 — {hint}")
            return None
        return {"meeting": self.meeting_no, "pos": hit["pos"], "speaker": hit["speaker"], "text": hit["text"][:240]}


def _add_evidence(entry: dict, ev: dict) -> None:
    kept = [e for e in entry.get("evidence", []) if (e["meeting"], e["pos"]) != (ev["meeting"], ev["pos"])]
    entry["evidence"] = (kept + [ev])[-3:]


def _same(a: str, b: str) -> bool:
    norm = lambda s: "".join((s or "").split())
    return norm(a) == norm(b)


def _similar(rows: list[dict], field: str, text: str) -> dict | None:
    best = max(rows, key=lambda r: difflib.SequenceMatcher(None, r[field], text).ratio(), default=None)
    return best if best and difflib.SequenceMatcher(None, best[field], text).ratio() >= 0.6 else None


def _touch(entry: dict, changed: bool, meeting_no: int) -> None:
    """값이 바뀌면 사람 확인을 다시 받도록 초안으로 돌린다. 같은 값을 다시 들은 것이면 상태를 그대로 둔다."""
    new = "status" not in entry
    entry.setdefault("since", meeting_no)
    if changed or new:
        entry["status"], entry["updated"] = DRAFT, meeting_no
        entry.pop("confirmed_by", None)
        # 이력: 언제 처음 들어왔고 언제 무엇으로 바뀌었는지. 사람이 확인한 기록도 여기에 쌓인다(confirm)
        entry.setdefault("history", []).append({"meeting": meeting_no, "action": "변경" if not new else "신규", "value": brief(entry)})


# ---- 갱신 ---------------------------------------------------------------------

def apply(base: dict, upd: dict, meeting_no: int, utts: list[dict], known_names: list[str], transcript: str = "",
          own_names: tuple | list = ()) -> tuple[dict, list[str], dict]:
    """갱신 요청을 결과지에 반영한다. 하나라도 어긋나면 아무것도 반영하지 않고 사유를 돌려준다.

    돌려주는 값: (새 결과지, 사유 목록, {"contacts": 고객 측 담당자로 등록할 사람들})
    """
    sh, g, contacts = copy.deepcopy(base), Grounder(utts, meeting_no), []
    errs = g.errors
    if not (upd.get("summary") or "").strip():
        errs.append("summary가 비어 있다. 지금까지 확인된 고객 상황을 두세 문장으로 적는다.")
    else:
        sh["summary"] = upd["summary"].strip()

    for it in upd.get("items") or []:
        code, value = it.get("code"), (it.get("value") or "").strip()
        if code not in TEXT_ITEMS:
            errs.append(f"items의 code '{code}'는 쓸 수 없다. {', '.join(TEXT_ITEMS)} 중 하나다. 목표·요구사항·사람은 goals·requirements·stakeholders에 적는다.")
            continue
        ev = g.find(f"{code} {ITEMS[code][1]}", it.get("evidence"))
        if not value:
            errs.append(f"{code} {ITEMS[code][1]}: value가 비어 있다.")
        if ev is None or not value:
            continue
        entry = sh["items"].setdefault(code, {})
        changed = "value" in entry and not _same(entry["value"], value)
        if changed:
            entry["previous"] = entry["value"]  # 번복을 사람이 알아보게 앞 값을 남긴다
        entry["value"] = value
        _touch(entry, changed, meeting_no)
        _add_evidence(entry, ev)

    for kind, title_field, fields in (("goals", "goal", ("current", "target", "measure")),
                                      ("requirements", "title", ("background", "response", "response_status"))):
        for row in upd.get(kind) or []:
            title = (row.get(title_field) or "").strip()
            where = f"{kind} '{title[:20]}'"
            if not title:
                errs.append(f"{kind}: {title_field}이(가) 비어 있다.")
                continue
            if kind == "requirements" and (row.get("response_status") or "확인 필요") not in RESPONSE_STATUS:
                errs.append(f"{where}: response_status는 {' · '.join(RESPONSE_STATUS)} 중 하나다.")
                continue
            ev = g.find(where, row.get("evidence"))
            if ev is None:
                continue
            entry = _similar(sh[kind], title_field, title)
            if entry is None:
                entry = {"id": f"{kind[0].upper()}{len(sh[kind]) + 1}", title_field: title}
                sh[kind].append(entry)
            changed = False
            for f in fields:
                new = (row.get(f) or "").strip() or ("확인 필요" if f == "response_status" else "")
                if new and not _same(entry.get(f, ""), new):
                    changed = changed or f in entry
                    entry[f] = new
            if kind == "goals":
                entry["speaker"] = ev["speaker"]
            _touch(entry, changed, meeting_no)
            _add_evidence(entry, ev)

    for p in upd.get("stakeholders") or []:
        name, roles = (p.get("name") or "").strip(), list(p.get("roles") or [])
        if not name:
            errs.append("stakeholders: name이 비어 있다.")
            continue
        if name in own_names:
            continue  # 우리 쪽 사람은 프로젝트 담당(조직 정보)에서 온다. 고객 측 영업상 역할을 붙이지 않는다
        if not re.fullmatch(r"[가-힣]{2,4}", name) or re.fullmatch(TITLE_WORDS, name):
            errs.append(f"stakeholders '{name}': 사람 이름이 아니다. 이름을 아직 모르는 사람은 넣지 않고, 이름을 알아낼 질문을 next_questions에 적는다.")
            continue
        if name not in known_names and name not in transcript:
            errs.append(f"stakeholders '{name}': 녹취·고객 담당자·우리 조직 어디에도 없는 이름이다. 녹취의 표기를 확인해 고친다.")
            continue
        bad = [r for r in roles if r not in ROLES]
        if bad:
            errs.append(f"stakeholders '{name}': 역할 {bad}은(는) 쓸 수 없다. {' · '.join(ROLES)} 중에서 고른다.")
            continue
        ev = g.find(f"stakeholders '{name}'", p.get("evidence")) if roles else None
        if roles and ev is None:
            continue
        entry = next((s for s in sh["stakeholders"] if s["name"] == name), None)
        if entry is None:
            entry = {"name": name, "roles": []}
            sh["stakeholders"].append(entry)
        merged = sorted(set(entry["roles"]) | set(roles), key=ROLES.index)
        changed = merged != entry["roles"] and "status" in entry
        entry.update(side="고객", roles=merged)
        for f in ("dept", "title"):
            if (p.get(f) or "").strip():
                entry[f] = p[f].strip()
        _touch(entry, changed, meeting_no)
        if ev:
            _add_evidence(entry, ev)
        contacts.append({f: (p.get(f) or "") for f in ("name", "dept", "title", "phone", "email")})

    for row in upd.get("deal_hints") or []:  # 거래 조건은 사람이 정한다. 에이전트는 단서를 근거와 함께 제안만 한다
        field, value = row.get("field"), row.get("value")
        if field not in DEAL_FIELDS or value not in DEAL_FIELDS[field]:
            errs.append(f"deal_hints: '{field}'의 값은 {' · '.join(DEAL_FIELDS.get(field, [])) or '쓸 수 없는 항목'} 중 하나다.")
            continue
        ev = g.find(f"deal_hints '{field}'", row.get("evidence"))
        if ev:
            sh.setdefault("deal_hints", {})[field] = {"value": value, "evidence": ev, "meeting": meeting_no}

    if upd.get("next_questions") is not None:
        sh["next_questions"] = [q.strip() for q in upd["next_questions"] if (q or "").strip()][:8]
    return (base, errs, {}) if errs else (sh, [], {"contacts": contacts})


# ---- 판정 ---------------------------------------------------------------------

def _rows_status(rows: list[dict]) -> str:
    if any(r.get("status") == CONFIRMED for r in rows):
        return CONFIRMED
    return DRAFT if rows else MISSING


def status_of(sh: dict, code: str) -> str:
    if code in TEXT_ITEMS:
        return sh["items"].get(code, {}).get("status", MISSING)
    if code == "S3":
        return _rows_status(sh["goals"])
    if code == "S4":
        return _rows_status(sh["requirements"])
    return _rows_status([s for s in sh["stakeholders"] if ROLE_OF[code] & set(s["roles"])])


def gate(sh: dict) -> dict:
    """추진 전환 판정의 재료. 사람이 확인한 것만 확정으로 센다."""
    by_code = {c: status_of(sh, c) for c in ITEMS}
    count = lambda s: sum(1 for c in GATE if by_code[c] == s)
    return {"confirmed": count(CONFIRMED), "draft": count(DRAFT), "missing": count(MISSING), "total": len(GATE), "by_code": by_code,
            "missing_labels": [f"{c} {ITEMS[c][1]}" for c in GATE if by_code[c] == MISSING]}


def gate_line(sh: dict) -> str:
    g = gate(sh)
    return f"확정 {g['confirmed']}/{g['total']} · AI 초안 {g['draft']} · 미확보 {g['missing']}"


def confirm(sh: dict, kind: str, key: str, who: str) -> int:
    """사람이 초안을 확인한다. kind: item(코드) · goal · requirement(id) · stakeholder(이름) · all. 바뀐 건수를 돌려준다."""
    rows = {"item": [v for c, v in sh["items"].items() if c == key],
            "goal": [r for r in sh["goals"] if r["id"] == key],
            "requirement": [r for r in sh["requirements"] if r["id"] == key],
            "stakeholder": [s for s in sh["stakeholders"] if s["name"] == key],
            "all": list(sh["items"].values()) + sh["goals"] + sh["requirements"] + sh["stakeholders"]}.get(kind, [])
    hit = [r for r in rows if r.get("status") == DRAFT]
    for r in hit:
        r["status"], r["confirmed_by"] = CONFIRMED, who
        r.pop("previous", None)
        r.setdefault("history", []).append({"action": "확인", "by": who, "at": time.strftime("%Y-%m-%d %H:%M"), "value": brief(r)})
    return len(hit)


# ---- 에이전트에게 보여줄 글 ----------------------------------------------------

def render(sh: dict, deal: dict | None = None) -> str:
    g, extras = gate(sh), required_extras(deal)
    lines = [f"### 결과지 현황 — {gate_line(sh)}", f"전체 요약: {sh['summary'] or '(없음)'}", "",
             "| 코드 | 항목 | 상태 | 지금까지 확인된 값 |", "| --- | --- | --- | --- |"]
    for code, (_, label, hint) in ITEMS.items():
        if code.startswith("C") and code not in extras:
            continue
        if code in TEXT_ITEMS:
            value = sh["items"].get(code, {}).get("value", "")
        elif code == "S3":
            value = " / ".join(f"{r['goal']}({r.get('current', '?')} → {r.get('target', '?')})" for r in sh["goals"])
        elif code == "S4":
            value = " / ".join(f"{r['title']}[{r.get('response_status', '확인 필요')}]" for r in sh["requirements"])
        else:
            value = ", ".join(s["name"] for s in sh["stakeholders"] if ROLE_OF[code] & set(s["roles"]))
        lines.append(f"| {code} | {label} | {g['by_code'][code]} | {value or '— ' + hint} |")
    if sh["requirements"]:
        lines += ["", "요구사항 상세"]
        lines += [f"- {r['id']} {r['title']} · 고객배경: {r.get('background') or '미확인'} · 당사 대응: {r.get('response') or '미정'} · {r.get('response_status', '확인 필요')}"
                  for r in sh["requirements"]]
    if sh["stakeholders"]:
        lines += ["", "이해관계자"]
        lines += [f"- {s['name']}({s.get('side', '')} {s.get('dept', '')} {s.get('title', '')}) 역할: {' · '.join(s['roles']) or '미확인'}" for s in sh["stakeholders"]]
    if sh["next_questions"]:
        lines += ["", "지난 회의 때 다음에 묻기로 한 것"] + [f"- {q}" for q in sh["next_questions"]]
    return "\n".join(lines)


# ---- 거래 조건에 따른 추가 확인 · 회의별 변경 ------------------------------------

def required_extras(deal: dict | None) -> list[str]:
    """거래 조건에 따라 더 확인해야 하는 항목 코드. 조건이 정해지지 않았으면 없다."""
    deal = deal or {}
    on = lambda event: deal.get(event) in ("필요", "완료")
    kind, goods = deal.get("사업 유형") or "", deal.get("계약 목적물") or []
    joint = (deal.get("공동계약 방식") or "단독") != "단독" or on("공동수급협정서 체결")
    below = deal.get("계약 지위") in ("하도급", "재하도급")
    public = kind == "공공조달"
    rules = [("C1", kind in ("정부지원사업(보조금 + 자부담)", "국가연구개발과제")),
             ("C2", below or joint or kind == "국가연구개발과제"),
             ("C3", "물품(HW·설비)" in goods or "설치 공사" in goods),
             ("C4", on("비밀유지계약 체결")),
             ("C5", joint),
             ("C6", on("시연·실증")),
             ("C7", on("입찰·제안서 제출") or public
              or deal.get("계약 방법") in ("경쟁입찰", "협상에 의한 계약(제안서 평가)", "과제 공모(협약)")),
             ("C8", public and (below or on("하도급 사전 승인") or on("파트너사 하도급 계약"))),
             ("C9", public and "상용SW(라이선스)" in goods)]
    return [code for code, on in rules if on]


def _core(row: dict) -> dict:
    return {k: v for k, v in row.items() if k not in ("evidence", "status", "updated", "since", "confirmed_by", "previous", "speaker", "history")}


def diff(prev: dict, new: dict) -> list[dict]:
    """한 회의가 결과지에서 바꾸는 것. 회의 상세의 '이 회의의 변경'과 세 번째 게이트가 이 목록을 쓴다."""
    out = []
    for code, it in new["items"].items():
        old = prev["items"].get(code)
        if old is None or not _same(old["value"], it["value"]):
            out.append({"kind": "item", "key": code, "label": ITEMS[code][1], "action": "변경" if old else "신규",
                        "value": it["value"], **({"previous": old["value"]} if old else {})})
    for kind, one, title in (("goals", "goal", "goal"), ("requirements", "requirement", "title")):
        olds = {r["id"]: r for r in prev[kind]}
        for r in new[kind]:
            if r["id"] not in olds or _core(olds[r["id"]]) != _core(r):
                out.append({"kind": one, "key": r["id"], "label": ITEMS["S3" if one == "goal" else "S4"][1],
                            "action": "변경" if r["id"] in olds else "신규", "value": r[title]})
    olds = {s["name"]: s for s in prev["stakeholders"]}
    for s in new["stakeholders"]:
        if s["name"] not in olds or olds[s["name"]]["roles"] != s["roles"]:
            out.append({"kind": "stakeholder", "key": s["name"], "label": "이해관계자", "action": "변경" if s["name"] in olds else "신규",
                        "value": f"{s['name']} — {' · '.join(s['roles']) or '역할 미확인'}"})
    for field, hint in (new.get("deal_hints") or {}).items():
        if (prev.get("deal_hints") or {}).get(field, {}).get("value") != hint["value"]:
            out.append({"kind": "deal", "key": field, "label": "거래 조건 제안", "action": "제안", "value": f"{field} = {hint['value']}"})
    return out


def pending_for(sh: dict, meeting_no: int) -> int:
    """이 회의가 넣거나 바꾼 값 가운데 아직 사람이 확인하지 않은 것의 수(세 번째 게이트)."""
    rows = list(sh["items"].values()) + sh["goals"] + sh["requirements"] + sh["stakeholders"]
    return sum(1 for r in rows if r.get("status") == DRAFT and r.get("updated") == meeting_no)


def brief(entry: dict) -> str:
    """결과지 값 하나를 이력에 남길 한 줄로."""
    if "value" in entry:
        return entry["value"]
    if "roles" in entry:
        return " · ".join(entry["roles"]) or "역할 미확인"
    if "goal" in entry:
        return f"{entry['goal']} ({entry.get('current') or '?'} → {entry.get('target') or '?'})"
    return f"{entry.get('title', '')} — {entry.get('response_status', '확인 필요')} · {entry.get('response') or '대응 미정'}"


def completeness(sh: dict, deal: dict | None = None) -> dict:
    """결과지가 얼마나 찼나. 확정 항목 10개에 거래 조건이 요구하는 추가 확인을 더한 것이 분모다.

    완결성(confirmed_pct): 사람이 확인한 것만 센다.  채움(filled_pct): AI 초안까지 센다.
    둘의 차이가 '사람이 아직 보지 않은 몫'이다.
    """
    codes = GATE + required_extras(deal)
    status = [status_of(sh, c) for c in codes]
    confirmed, filled = status.count(CONFIRMED), len(status) - status.count(MISSING)
    pct = lambda n: round(n / len(codes) * 100)
    return {"total": len(codes), "confirmed": confirmed, "filled": filled, "confirmed_pct": pct(confirmed), "filled_pct": pct(filled)}
