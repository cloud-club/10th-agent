"""첨부파일: 회의나 프로젝트에 붙는 자료(명함 사진, 견적서, 현장 사진, 고객이 준 사양서).

  customers/{고객}/projects/{프로젝트}/files/NN/        NN차 회의에 붙는 것
  customers/{고객}/projects/{프로젝트}/files/project/   프로젝트에 붙는 것
  각 폴더의 index.json                                  이름 · 크기 · 종류 · 올린 사람 · 올린 시각 · 메모, 지운 기록(removed)

- 첨부파일의 내용은 LLM으로 보내지 않는다. 에이전트는 파일이 있다는 것도 모른다(이번 범위 밖). 사람이 올리고 내려받는 보관함이다.
- 받는 것은 문서·이미지·음성뿐이다. 실행 파일은 이름을 어떻게 붙였든 받지 않는다.
- 저장 위치를 구글 드라이브 같은 곳으로 옮길 때는 save / path / listing 세 함수만 그쪽 API 호출로 바꾼다
  (remove는 목록만 고치면 되고, content_type과 이름 정리는 그대로 쓴다).
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from . import store

MAX_FILE_BYTES = 20 * 1024 * 1024        # 한 파일
MAX_PROJECT_BYTES = 200 * 1024 * 1024    # 한 프로젝트의 첨부 전체
MAX_NAME = 120

KINDS = {
    "document": ["pdf", "docx", "xlsx", "pptx", "hwp", "hwpx", "txt", "md", "csv"],
    "image": ["png", "jpg", "jpeg", "gif", "webp", "heic"],
    "audio": ["m4a", "mp3", "wav"],
}
ALLOWED = {ext: kind for kind, exts in KINDS.items() for ext in exts}
# 이름 어디에든 이 확장자가 끼어 있으면 받지 않는다('견적서.exe.pdf' 같은 눈속임 차단)
EXECUTABLE = {"exe", "bat", "cmd", "com", "scr", "pif", "msi", "msp", "dll", "sys", "ps1", "psm1", "vbs", "vbe", "js", "jse",
              "wsf", "wsh", "hta", "lnk", "jar", "sh", "bash", "py", "pyw", "app", "apk", "reg", "cpl", "html", "htm", "svg"}
MIME = {
    "pdf": "application/pdf", "txt": "text/plain; charset=utf-8", "md": "text/markdown; charset=utf-8", "csv": "text/csv; charset=utf-8",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "hwp": "application/x-hwp", "hwpx": "application/hwp+zip",
    "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif", "webp": "image/webp", "heic": "image/heic",
    "m4a": "audio/mp4", "mp3": "audio/mpeg", "wav": "audio/wav",
}


# ---- 이름과 위치 ---------------------------------------------------------------

def clean_name(filename: str) -> str:
    """올라온 파일 이름을 저장해도 되는 이름으로. 경로는 떼고 한글은 살린다. 받을 수 없는 이름이면 ValueError."""
    name = re.split(r"[\\/]", str(filename or ""))[-1]          # 브라우저나 공격자가 붙인 경로는 버린다
    name = re.sub(r"[\x00-\x1f\x7f]", "", name)                 # 제어문자
    name = re.sub(r'[<>:"|?*]', "_", name).replace("..", ".")   # 윈도우에서 못 쓰는 글자, 위로 올라가는 표기
    name = name.strip().strip(".").strip()
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem.strip():
        raise ValueError("파일 이름이 비어 있거나 확장자가 없습니다.")
    ext = ext.lower()
    parts = [p.lower() for p in name.split(".")[1:]]
    if any(p in EXECUTABLE for p in parts):
        raise ValueError("실행 파일은 받지 않습니다.")
    if ext not in ALLOWED:
        raise ValueError(f"받을 수 없는 파일 형식입니다(.{ext}). 받는 형식: {', '.join(sorted(ALLOWED))}")
    stem = stem.strip()[: MAX_NAME - len(ext) - 1].strip()
    return f"{stem}.{ext}"


def _bucket(meeting_no) -> str:
    if meeting_no is None:
        return "project"
    n = int(meeting_no)
    if n < 1:
        raise ValueError(f"잘못된 회차: {meeting_no}")
    return f"{n:02d}"


def _root(customer_id: str, project_id: str) -> Path:
    return store.project_dir(customer_id, project_id) / "files"


def _dir(customer_id: str, project_id: str, meeting_no) -> Path:
    return _root(customer_id, project_id) / _bucket(meeting_no)


def _index(d: Path) -> dict:
    p = d / "index.json"
    data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    data.setdefault("files", [])
    data.setdefault("removed", [])
    return data


def _write_index(d: Path, data: dict) -> None:
    d.mkdir(parents=True, exist_ok=True)
    (d / "index.json").write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _used_bytes(customer_id: str, project_id: str) -> int:
    root = _root(customer_id, project_id)
    return sum(f["size"] for d in root.glob("*") if d.is_dir() for f in _index(d)["files"]) if root.is_dir() else 0


# ---- 올리기 · 보기 · 내려받기 · 지우기 ---------------------------------------------

def save(customer_id: str, project_id: str, meeting_no: int | None, filename: str, data: bytes,
         who: str = "담당자", note: str = "") -> dict:
    """첨부파일 하나를 저장한다. meeting_no가 None이면 프로젝트에 붙인다. 같은 이름이 있으면 '이름 (2).확장자'로 저장한다."""
    name = clean_name(filename)
    if not data:
        raise ValueError("빈 파일입니다.")
    if data[:2] == b"MZ" or data[:4] == b"\x7fELF":  # 확장자를 바꿔 올린 실행 파일
        raise ValueError("실행 파일은 받지 않습니다.")
    if len(data) > MAX_FILE_BYTES:
        raise ValueError(f"파일이 너무 큽니다({len(data) / 1024 / 1024:.1f}MB). 한 파일은 {MAX_FILE_BYTES // 1024 // 1024}MB까지입니다.")
    if _used_bytes(customer_id, project_id) + len(data) > MAX_PROJECT_BYTES:
        raise ValueError(f"이 프로젝트의 첨부 용량({MAX_PROJECT_BYTES // 1024 // 1024}MB)을 넘습니다. 쓰지 않는 파일을 지운 뒤 올리세요.")
    d = _dir(customer_id, project_id, meeting_no)
    index = _index(d)
    taken = {f["name"] for f in index["files"]}
    stem, ext = name.rsplit(".", 1)
    n = 2
    while name in taken or (d / name).exists():
        name = f"{stem} ({n}).{ext}"
        n += 1
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_bytes(data)
    entry = {"name": name, "size": len(data), "kind": ALLOWED[ext], "meeting_no": None if meeting_no is None else int(meeting_no),
             "uploaded_by": who, "uploaded_at": _now(), "note": (note or "").strip()[:200]}
    index["files"].append(entry)
    _write_index(d, index)
    return entry


def listing(customer_id: str, project_id: str, meeting_no="all") -> list[dict]:
    """첨부 목록, 최근에 올린 것부터. meeting_no: 회차 · None(프로젝트에 붙은 것) · "all"(전부)."""
    root = _root(customer_id, project_id)
    if meeting_no == "all":
        dirs = sorted(d for d in root.glob("*") if d.is_dir()) if root.is_dir() else []
    else:
        dirs = [_dir(customer_id, project_id, meeting_no)]
    rows = [f for d in dirs for f in _index(d)["files"]]
    rows.reverse()  # 같은 시각에 올린 것은 나중 것이 앞에 오게
    return sorted(rows, key=lambda f: f["uploaded_at"], reverse=True)


def path(customer_id: str, project_id: str, meeting_no, name: str) -> Path:
    """내려받을 파일의 위치. 목록에 있고, 그 첨부 폴더 바로 안에 있는 파일만 돌려준다."""
    d = _dir(customer_id, project_id, meeting_no)
    if name != re.split(r"[\\/]", str(name or ""))[-1] or name in ("", ".", "..", "index.json"):
        raise FileNotFoundError(f"첨부파일이 없습니다: {name}")
    p = d / name
    listed = any(f["name"] == name for f in _index(d)["files"])
    if not listed or not p.is_file() or p.resolve().parent != d.resolve():
        raise FileNotFoundError(f"첨부파일이 없습니다: {name}")
    return p


def remove(customer_id: str, project_id: str, meeting_no, name: str, who: str) -> None:
    """첨부파일을 지운다. 누가 언제 무엇을 지웠는지는 목록 파일에 남는다."""
    p = path(customer_id, project_id, meeting_no, name)
    d = p.parent
    index = _index(d)
    entry = next(f for f in index["files"] if f["name"] == name)
    index["files"] = [f for f in index["files"] if f["name"] != name]
    index["removed"].append({**entry, "removed_by": who, "removed_at": _now()})
    p.unlink()
    _write_index(d, index)


def content_type(name: str) -> str:
    return MIME.get(str(name).rsplit(".", 1)[-1].lower(), "application/octet-stream")
