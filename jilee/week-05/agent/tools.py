"""에이전트가 호출하는 Tool 6종(조회 2 · 결과지 갱신 · 회의록 저장 · 이력 검색 · SOP 문의)과 그 스키마.

모델은 판단만 하고, 파일을 읽고 쓰는 행동은 여기 있는 파이썬 함수가 한다.
Week 5: 일의 단위가 고객 → 프로젝트로 바뀌었고, 산출물의 중심이 회의록에서 결과지로 옮겨 갔다.
결과지 갱신은 근거 발화 대조를, 회의록 저장은 양식 규칙 검사를 통과해야 한다. 어긋나면 사유를 돌려주어 모델이 고쳐 다시 부른다.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from . import graph, memory, retrieval, sheet, store
from .sop_agent import consult_sop

KNOWLEDGE_DIR = store.ROOT / "knowledge"

VALIDATE = True           # 저장 전 규칙 검사. 루프 자체만 시험할 때 끈다
LONG_TRANSCRIPT = 4000    # 이보다 긴 녹취는 구간별로 정리해서 넘긴다(분당 토큰 한도·출력 상한 대응)
CHUNK_CHARS = 2500

_run_llm = None           # 실행 중인 LLM(마스킹 포함). 녹취 구간 정리에 같은 클라이언트를 쓴다
_run_opts: dict = {}      # 이번 실행의 옵션(loop.normalize_options). 결과지를 건드리지 않는 실행인지 등


def bind_llm(llm) -> None:
    global _run_llm
    _run_llm = llm


def bind_options(opts: dict | None) -> None:
    global _run_opts
    _run_opts = dict(opts or {})


def _project(customer_id: str, project_id: str) -> tuple[Path, str]:
    return store.project_dir(customer_id, project_id), store.key(customer_id, project_id)


# ---- Tool ① 조회 ---------------------------------------------------------

def read_project_context(customer_id: str, project_id: str, meeting_no: int | None = None) -> str:
    """고객·프로젝트·담당자와 프로젝트 기억(지난 회의 누적 + 결과지 현황)을 돌려준다. 이것이 에이전트의 '기억'이다."""
    d, key = _project(customer_id, project_id)
    n = int(meeting_no) if meeting_no is not None else memory.latest_no(d) + 1
    st = memory.load_before(key, d, n)
    return (store.render_customer(customer_id, project_id)
            + f"\n\n## 프로젝트 기억 ({st['meeting_no']}차까지 확정분)\n"
            + memory.render_state(st, store.load_project(customer_id, project_id).get("deal"))
            + _pre_info(customer_id, project_id, n))


def suggest_agenda(customer_id: str, project_id: str) -> dict:
    """다음 회의의 안건 초안: 지난 회의에서 정하지 못한 것, 끝나지 않은 진행 예정 사항, 결과지의 추가 확인 필요 사항."""
    d, key = _project(customer_id, project_id)
    st = memory.latest(key, d)
    clean = lambda s: re.sub(r"\*\*|\s+", " ", str(s)).strip()
    last = st.get("meeting_no", 0)
    issues = [clean(i["text"]).split(" → ")[0] for i in st.get("open_issues", []) if i.get("meeting") == last]
    actions = [f"{clean(a['task'])} ({a.get('owner', '')} · {a.get('due', '')})" for a in st.get("action_items", []) if a.get("status") != "완료"]
    questions = [clean(q) for q in st["sheet"].get("next_questions", [])]
    lines = ([f"[{last}차에서 정하지 못한 것]"] + [f"- {x}" for x in issues[:6]] if issues else []) \
        + (["[진행 예정 사항 점검]"] + [f"- {x}" for x in actions[:8]] if actions else []) \
        + (["[추가 확인 필요 사항]"] + [f"- {x}" for x in questions[:6]] if questions else [])
    return {"text": "\n".join(lines), "from_meeting": last, "counts": {"issues": len(issues), "actions": len(actions), "questions": len(questions)}}


AGENDA_PROMPT = """영업 담당자가 다음 회의를 준비한다. 아래 재료(지난 회의에서 정하지 못한 것, 끝나지 않은 일, 아직 모르는 것)만으로 다음 회의 안건을 쓴다.
- 4~7줄. 중요한 것부터. 한 줄은 "- "로 시작하고 40자 안팎의 명사구로 쓴다(예: "- 하자보수 기간 최종 합의(1년 vs 2년)").
- 비슷한 것은 하나로 묶는다. 재료에 없는 내용은 더하지 않는다. 사람 이름과 숫자는 재료 그대로 쓴다.
- 안건 줄만 쓴다. 머리말이나 설명은 쓰지 않는다."""


def draft_agenda(customer_id: str, project_id: str, llm=None) -> dict:
    """AI로 다음 회의 안건을 정리한다. 모델이 답하지 못하면 재료를 그대로 돌려준다."""
    base = suggest_agenda(customer_id, project_id)
    if not base["text"]:
        return {**base, "ai": False, "note": "지난 회의에서 가져올 것이 없습니다."}
    try:
        from .chat import build_masker
        from .llm import LLMClient
        from .masking import MaskedLLM
        llm = llm or LLMClient()
        if os.environ.get("MASKING", "on").lower() != "off":
            llm = MaskedLLM(llm, build_masker())
        msg = llm.chat([{"role": "system", "content": AGENDA_PROMPT}, {"role": "user", "content": base["text"]}])
        lines = [l.strip() for l in (msg.get("content") or "").splitlines() if l.strip().startswith("- ")]
        if len(lines) < 2:
            raise ValueError("모델이 안건을 쓰지 못했습니다")
        return {**base, "text": "\n".join(lines[:8]), "ai": True}
    except Exception as e:
        return {**base, "ai": False, "note": f"AI 정리 실패 — 지난 회의의 남은 일을 그대로 넣었습니다 ({str(e)[:80]})"}


def _pre_info(customer_id: str, project_id: str, n: int) -> str:
    """회의를 등록할 때 사람이 적어 둔 것: 안건 · 참석 예정 · 참고 링크와 자료 이름(자료의 내용은 읽지 않는다)."""
    if not store.meeting_path(customer_id, project_id, n).exists():
        return ""
    m = store.load_meeting(customer_id, project_id, n)
    lines = [f"- 안건·메모: {m['agenda']}"] if m.get("agenda") else []
    if m.get("links"):
        lines.append("- 참고 링크: " + ", ".join(m["links"]))
    try:
        from . import files
        names = [f["name"] for f in files.listing(customer_id, project_id, n)]
        if names:
            lines.append("- 참고 자료(이름만): " + ", ".join(names))
    except Exception:
        pass
    return (f"\n\n## {n}차 회의 사전 정보(등록할 때 담당자가 적음)\n" + "\n".join(lines)) if lines else ""


def read_transcript(customer_id: str, project_id: str, meeting_no: int) -> str:
    """지정한 회차의 녹취/메모를 발언 번호(#n)와 함께 돌려준다. 길면 구간별 정리본을 돌려준다."""
    p = store.transcript_path(customer_id, project_id, meeting_no)
    if not p.exists():
        raise FileNotFoundError(f"녹취 파일이 없습니다: {p.name}")
    raw = p.read_text(encoding="utf-8")
    text = store.numbered(raw)  # 발언마다 #번호를 붙여, 결과지 근거를 번호로 가리키게 한다
    if len(raw) <= LONG_TRANSCRIPT or _run_llm is None:
        return text
    return summarize_long_transcript(text, _run_llm)


CHUNK_PROMPT = """당신은 영업 회의 녹취 정리 담당자다. 아래는 긴 회의 녹취의 한 구간이다. 이 구간만 보고 다음을 한국어로 정리한다.
녹취는 음성인식 결과라 오타·잘못 들린 이름이 섞여 있다. 머리말의 참석자 명단으로 바로잡고, 확신이 없으면 원문을 괄호로 남긴다.
각 줄 앞의 #번호는 발언 번호다. 정리한 줄마다 근거가 된 발언 번호를 끝에 (#12)처럼 붙인다. 번호는 주어진 것만 쓴다.

### 논의 요지
- 화자: 요지 (중요한 고객 발언은 "원문 그대로" 인용)
### 결정·합의
- 결정 내용 (이 구간 안에서 앞서 한 말을 뒤집었으면 "[번복] 처음 말 → 바뀐 말"로 표시)
### 약속·할 일
- 할 일 / 담당 / 기한(말한 그대로)
### 숫자
- 금액·수량·날짜·비율 (말한 그대로와 숫자 표기를 함께)
### 미결
- 정하지 못한 것

이 구간에 없는 내용은 쓰지 않는다. 해당 없는 항목은 "- 없음"."""


def split_transcript(text: str, size: int = CHUNK_CHARS) -> tuple[str, list[str]]:
    """머리말(일시·참석 줄)과 발화 단위 구간 목록. 발화 한 줄은 자르지 않는다."""
    lines = text.splitlines()
    head = [l for l in lines[:3] if l.startswith("[") or l.startswith("참석")]
    body = lines[len(head):]
    chunks, buf = [], []
    for line in body:
        if buf and sum(len(b) for b in buf) + len(line) > size:
            chunks.append("\n".join(buf))
            buf = []
        buf.append(line)
    if buf:
        chunks.append("\n".join(buf))
    return "\n".join(head), [c for c in chunks if c.strip()]


def summarize_long_transcript(text: str, llm) -> str:
    """map 단계: 구간마다 정리본을 만든다. reduce(결과지 갱신·회의록 작성)는 주 에이전트가 한다."""
    head, chunks = split_transcript(text)
    parts = [f"(녹취가 {len(text):,}자로 길어 {len(chunks)}개 구간으로 나눠 정리했다. 구간 순서가 시간 순서다. "
             "뒤 구간의 결정이 앞 구간과 다르면 뒤의 것이 최종이며, [번복] 표시를 회의록 3절·5절에 반영한다. "
             "줄 끝의 (#번호)는 그 내용을 말한 발언 번호이니 결과지의 근거로 쓴다.)", head]
    for i, c in enumerate(chunks, start=1):
        msg = llm.chat([{"role": "system", "content": CHUNK_PROMPT},
                        {"role": "user", "content": f"{head}\n\n[구간 {i}/{len(chunks)}]\n{c}"}])
        parts.append(f"## 구간 {i}/{len(chunks)}\n" + (msg.get("content") or "(정리 실패)").strip())
    return "\n\n".join(parts)


# ---- Tool ② 결과지 갱신 ----------------------------------------------------

def _numbered(errs: list[str]) -> str:
    return "\n".join(f"{i}. {e}" for i, e in enumerate(errs, start=1))


def update_result_sheet(customer_id: str, project_id: str, meeting_no: int, summary: str = "", items: list | None = None,
                        goals: list | None = None, requirements: list | None = None, stakeholders: list | None = None,
                        customer_profile: list | None = None, next_questions: list | None = None,
                        deal_hints: list | None = None) -> str:
    """이번 회의에서 확인된 것을 결과지에 반영한다. 근거가 녹취의 실제 발언과 맞지 않으면 받지 않는다."""
    d, key = _project(customer_id, project_id)
    n = int(meeting_no)
    tp = store.transcript_path(customer_id, project_id, n)
    if not tp.exists():
        raise FileNotFoundError(f"녹취 파일이 없습니다: {tp.name}")
    text = tp.read_text(encoding="utf-8")
    utts = store.utterances(text)
    base = memory._read_json(memory.pending_path(d, n)) or memory.load_before(key, d, n)["sheet"]
    if store.load_meeting(customer_id, project_id, n).get("scope") == "내부":
        # 내부 회의에서는 고객이 말한 사실을 새로 채우지 않는다. 우리가 정한 것만 반영한다
        if items or goals or stakeholders or customer_profile:
            raise ValueError("결과지 갱신 거부 — 내부 회의다. 고객이 말한 사실(items · goals · stakeholders · customer_profile)은 고객과의 회의에서만 채운다. "
                             "우리 대응(requirements의 response), 다음에 물을 질문(next_questions), 거래 조건 제안(deal_hints)만 보낸다.")
        summary = (summary or "").strip() or base.get("summary") or "고객과의 회의 전이다."
    upd = {"summary": summary, "items": items, "goals": goals, "requirements": requirements,
           "stakeholders": stakeholders, "next_questions": next_questions, "deal_hints": deal_hints}
    ours = [m["name"] for m in store.load_org().get("members", [])] + [t["name"] for t in store.team(customer_id, project_id)]
    new, errs, extra = sheet.apply(base, upd, n, utts, store.known_names(customer_id, project_id), text, own_names=ours)

    g, profile = sheet.Grounder(utts, n), []  # 고객 단위 사실(생산품·주요 설비·기존 시스템)은 고객정보에 쌓는다
    for row in customer_profile or []:
        field, value = row.get("field"), (row.get("value") or "").strip()
        if field not in store.PROFILE_FIELDS or not value:
            g.errors.append(f"customer_profile: field는 {' · '.join(store.PROFILE_FIELDS)} 중 하나이고 value가 있어야 한다.")
            continue
        ev = g.find(f"customer_profile '{field}'", row.get("evidence"))
        if ev:
            profile.append((field, value, ev))
    errs = errs + g.errors
    if errs:
        raise ValueError("결과지 갱신 거부 — 아래를 고쳐 update_result_sheet를 다시 호출하라. 하나라도 어긋나면 전체가 반영되지 않는다.\n" + _numbered(errs))

    memory._write_json(memory.pending_path(d, n), new)
    for person in extra["contacts"]:
        store.upsert_contact(customer_id, person, f"{project_id} {n}차 회의 · AI 등록")
    if profile:
        cust = store.load_customer(customer_id)
        for field, value, ev in profile:
            cust["profile"][field] = {"value": value, "evidence": ev, "project": project_id, "status": sheet.DRAFT}
        store.save_customer(customer_id, cust)
    missing = sheet.gate(new)["missing_labels"]
    return (f"결과지 갱신 완료 — {sheet.gate_line(new)}. AI가 채운 값은 사람이 확인하기 전까지 초안으로 남는다.\n"
            f"아직 미확보: {', '.join(missing) or '없음'}")


def confirm_sheet(customer_id: str, project_id: str, kind: str, target: str, who: str = "담당자") -> int:
    """사람이 화면에서 초안을 확인한다(에이전트의 도구가 아니다). 가장 최근 회차의 결과지를 고친다."""
    d, _ = _project(customer_id, project_id)
    p = memory.state_path(d, memory.latest_no(d))
    st = memory._read_json(p)
    if not st:
        raise FileNotFoundError("확인할 결과지가 아직 없습니다. 에이전트를 먼저 실행하세요.")
    count = sheet.confirm(st["sheet"], kind, target, who)
    memory._write_json(p, st)
    return count


def edit_sheet(customer_id: str, project_id: str, kind: str, key: str, fields: dict, who: str = "담당자") -> None:
    """사람이 결과지 값을 직접 고친다. 고친 값은 확인된 값이고, 변경 이력에 남는다."""
    d, _ = _project(customer_id, project_id)
    p = memory.state_path(d, memory.latest_no(d))
    st = memory._read_json(p)
    if not st:
        raise FileNotFoundError("수정할 결과지가 아직 없습니다.")
    sh, at = st["sheet"], time.strftime("%Y-%m-%d %H:%M")

    def mark(r: dict) -> None:
        r["status"], r["confirmed_by"] = sheet.CONFIRMED, who
        r.pop("previous", None)
        r.setdefault("history", []).append({"action": "수정", "by": who, "at": at, "value": sheet.brief(r)})

    if kind == "summary":
        sh["summary"] = str(fields.get("value", "")).strip()
    elif kind == "questions":
        sh["next_questions"] = [str(q).strip() for q in fields.get("value", []) if str(q).strip()]
    elif kind == "item":
        value = str(fields.get("value", "")).strip()
        if key not in sheet.TEXT_ITEMS or not value:
            raise ValueError("고칠 수 없는 항목이거나 값이 비었습니다.")
        r = sh["items"].setdefault(key, {"value": "", "evidence": [], "history": []})
        r["value"] = value
        mark(r)
    elif kind in ("goal", "requirement"):
        rows = sh["goals"] if kind == "goal" else sh["requirements"]
        allowed = ("goal", "current", "target", "measure") if kind == "goal" else ("title", "background", "response", "response_status")
        r = next((x for x in rows if x["id"] == key), None)
        if r is None:
            raise FileNotFoundError(f"{key} 줄을 찾지 못했습니다.")
        if "response_status" in fields and fields["response_status"] not in sheet.RESPONSE_STATUS:
            raise ValueError(f"대응 상태는 {' · '.join(sheet.RESPONSE_STATUS)} 중 하나입니다.")
        for f in allowed:
            if f in fields:
                r[f] = str(fields[f]).strip()
        mark(r)
    else:
        raise ValueError(f"고칠 수 없는 종류입니다: {kind}")
    memory._write_json(p, st)


# ---- Tool ③ 회의록 저장 -----------------------------------------------------

def tidy_minutes(markdown: str) -> str:
    """회의록에 남으면 안 되는 것을 걷어 낸다: 양식의 안내 문구를 그대로 옮긴 머리말, 발화 번호(#12) 표시."""
    markdown = re.sub(r"이 얘기가 왜 나왔고 어떻게 정리됐는지\s*[:：]\s*", "", markdown)
    return re.sub(r"[ 	]*\(#\d+(?:\s*[,·→~-]\s*#\d+)*\)", "", markdown)


def save_minutes(customer_id: str, project_id: str, meeting_no: int, markdown: str) -> str:
    """규칙 검사를 통과한 회의록을 초안으로 저장한다. confirm_minutes가 불려야 프로젝트 기억과 결과지에 반영된다(앱은 실행 직후 부른다)."""
    d, key = _project(customer_id, project_id)
    n = int(meeting_no)
    markdown = tidy_minutes(markdown)
    prev = memory.load_before(key, d, n)
    pending = memory._read_json(memory.pending_path(d, n))
    if VALIDATE:
        tp = store.transcript_path(customer_id, project_id, n)
        known = " ".join(store.known_names(customer_id, project_id) + [a["name"] for a in prev["attendees"]])
        known += tp.read_text(encoding="utf-8") if tp.exists() else ""
        errs = memory.validate(markdown, known)
        if pending is None and _run_opts.get("update_sheet", True):
            errs.insert(0, "이번 회차의 결과지 갱신이 없다. 먼저 update_result_sheet로 결과지를 갱신한 뒤 회의록을 저장한다.")
        if errs:
            raise ValueError("저장 거부 — 아래를 고친 회의록 전문으로 save_minutes를 다시 호출하라.\n" + _numbered(errs))
    p = store.minutes_path(customer_id, project_id, n)
    p.write_text(markdown.strip() + "\n", encoding="utf-8")
    (d / "state" / f"minutes_{n:02d}.agent.md").unlink(missing_ok=True)  # 사람이 고치기 전 원문(edit.py)은 새 초안과 맞지 않는다
    st = memory.update_state(prev, markdown, n, pending)
    memory._write_json(memory.draft_path(d, n), st)  # confirm_minutes가 불릴 때까지 초안으로 둔다
    memory.pending_path(d, n).unlink(missing_ok=True)
    m = store.load_meeting(customer_id, project_id, n)
    m["minutes"], m["changes"] = {"status": "초안"}, sheet.diff(prev["sheet"], st["sheet"])
    store.save_meeting(customer_id, project_id, n, m)
    shown = p.relative_to(store.ROOT).as_posix() if p.is_relative_to(store.ROOT) else str(p)
    return (f"저장 완료(초안): {shown} ({len(markdown)}자) · 결과지 변경 제안 {len(m['changes'])}건 · "
            f"담당자가 회의록을 확정하면 반영된다 · 반영되면 결과지 {sheet.gate_line(st['sheet'])} · 지난 액션 이행률 {memory.kpi(st)['이행률']}")


def save_graph(customer_id: str, d: Path, st: dict) -> Path:
    p = d / "state" / f"graph_{st['meeting_no']:02d}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    company = store.load_customer(customer_id)["name"]
    p.write_text(json.dumps(graph.build_graph(st, company), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return p


# ---- Tool ④ 이력 검색 -------------------------------------------------------

def search_project_history(customer_id: str, project_id: str, query: str, meeting_from: int | None = None,
                           meeting_to: int | None = None, kind: str | None = None) -> str:
    """지난 회의 녹취·회의록에서 원문 근거를 찾는다. 질문에 아는 인물이 있으면 관계 정보를 먼저 붙인다."""
    d, key = _project(customer_id, project_id)
    latest = memory.latest_no(d)
    st = memory.load_before(key, d, latest + 1)
    g = graph.build_graph(st, store.load_customer(customer_id)["name"])
    named = [a["name"] for a in st["attendees"] if a["name"] in query]
    head = [graph.person_view(g, n) for n in named]
    results = retrieval.search(key, d, d, query, meeting_from, meeting_to, kind, latest_meeting=latest, speakers=named or None)
    return "\n\n".join([h for h in head if h] + [retrieval.format_results(results)])


def ask_sop(question: str) -> str:
    """SOP 서브에이전트에 묻는다. 실행 중인 LLM(마스킹 포함)을 넘겨, 질문에 섞인 실명이 그대로 나가지 않게 한다."""
    return consult_sop(question, llm=_run_llm)


# ---- 스키마 (모델에게 알려주는 도구 설명) --------------------------------

_IDS = {"customer_id": {"type": "string", "description": "고객코드 (예: KR0001)"},
        "project_id": {"type": "string", "description": "프로젝트코드 (예: P001)"}}
_NO = {"type": "integer", "description": "이번 회차 번호"}
_EV = {"type": "string", "description": "근거: 이번 녹취에서 그 내용을 말한 발언의 번호(예: #12). 번호가 없을 때만 발언을 그대로 옮긴다"}


def _fn(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {"name": name, "description": description,
                                             "parameters": {"type": "object", "properties": properties, "required": required}}}


def _rows(properties: dict, required: list[str], description: str) -> dict:
    return {"type": "array", "description": description,
            "items": {"type": "object", "properties": properties, "required": required}}


TOOL_SCHEMAS = [
    _fn("read_project_context",
        "고객·프로젝트·담당자와 프로젝트 기억(지난 회의 누적, 결과지 현황, 아직 모르는 항목)을 읽는다. 일을 시작할 때 먼저 부른다.",
        {**_IDS, "meeting_no": _NO}, ["customer_id", "project_id", "meeting_no"]),
    _fn("read_transcript", "이번 회차의 회의 녹취/메모를 읽는다. 긴 녹취는 구간별 정리본으로 돌려준다.",
        {**_IDS, "meeting_no": _NO}, ["customer_id", "project_id", "meeting_no"]),
    _fn("update_result_sheet",
        "이번 회의에서 확인된 것을 리드 결과지에 반영한다. 새로 알게 된 것과 바뀐 것만 보낸다. 근거가 녹취의 실제 발언과 맞지 않으면 거부 사유가 돌아오니 고쳐서 다시 부른다. 여러 번 불러도 된다.",
        {**_IDS, "meeting_no": _NO,
         "summary": {"type": "string", "description": "지금까지 확인된 고객 상황 두세 문장(지난 요약에 이번 회의를 더해 다시 쓴다)"},
         "items": _rows({"code": {"type": "string", "enum": sheet.TEXT_ITEMS,
                                  "description": "S1 현행 운영 방식 · S2 주요 이슈 · B1 예산 상태 · B2 사업예산·예상 수주금액 · B3 긴급도·착수 희망시기 · B4 계약 예상시기 · X1 경쟁 상황 · C1~C7은 결과지 현황에 나온 경우에만"},
                         "value": {"type": "string"}, "evidence": _EV}, ["code", "value", "evidence"], "글로 적는 항목"),
         "goals": _rows({"goal": {"type": "string"}, "current": {"type": "string", "description": "현재 수준"},
                         "target": {"type": "string", "description": "희망 수준"}, "measure": {"type": "string", "description": "확인 기준"},
                         "evidence": _EV}, ["goal", "evidence"], "비즈니스 목표 — 풀리면 무엇이 좋아지는가"),
         "requirements": _rows({"title": {"type": "string"}, "background": {"type": "string", "description": "고객배경: 왜 필요한가"},
                                "response": {"type": "string", "description": "당사 대응: 우리가 내놓은 해결안. 회의에서 말하지 않았으면 비운다"},
                                "response_status": {"type": "string", "enum": sheet.RESPONSE_STATUS}, "evidence": _EV},
                               ["title", "evidence"], "핵심 요구사항과 당사 대응"),
         "stakeholders": _rows({"name": {"type": "string", "description": "사람 이름. 직함만 아는 사람은 넣지 않는다"},
                                "dept": {"type": "string"}, "title": {"type": "string"},
                                "roles": {"type": "array", "items": {"type": "string", "enum": sheet.ROLES}},
                                "phone": {"type": "string"}, "email": {"type": "string"}, "evidence": _EV},
                               ["name"], "고객 측 이해관계자와 영업상 역할. 역할을 붙이면 근거가 있어야 한다. 우리 쪽 사람은 적지 않는다"),
         "customer_profile": _rows({"field": {"type": "string", "enum": store.PROFILE_FIELDS}, "value": {"type": "string"}, "evidence": _EV},
                                   ["field", "value", "evidence"], "프로젝트와 무관하게 고객에 대해 알게 된 사실"),
         "deal_hints": _rows({"field": {"type": "string", "enum": list(store.DEAL_FIELDS)},
                              "value": {"type": "string", "description": " / ".join(f"{f}: {'·'.join(v)}" for f, v in store.DEAL_FIELDS.items())},
                              "evidence": _EV}, ["field", "value", "evidence"],
                             "거래 조건의 단서(정부지원·입찰, 하도급, 하드웨어 납품, 비밀유지, 컨소시엄·파트너사, 시연)가 나왔을 때의 제안. 값은 사람이 정한다"),
         "next_questions": {"type": "array", "items": {"type": "string"}, "description": "아직 모르는 항목을 채우려고 다음 회의에서 물을 질문"}},
        ["customer_id", "project_id", "meeting_no", "summary"]),
    _fn("save_minutes",
        "표준 양식으로 작성한 회의록 마크다운을 초안으로 저장한다. 결과지를 갱신한 뒤에 부른다. 규칙 검사에 걸리면 저장 거부 사유가 돌아오니 고쳐서 다시 부른다.",
        {**_IDS, "meeting_no": _NO, "markdown": {"type": "string", "description": "회의록 전문(마크다운)"}},
        ["customer_id", "project_id", "meeting_no", "markdown"]),
    _fn("search_project_history",
        "지난 회의의 녹취 발화·회의록 항목에서 원문을 찾는다. 이번 녹취가 지난 회의 내용을 가리키는데 프로젝트 기억만으로 정확한 발언·숫자·날짜를 알 수 없을 때, 또는 이번 말이 지난 말과 어긋나 보일 때 부른다.",
        {**_IDS, "query": {"type": "string", "description": "찾을 내용. 고객이 쓴 말·금액·날짜·인물 이름"},
         "meeting_from": {"type": "integer"}, "meeting_to": {"type": "integer"},
         "kind": {"type": "string", "enum": ["발화", "회의록"]}},
        ["customer_id", "project_id", "query"]),
    _fn("consult_sop",
        "영업 SOP 담당자(서브에이전트)에게 묻는다. 어떤 항목을 확인·기록해야 하는지, 예산·의사결정 조직·미합의를 SOP상 어떻게 다뤄야 하는지 애매할 때 부른다.",
        {"question": {"type": "string", "description": "SOP 용어를 넣은 한 문장 질문"}}, ["question"]),
]

TOOL_FUNCTIONS = {
    "read_project_context": read_project_context,
    "read_transcript": read_transcript,
    "update_result_sheet": update_result_sheet,
    "save_minutes": save_minutes,
    "search_project_history": search_project_history,
    "consult_sop": ask_sop,
}


# ---- 게이트: 회의 → 회의록 → 결과지 (에이전트의 도구가 아니다. 사람과 화면이 쓴다) -----------

GATE_STEPS = {"예정": 0, "녹취 확보": 1, "회의록 검토": 1, "결과지 확인": 2, "완료": 3}  # 지나온 문의 수


def gate_of(customer_id: str, project_id: str, meeting_no: int) -> dict:
    """회의 한 건이 세 문 가운데 어디에 서 있는가.

    ① 회의: 녹취가 있다  ② 회의록: 초안이 반영됐다  ③ 결과지: 이 회의가 넣은 값을 모두 확인했다
    '회의록 검토'는 초안이 반영되기 전의 상태다. 앱은 실행 직후 반영하므로 터미널에서 --confirm 없이 돌렸을 때만 머문다.
    """
    d, key = _project(customer_id, project_id)
    n, pending = int(meeting_no), 0
    if not store.transcript_path(customer_id, project_id, n).exists():
        state = "예정"
    elif memory.draft_path(d, n).exists():
        state = "회의록 검토"
    elif not memory.state_path(d, n).exists():
        state = "녹취 확보"
    else:
        pending = sheet.pending_for(memory.latest(key, d)["sheet"], n)
        state = "결과지 확인" if pending else "완료"
    todo = {"예정": "녹취 필요", "녹취 확보": "실행 대기", "회의록 검토": "회의록 확정 대기",
            "결과지 확인": f"결과지 확인 대기 {pending}건", "완료": ""}[state]
    return {"state": state, "passed": GATE_STEPS[state], "pending": pending, "todo": todo}


def check_gate(customer_id: str, project_id: str, meeting_no: int) -> None:
    """앞선 회의가 반영되지 않았으면(원문은 있는데 state가 없으면) 다음 회의를 처리하지 않는다(반영되지 않은 내용 위에 쌓지 않는다)."""
    d, _ = _project(customer_id, project_id)
    for m in store.meeting_nos(customer_id, project_id):
        if m < int(meeting_no) and store.transcript_path(customer_id, project_id, m).exists() and not memory.state_path(d, m).exists():
            raise ValueError(f"{m}차 회의록이 아직 확정되지 않았습니다. {m}차를 먼저 처리하고 회의록을 확정한 뒤 {meeting_no}차를 실행하세요.")


def confirm_minutes(customer_id: str, project_id: str, meeting_no: int, who: str = "담당자") -> dict:
    """회의록 초안을 반영한다. 이때 프로젝트 기억이 갱신되고, 결과지 변경이 AI 초안으로 들어간다.
    사람이 누르는 확정 단계는 없앴고, 앱이 실행 직후 who="자동 반영"으로 부른다(터미널은 --confirm)."""
    d, _ = _project(customer_id, project_id)
    n = int(meeting_no)
    st = memory._read_json(memory.draft_path(d, n))
    if not st:
        raise FileNotFoundError(f"{n}차에 확정할 회의록 초안이 없습니다.")
    memory.save_snapshot(d, st)
    memory.draft_path(d, n).unlink()
    save_graph(customer_id, d, st)
    m = store.load_meeting(customer_id, project_id, n)
    m["minutes"] = {"status": "확정", "confirmed_by": who, "confirmed_at": time.strftime("%Y-%m-%d %H:%M")}
    store.save_meeting(customer_id, project_id, n, m)
    return gate_of(customer_id, project_id, n)


def meeting_view(customer_id: str, project_id: str, meeting_no: int) -> dict:
    return {**store.load_meeting(customer_id, project_id, meeting_no), "gate": gate_of(customer_id, project_id, meeting_no)}


def all_meetings() -> list[dict]:
    """전체 회의 목록: 고객·프로젝트를 가로질러 최근 것부터. 홈의 '내 할 일'도 여기서 나온다."""
    rows = []
    for c in store.list_tree():
        for p in c["projects"]:
            for n in p["meetings"]:
                m = meeting_view(c["customer_id"], p["project_id"], n)
                rows.append({"customer_id": c["customer_id"], "customer": c["name"], "project_id": p["project_id"], "project": p["name"],
                             "no": n, "date": m["date"], "time": m["time"], "type": m["type"], "place": m["place"],
                             "attendees": m["attendees"], "changes": len(m["changes"]), "gate": m["gate"]})
    return sorted(rows, key=lambda r: (r["date"] or "9999", r["no"]), reverse=True)


def set_action(customer_id: str, project_id: str, action_id: str, status: str, who: str = "담당자", note: str = "") -> dict:
    """액션아이템의 수행 기록을 사람이 남긴다(완료 · 진행 중 · 미착수). 다음 회의 때 에이전트가 이 상태를 이어받는다."""
    if status not in memory.ACTION_RESULTS:
        raise ValueError(f"상태는 {' · '.join(memory.ACTION_RESULTS)} 중 하나입니다.")
    d, _ = _project(customer_id, project_id)
    p = memory.state_path(d, memory.latest_no(d))
    st = memory._read_json(p)
    item = next((a for a in (st or {}).get("action_items", []) if a["id"] == action_id), None)
    if item is None:
        raise FileNotFoundError(f"액션아이템을 찾지 못했습니다: {action_id}")
    item["status"] = status
    item.setdefault("log", []).append({"at": time.strftime("%Y-%m-%d %H:%M"), "by": who, "status": status, "note": note.strip()})
    memory._write_json(p, st)
    return item
