"""사람이 회의록을 고친다.

에이전트가 만든 회의록의 틀린 곳을 담당자가 직접 고칠 수 있게 한다(사람이 누르는 확정 단계는 없앴다).
아래는 반영되기 전의 초안을 고칠 때의 설명이다. 고치면
  - 회의록 파일을 덮어쓰고,
  - 초안 상태(draft_NN.json)의 참석자·합의·미합의·액션아이템을 고친 회의록에서 다시 뽑는다.
    결과지 변경 제안(sheet)은 근거 발화와 묶여 있으므로 건드리지 않는다.
  - 에이전트가 쓴 원문은 state/minutes_NN.agent.md로 한 번만 남긴다(사람이 무엇을 고쳤는지 나중에 견줄 수 있게).

형식 규칙은 에이전트와 똑같이 지켜야 저장된다(다음 회의의 기억이 이 형식에서 뽑히기 때문이다).
다만 이름 검사는 경고로만 돌려준다. 녹취에 잘못 들린 이름을 사람이 바로잡는 것이 고치는 까닭 가운데 하나다.
"""

from __future__ import annotations

import difflib
import time
from pathlib import Path

from . import memory, store

NAME_ERROR = "어디에도 없는 이름이다"  # memory.validate의 인물 검사 문구
MAX_DIFF_LINES = 200


def _original_path(project_dir: Path, meeting_no: int) -> Path:
    return project_dir / "state" / f"minutes_{meeting_no:02d}.agent.md"


def save_minutes_edit(customer_id: str, project_id: str, meeting_no: int, markdown: str, who: str = "담당자") -> dict:
    """담당자가 고친 회의록 초안을 저장한다. 형식 위반이 있으면 저장하지 않고 사유를 돌려준다."""
    d, key, n = store.project_dir(customer_id, project_id), store.key(customer_id, project_id), int(meeting_no)
    target = memory.draft_path(d, n)
    draft = memory._read_json(target)
    if draft is None:  # 반영된 회의록도 고칠 수 있다. 그 회차의 상태를 고친 회의록으로 다시 만든다
        target = memory.state_path(d, n)
        draft = memory._read_json(target)
        if draft is None:
            raise FileNotFoundError(f"{n}차에 고칠 회의록이 없습니다. 에이전트를 먼저 실행하세요.")
    final = target == memory.state_path(d, n)

    prev = memory.load_before(key, d, n)
    tp = store.transcript_path(customer_id, project_id, n)
    known = " ".join(store.known_names(customer_id, project_id) + [a["name"] for a in prev["attendees"]])
    known += tp.read_text(encoding="utf-8") if tp.exists() else ""
    errs = memory.validate(markdown, known)
    # 고치기 전부터 있던 양식 위반(양식이 바뀌기 전에 쓴 회의록)은 막지 않고 알리기만 한다
    mp = store.minutes_path(customer_id, project_id, n)
    old = set(memory.validate(mp.read_text(encoding="utf-8"), known)) if mp.exists() else set()
    warnings = [e for e in errs if NAME_ERROR in e or e in old]
    blocking = [e for e in errs if NAME_ERROR not in e and e not in old]
    if blocking:
        return {"ok": False, "errors": blocking, "warnings": warnings}

    p = store.minutes_path(customer_id, project_id, n)
    original = _original_path(d, n)
    if p.exists() and not original.exists():  # 에이전트가 쓴 원문은 처음 고칠 때 한 번만 남긴다
        original.parent.mkdir(parents=True, exist_ok=True)
        original.write_text(p.read_text(encoding="utf-8"), encoding="utf-8")
    p.write_text(markdown.strip() + "\n", encoding="utf-8")
    memory._write_json(target, memory.update_state(prev, markdown, n, draft["sheet"]))

    at = time.strftime("%Y-%m-%d %H:%M")
    m = store.load_meeting(customer_id, project_id, n)
    m["minutes"] = {**m.get("minutes", {}), "status": "확정" if final else "초안", "edited_by": who, "edited_at": at}
    store.save_meeting(customer_id, project_id, n, m)
    return {"ok": True, "warnings": warnings, "edited_by": who, "edited_at": at}


def diff_from_agent(customer_id: str, project_id: str, meeting_no: int) -> dict | None:
    """에이전트가 쓴 원문과 지금 회의록의 차이. 사람이 고친 적이 없으면 None."""
    d, n = store.project_dir(customer_id, project_id), int(meeting_no)
    original, p = _original_path(d, n), store.minutes_path(customer_id, project_id, n)
    if not original.exists() or not p.exists():
        return None
    a, b = original.read_text(encoding="utf-8").splitlines(), p.read_text(encoding="utf-8").splitlines()
    lines = list(difflib.unified_diff(a, b, "에이전트가 쓴 원문", "지금 회의록", lineterm="", n=1))
    body = [l for l in lines if not l.startswith(("+++", "---", "@@"))]
    return {"added": sum(1 for l in body if l.startswith("+")), "removed": sum(1 for l in body if l.startswith("-")),
            "lines": lines[:MAX_DIFF_LINES]}
