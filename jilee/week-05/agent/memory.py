"""프로젝트 기억(상태 파일)과 회의록 저장 전 규칙 검사.

회의록 마크다운은 사람이 읽는 근거 문서이고, 상태 파일(state_NN.json)은 에이전트가 다음 회의에 들고 가는 기억이다.
상태 파일에는 사실만 남긴다: 참석자, 회차별 합의, 현재 미합의, 액션아이템(담당·기한·상태), 그리고 결과지(sheet).

- 에이전트가 회의를 처리하면 초안(draft_NN.json)이 생기고, 담당자가 회의록을 확정해야 스냅숏(state_NN.json)이 된다.
  확정되지 않은 회의는 기억에 들어오지 않는다(회의 → 회의록 → 결과지 게이트의 두 번째 문).
- N차를 다시 돌릴 때는 N-1차 스냅숏을 읽는다.
- 저장 위치를 바꿀 때(예: AWS S3·DynamoDB)는 _read_json / _write_json 두 함수만 고친다.
"""

from __future__ import annotations

import difflib
import json
import re
from pathlib import Path

from . import sheet

ACTION_RESULTS = ("완료", "진행 중", "미착수")
EMPTY = {"", "-", "확인 불가", "미정", "없음", "기한 미정"}
REQUIRED_SECTIONS = ["참석자", "지난 회의 액션아이템 점검", "논의 요지", "고객 요구·문제점",
                     "합의 사항", "미합의 사항", "액션아이템"]


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

def validate(md: str, known_text: str) -> list[str]:
    """회의록이 저장 조건을 채우는지 코드로 검사한다. 위반 사항 목록(빈 목록이면 통과)."""
    errs: list[str] = []
    secs = split_sections(md)
    for i, title in enumerate(REQUIRED_SECTIONS, start=1):
        if i not in secs:
            errs.append(f"'## {i}. {title}' 절이 없다. 양식의 7개 절을 모두 쓴다.")

    for no, body in secs.items():
        header, rows = parse_table(body)
        for r in rows:
            if header and len(r) != len(header):
                errs.append(f"{no}절 표의 행 칸 수({len(r)})가 헤더({len(header)})와 다르다: '{' | '.join(r)[:60]}'. 셀 안의 |는 ·로 바꾼다.")

    # 3절: 오간 말을 늘어놓지 않고 주제로 묶어 결론을 제목에 적는다(읽는 사람이 요지를 바로 잡게)
    if 3 in secs and len(bullets(secs[3])) > 3 and not re.search(r"^###\s+\S", secs[3], re.M):
        errs.append("3절 논의 요지가 한 줄 목록이다. '### 주제 — 결론 한 줄'로 3~6개 주제로 묶고, 결론을 가른 논의에 **[중요]**를 붙인다.")

    # 7절: 담당·기한이 모두 있는 일만 액션아이템
    if 7 in secs:
        for r in parse_table(secs[7])[1]:
            if len(r) < 3:
                continue
            task, owner, due = r[0], r[1], r[2]
            if owner in EMPTY or due in EMPTY:
                errs.append(f"7절 '{task[:30]}'의 담당 또는 기한이 비어 있다('{owner}' / '{due}'). 6절 미합의 사항으로 옮기고 다음 행동을 적는다.")

    # 인물: 참석자·담당자는 녹취·고객 담당자·우리 조직·프로젝트 기억에 있는 이름이어야 한다(지어낸 인물 차단)
    names = []
    if 1 in secs:
        names += [r[1] for r in parse_table(secs[1])[1] if len(r) > 1]
    if 7 in secs:
        names += [n for r in parse_table(secs[7])[1] if len(r) > 1 for n in person_names(r[1])]
    for n in sorted({n for n in names if re.fullmatch(r"[가-힣]{2,4}", n)}):
        if n not in known_text:
            errs.append(f"'{n}'은(는) 녹취·고객 담당자·우리 조직 어디에도 없는 이름이다. 녹취의 표기를 확인해 고친다.")
    return errs


# ---- 상태 추출·갱신 ----------------------------------------------------------

def empty_state(project_key: str) -> dict:
    return {"project": project_key, "meeting_no": 0, "meeting_dates": {}, "attendees": [], "decisions": [],
            "open_issues": [], "action_items": [], "sheet": sheet.empty()}


def update_state(prev: dict, md: str, meeting_no: int, new_sheet: dict | None = None) -> dict:
    """이번 회의록에서 사실을 뽑아 직전 상태에 덧붙인다. 규칙 검사를 통과한 회의록을 전제로 한다.

    결과지는 회의록이 아니라 update_result_sheet 도구가 채운다. new_sheet가 오면 그것으로 바꾼다.
    """
    st = json.loads(json.dumps(prev))  # 깊은 복사
    st["meeting_no"] = meeting_no
    if new_sheet is not None:
        st["sheet"] = new_sheet
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
            a["meetings"] = sorted(set(a.get("meetings", [])) | {meeting_no})
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
    return st


def kpi(st: dict) -> dict:
    past = [a for a in st["action_items"] if a["meeting"] < st["meeting_no"]]
    done = sum(1 for a in past if a["status"] == "완료")
    return {"결과지": sheet.gate_line(st["sheet"]),
            "이행률": f"{done}/{len(past)} ({round(done / len(past) * 100)}%)" if past else "해당 없음(첫 회의)"}


# ---- 에이전트에게 보여줄 요약 ------------------------------------------------

def render_state(st: dict, deal: dict | None = None) -> str:
    """상태 파일을 짧은 마크다운으로. 지난 회의록 전문 대신 이것을 컨텍스트에 넣는다. deal은 프로젝트의 거래 조건."""
    if st["meeting_no"] == 0:
        return "(없음 — 이번이 첫 회의)\n\n" + sheet.render(st["sheet"], deal)
    lines = [f"누적 회차: 1~{st['meeting_no']}차 · 지난 액션 이행률 {kpi(st)['이행률']}", "",
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
    return "\n".join(lines) + "\n\n" + sheet.render(st["sheet"], deal)


# ---- 스냅숏 입출력 ----------------------------------------------------------

def state_path(project_dir: Path, meeting_no: int) -> Path:
    return project_dir / "state" / f"state_{meeting_no:02d}.json"


def draft_path(project_dir: Path, meeting_no: int) -> Path:
    """에이전트가 만든, 아직 담당자가 회의록을 확정하지 않은 상태."""
    return project_dir / "state" / f"draft_{meeting_no:02d}.json"


def pending_path(project_dir: Path, meeting_no: int) -> Path:
    """회의록을 저장하기 전, 이번 회차에 갱신한 결과지를 잠시 두는 곳."""
    return project_dir / "state" / f"pending_{meeting_no:02d}.json"


def save_snapshot(project_dir: Path, st: dict) -> Path:
    p = state_path(project_dir, st["meeting_no"])
    _write_json(p, st)
    return p


def load_before(project_key: str, project_dir: Path, meeting_no: int) -> dict:
    """meeting_no 직전까지 확정된 상태. 회의록이 확정된 회차만 스냅숏이 있다."""
    for n in range(meeting_no - 1, 0, -1):
        snap = _read_json(state_path(project_dir, n))
        if snap:
            snap.setdefault("sheet", sheet.empty())
            return snap
    return empty_state(project_key)


def latest_no(project_dir: Path) -> int:
    """회의록이 확정된 가장 최근 회차."""
    return max([int(p.stem.split("_")[1]) for p in (project_dir / "state").glob("state_*.json")] or [0])


def latest(project_key: str, project_dir: Path) -> dict:
    """확정된 가장 최근 회차까지의 상태(화면의 결과지 보기 · 이력 검색)."""
    return load_before(project_key, project_dir, latest_no(project_dir) + 1)
