"""딜 기억(상태 파일)과 저장 전 규칙 검사.

회의록 마크다운은 사람이 읽는 산출물이고, 상태 파일(state_NN.json)은 에이전트가 다음 회의에 들고 가는 기억이다.
상태 파일에는 사실만 남긴다: 참석자, 회차별 합의, 현재 미합의, 액션아이템(담당·기한·상태), Q코드 확보 현황.

- 회차마다 스냅숏을 남긴다(state_01.json, state_02.json …). N차를 다시 돌릴 때는 N-1차 스냅숏을 읽는다.
- 스냅숏이 없는 과거 회의록(Week 3 산출물)은 회의록을 차례로 다시 읽어 상태를 복원한다.
- 저장 위치를 바꿀 때(예: AWS S3·DynamoDB)는 _read_json / _write_json 두 함수만 고친다.
"""

from __future__ import annotations

import difflib
import json
import re
from pathlib import Path

Q_CODES = [f"Q{i:02d}" for i in range(1, 12)]
Q_STATUSES = {"이번 확보", "기존 확보", "미확보"}
ACTION_RESULTS = ("완료", "진행 중", "미착수")
EMPTY = {"", "-", "확인 불가", "미정", "없음", "기한 미정"}
REQUIRED_SECTIONS = ["참석자", "지난 회의 액션아이템 점검", "논의 요지", "고객 요구·문제점",
                     "합의 사항", "미합의 사항", "액션아이템", "확보된 표준 질문 항목"]


# ---- 저장소 (여기만 바꾸면 저장 위치가 바뀐다) -----------------------------

def _read_json(path: Path) -> dict | None:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# ---- 회의록 파싱 -----------------------------------------------------------

def split_sections(md: str) -> dict[int, str]:
    """'## N. 제목' 단위로 자른다. 키는 절 번호."""
    out, cur, buf = {}, None, []
    for line in md.splitlines():
        m = re.match(r"^##\s*(\d+)\.\s*(.*)$", line.strip())
        if m:
            if cur is not None:
                out[cur] = "\n".join(buf)
            cur, buf = int(m.group(1)), [line]
        elif cur is not None:
            buf.append(line)
    if cur is not None:
        out[cur] = "\n".join(buf)
    return out


def parse_table(section: str) -> tuple[list[str], list[list[str]]]:
    """절 안의 첫 표를 (헤더, 행 목록)으로. 구분선(| --- |)은 건너뛴다."""
    lines = [l.strip() for l in section.splitlines() if l.strip().startswith("|")]
    if not lines:
        return [], []
    cells = lambda l: [c.strip() for c in l.strip().strip("|").split("|")]
    header = cells(lines[0])
    rows = [cells(l) for l in lines[1:] if not re.fullmatch(r"\|?[\s:\-|]+\|?", l)]
    return header, [r for r in rows if any(c for c in r)]


def bullets(section: str) -> list[str]:
    return [l.strip()[2:].strip() for l in section.splitlines() if l.strip().startswith("- ")]


def person_names(cell: str) -> list[str]:
    """'김도현·박성민(당사)' → ['김도현', '박성민']. 괄호 속 설명은 버린다."""
    cell = re.sub(r"\([^)]*\)", "", cell)
    return [p.strip() for p in re.split(r"[·,/、&]| 및 ", cell) if re.fullmatch(r"[가-힣]{2,4}", p.strip())]


# ---- 저장 전 규칙 검사 ------------------------------------------------------

def validate(md: str, known_text: str, prev: dict | None) -> list[str]:
    """회의록이 저장 조건을 채우는지 코드로 검사한다. 위반 사항 목록(빈 목록이면 통과)."""
    errs: list[str] = []
    secs = split_sections(md)
    for i, title in enumerate(REQUIRED_SECTIONS, start=1):
        if i not in secs:
            errs.append(f"'## {i}. {title}' 절이 없다. 양식의 8개 절을 모두 쓴다.")

    for no, body in secs.items():
        header, rows = parse_table(body)
        for r in rows:
            if header and len(r) != len(header):
                errs.append(f"{no}절 표의 행 칸 수({len(r)})가 헤더({len(header)})와 다르다: '{' | '.join(r)[:60]}'. 셀 안의 |는 ·로 바꾼다.")

    # 7절: 담당·기한이 모두 있는 일만 액션아이템
    if 7 in secs:
        for r in parse_table(secs[7])[1]:
            if len(r) < 3:
                continue
            task, owner, due = r[0], r[1], r[2]
            if owner in EMPTY or due in EMPTY:
                errs.append(f"7절 '{task[:30]}'의 담당 또는 기한이 비어 있다('{owner}' / '{due}'). 6절 미합의 사항으로 옮기고 다음 행동을 적는다.")

    # 8절: Q01~Q11을 한 행씩, 상태는 세 가지 중 하나
    if 8 in secs:
        rows = parse_table(secs[8])[1]
        seen = {}
        for r in rows:
            if r and re.fullmatch(r"Q\d{2}", r[0]):
                seen[r[0]] = r
        missing = [q for q in Q_CODES if q not in seen]
        if missing:
            errs.append(f"8절 표에 {', '.join(missing)} 행이 없다. Q01~Q11을 모두 한 행씩 적는다.")
        prev_q = (prev or {}).get("questions", {})
        for q, r in seen.items():
            status = r[2] if len(r) > 2 else ""
            if status not in Q_STATUSES:
                errs.append(f"8절 {q}의 상태 '{status}'는 허용 값이 아니다. '이번 확보'·'기존 확보'·'미확보' 중 하나로 쓴다.")
            elif prev_q.get(q, {}).get("status") == "확보" and status == "미확보":
                errs.append(f"8절 {q}는 {prev_q[q].get('since')}차 회의에서 이미 확보되었다. '기존 확보'로 적는다.")

    # 인물: 참석자·담당자는 녹취·고객정보·딜 기억에 있는 이름이어야 한다(지어낸 인물 차단)
    names = []
    if 1 in secs:
        names += [r[1] for r in parse_table(secs[1])[1] if len(r) > 1]
    if 7 in secs:
        names += [n for r in parse_table(secs[7])[1] if len(r) > 1 for n in person_names(r[1])]
    for n in sorted({n for n in names if re.fullmatch(r"[가-힣]{2,4}", n)}):
        if n not in known_text:
            errs.append(f"'{n}'은(는) 녹취·고객정보·이전 참석자 어디에도 없는 이름이다. 녹취의 표기를 확인해 고친다.")
    return errs


# ---- 상태 추출·갱신 ----------------------------------------------------------

def empty_state(deal_id: str) -> dict:
    return {"deal_id": deal_id, "meeting_no": 0, "meeting_dates": {}, "attendees": [], "decisions": [],
            "open_issues": [], "action_items": [], "questions": {}}


def update_state(prev: dict, md: str, meeting_no: int) -> dict:
    """이번 회의록에서 사실을 뽑아 직전 상태에 덧붙인다. 규칙 검사를 통과한 회의록을 전제로 한다."""
    st = json.loads(json.dumps(prev))  # 깊은 복사
    st["meeting_no"] = meeting_no
    secs = split_sections(md)
    m = re.search(r"일시:\s*(\d{4}-\d{2}-\d{2})", md)
    if m:
        st["meeting_dates"][str(meeting_no)] = m.group(1)

    # 1절 참석자: 이름 기준으로 합치고 최신 직위로 갱신
    by_name = {a["name"]: a for a in st["attendees"]}
    for r in parse_table(secs.get(1, ""))[1]:
        if len(r) >= 3 and re.fullmatch(r"[가-힣]{2,4}", r[1]):
            a = by_name.setdefault(r[1], {"name": r[1], "side": r[0], "first_meeting": meeting_no})
            a.update(side=r[0], title=r[2], note=r[3] if len(r) > 3 else "", last_meeting=meeting_no)
    st["attendees"] = list(by_name.values())

    # 2절: 지난 액션아이템 결과를 기존 항목에 반영(항목명이 가장 비슷한 것과 짝짓기)
    open_items = [a for a in st["action_items"] if a["status"] != "완료"]
    for r in parse_table(secs.get(2, ""))[1]:
        if len(r) < 4 or not open_items:
            continue
        best = max(open_items, key=lambda a: difflib.SequenceMatcher(None, a["task"], r[0]).ratio())
        if difflib.SequenceMatcher(None, best["task"], r[0]).ratio() >= 0.4:
            result = next((s for s in ACTION_RESULTS if r[3].startswith(s)), None)
            if result:
                best["status"], best["checked_at"], best["result_note"] = result, meeting_no, r[3]

    # 5절 합의는 회차별로 누적, 6절 미합의는 최신 회차 것으로 교체
    st["decisions"] = [d for d in st["decisions"] if d["meeting"] != meeting_no]
    st["decisions"] += [{"meeting": meeting_no, "text": b} for b in bullets(secs.get(5, ""))]
    st["open_issues"] = [{"meeting": meeting_no, "text": b} for b in bullets(secs.get(6, ""))]

    # 7절 새 액션아이템
    st["action_items"] = [a for a in st["action_items"] if a["meeting"] != meeting_no]
    for i, r in enumerate(parse_table(secs.get(7, ""))[1], start=1):
        if len(r) >= 3:
            st["action_items"].append({"id": f"A{meeting_no:02d}-{i}", "meeting": meeting_no, "task": r[0],
                                       "owner": r[1], "due": r[2], "status": "미착수"})

    # 8절 Q코드: 이번 확보는 이번 회차로, 기존 확보는 처음 확보한 회차를 유지
    for r in parse_table(secs.get(8, ""))[1]:
        if len(r) >= 3 and re.fullmatch(r"Q\d{2}", r[0]):
            q, status, note = r[0], r[2], (r[3] if len(r) > 3 else "")
            old = st["questions"].get(q, {})
            if status == "미확보":
                st["questions"][q] = {"status": "미확보", "next_question": note}
            elif old.get("status") == "확보":
                old.setdefault("evidence", note)
            else:
                st["questions"][q] = {"status": "확보", "since": meeting_no, "evidence": note}
    # Week 3 형식(목록형 8절)도 읽는다: "새로 확보: Q06"
    for q in re.findall(r"새로 확보[^\n]*", secs.get(8, "")):
        for code in re.findall(r"Q\d{2}", q):
            if st["questions"].get(code, {}).get("status") != "확보":
                st["questions"][code] = {"status": "확보", "since": meeting_no, "evidence": "(Week 3 형식 회의록 — 근거 없음)"}
    for q in re.findall(r"미확보:[^\n]*", secs.get(8, "")):  # 최신 회의록이 미확보라고 하면 그쪽을 따른다
        for code in re.findall(r"Q\d{2}", q):
            st["questions"][code] = {"status": "미확보", "next_question": ""}
    return st


def kpi(st: dict) -> dict:
    got = sum(1 for q in Q_CODES if st["questions"].get(q, {}).get("status") == "확보")
    past = [a for a in st["action_items"] if a["meeting"] < st["meeting_no"]]
    done = sum(1 for a in past if a["status"] == "완료")
    return {"확보율": f"{got}/11 ({round(got / 11 * 100)}%)",
            "이행률": f"{done}/{len(past)} ({round(done / len(past) * 100)}%)" if past else "해당 없음(첫 회의)"}


# ---- 에이전트에게 보여줄 요약 ------------------------------------------------

def render_state(st: dict) -> str:
    """상태 파일을 짧은 마크다운으로. 직전 회의록 전문 대신 이것을 컨텍스트에 넣는다."""
    if st["meeting_no"] == 0:
        return "(없음 — 이번이 첫 회의)"
    k = kpi(st)
    lines = [f"누적 회차: 1~{st['meeting_no']}차 · 확보율 {k['확보율']} · 지난 액션 이행률 {k['이행률']}", "",
             "### 알려진 참석자", "| 구분 | 이름 | 직위/역할 | 비고 |", "| --- | --- | --- | --- |"]
    lines += [f"| {a['side']} | {a['name']} | {a.get('title', '')} | {a.get('note', '')} |" for a in st["attendees"]]
    lines += ["", "### 회차별 합의 사항(누적)"]
    lines += [f"- ({d['meeting']}차) {d['text']}" for d in st["decisions"]] or ["- 없음"]
    lines += ["", f"### 현재 미합의 사항({st['meeting_no']}차 기준)"]
    lines += [f"- {d['text']}" for d in st["open_issues"]] or ["- 없음"]
    lines += ["", "### 점검할 액션아이템(완료되지 않은 것 전부 — 이번 회의록 2절에 옮겨 적는다)",
              "| 항목 | 담당 | 기한 | 현재 상태 |", "| --- | --- | --- | --- |"]
    open_items = [a for a in st["action_items"] if a["status"] != "완료"]
    lines += [f"| {a['task']} | {a['owner']} | {a['due']} | {a['status']} ({a['meeting']}차 발생) |" for a in open_items] or ["| 없음 | - | - | - |"]
    lines += ["", "### 표준 질문 확보 현황", "| 코드 | 상태 | 근거 · 다음 질문 |", "| --- | --- | --- |"]
    for q in Q_CODES:
        v = st["questions"].get(q, {"status": "미확보"})
        note = f"{v.get('since')}차 확보 · {v.get('evidence', '')}" if v["status"] == "확보" else v.get("next_question", "")
        lines.append(f"| {q} | {v['status']} | {note} |")
    return "\n".join(lines)


# ---- 스냅숏 입출력 ----------------------------------------------------------

def state_path(deal_dir: Path, meeting_no: int) -> Path:
    return deal_dir / "state" / f"state_{meeting_no:02d}.json"


def save_snapshot(deal_dir: Path, st: dict) -> Path:
    p = state_path(deal_dir, st["meeting_no"])
    _write_json(p, st)
    return p


def load_before(deal_id: str, deal_dir: Path, minutes_dir: Path, meeting_no: int) -> dict:
    """meeting_no 직전까지의 상태. 스냅숏이 없으면 앞선 회의록을 차례로 읽어 복원한다."""
    for n in range(meeting_no - 1, 0, -1):
        snap = _read_json(state_path(deal_dir, n))
        if snap:
            st, start = snap, n + 1
            break
    else:
        st, start = empty_state(deal_id), 1
    for n in range(start, meeting_no):
        p = minutes_dir / f"minutes_{n:02d}.md"
        if p.exists():
            st = update_state(st, p.read_text(encoding="utf-8"), n)
    return st
