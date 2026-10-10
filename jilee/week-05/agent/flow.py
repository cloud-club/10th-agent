"""화면의 노드 그래프에 넘길 데이터: 일의 흐름(고객 → 프로젝트 → 회의 → 회의록 → 결과지 → 항목)과 그 둘레의 관계.

graph.py가 '다음 회의에 들고 갈 기억'을 그래프로 옮긴 것이라면(에이전트의 이력 검색이 쓴다), 이쪽은 사람이 보는 그림이다.
  - 일의 흐름(flow) : 회의마다 세 문(① 회의 → ② 회의록 → ③ 결과지 반영) 가운데 어디에 서 있는지가 노드와 선의 상태로 드러난다.
  - 관계(link)      : 누가 어느 회의에 나왔고, 어떤 역할이며, 무슨 일을 맡았고, 결과지 항목의 근거가 어느 회의에서 나왔는지.

읽기만 한다. 확정된 기억(memory.latest)과 회의 기록(tools.meeting_view)에서 코드가 만든다. LLM을 부르지 않는다.
"""

from __future__ import annotations

from . import memory, sheet, store, tools

ITEM_STATE = {sheet.CONFIRMED: "confirmed", sheet.DRAFT: "draft", sheet.MISSING: "missing"}
RECENT_DONE = 3  # 완료된 액션아이템은 가장 최근 것 몇 개만 보여 준다(끝난 일로 그림이 덮이지 않게)


def _step(passed: int, index: int) -> str:
    """세 문 가운데 index번째(0 회의 · 1 회의록 · 2 결과지 반영)의 상태. 지나왔으면 done, 지금 서 있으면 now."""
    return "done" if index < passed else "now" if index == passed else "todo"


def _cut(text: str, n: int = 40) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[:n] + "…"


def _item_sub(sh: dict, code: str) -> str:
    """결과지 항목의 지금 값 한 줄. 글 항목은 값, 나머지는 그 항목을 채우는 목표·요구사항·사람."""
    if code in sheet.TEXT_ITEMS:
        return _cut(sh["items"].get(code, {}).get("value", ""))
    if code == "S3":
        return _cut(" / ".join(g["goal"] for g in sh["goals"]))
    if code == "S4":
        return _cut(" / ".join(r["title"] for r in sh["requirements"]))
    return _cut(", ".join(s["name"] for s in sh["stakeholders"] if sheet.ROLE_OF[code] & set(s["roles"])))


def _item_meetings(sh: dict, code: str) -> set[int]:
    """그 항목의 근거가 나온 회차들."""
    if code in sheet.TEXT_ITEMS:
        rows = [sh["items"].get(code, {})]
    elif code == "S3":
        rows = sh["goals"]
    elif code == "S4":
        rows = sh["requirements"]
    else:
        rows = [s for s in sh["stakeholders"] if sheet.ROLE_OF[code] & set(s["roles"])]
    return {e["meeting"] for r in rows for e in r.get("evidence", [])}


def graph(customer_id: str, project_id: str) -> dict:
    """한 프로젝트의 그래프. {"nodes": [...], "edges": [...], "summary": {...}}

    노드 {"id","type","label","sub","state","ref"} · 선 {"src","dst","rel","kind","state"}
    ref는 눌렀을 때 갈 곳({"tab": transcript|minutes|sheet, "no": 회차|None})이다.
    """
    d = store.project_dir(customer_id, project_id)
    cust, proj = store.load_customer(customer_id), store.load_project(customer_id, project_id)
    st = memory.latest(store.key(customer_id, project_id), d)
    sh = st["sheet"]
    nodes: dict[str, dict] = {}
    edges: list[dict] = []

    def node(nid: str, typ: str, label: str, sub: str = "", state: str = "", tab: str = "sheet", no: int | None = None) -> str:
        nodes.setdefault(nid, {"id": nid, "type": typ, "label": label, "sub": sub, "state": state, "ref": {"tab": tab, "no": no}})
        return nid

    def edge(src: str, dst: str, rel: str, kind: str = "link", state: str = "") -> None:
        if src in nodes and dst in nodes:  # 그림에 없는 것을 가리키는 선은 긋지 않는다
            edges.append({"src": src, "dst": dst, "rel": rel, "kind": kind, "state": state})

    # ---- 일의 흐름: 고객 → 프로젝트 → 회의 → 회의록 → 결과지 → 항목 ----
    c = node(f"customer:{customer_id}", "customer", cust["name"], " · ".join(x for x in (cust.get("업종"), cust.get("소재지")) if x))
    p = node(f"project:{project_id}", "project", proj["name"], proj.get("stage", ""))
    edge(c, p, "프로젝트", "flow", "done")

    meetings = [tools.meeting_view(customer_id, project_id, n) for n in store.meeting_nos(customer_id, project_id)]
    passed = [m["gate"]["passed"] for m in meetings]
    # 결과지: 확인을 기다리는 회의가 있으면 지금 볼 곳, 반영된 회의가 모두 확인까지 끝났으면 끝난 것
    sheet_state = "now" if 2 in passed else "done" if 3 in passed else "todo"
    s = node("sheet", "sheet", "리드 결과지", sheet.gate_line(sh), sheet_state)
    for m in meetings:
        n, g = m["no"], m["gate"]
        when = " · ".join(x for x in (m["date"], m["type"]) if x) or "일시 미등록"
        mt = node(f"meeting:{n}", "meeting", f"{n}차 회의", when, _step(g["passed"], 0), "transcript", n)
        mn = node(f"minutes:{n}", "minutes", f"{n}차 회의록", g["state"] + (f" · {g['todo']}" if g["todo"] else ""), _step(g["passed"], 1), "minutes", n)
        edge(p, mt, "회의", "flow", "done")
        edge(mt, mn, "회의록", "flow", _step(g["passed"], 0))   # 녹취가 있어야 회의록으로 넘어간다
        edge(mn, s, "반영", "flow", _step(g["passed"], 2) if g["passed"] >= 2 else "todo")  # 확정 전에는 반영되지 않는다

    by_code = sheet.gate(sh)["by_code"]
    for code in sheet.GATE + ["X1"] + sheet.required_extras(proj.get("deal")):
        it = node(f"item:{code}", "item", f"{code} {sheet.ITEMS[code][1]}", _item_sub(sh, code) or sheet.ITEMS[code][2], ITEM_STATE[by_code[code]])
        edge(s, it, sheet.ITEMS[code][0], "flow", "done" if by_code[code] != sheet.MISSING else "todo")
        for n in sorted(_item_meetings(sh, code)):
            edge(it, f"meeting:{n}", "근거")

    # ---- 관계: 사람 · 요구사항 · 액션아이템 ----
    contacts = {x["name"]: x for x in cust["contacts"]}
    for who in sh["stakeholders"]:
        info = contacts.get(who["name"], {})
        about = " ".join(x for x in (who.get("dept") or info.get("dept"), who.get("title") or info.get("title")) if x)
        pn = node(f"person:{who['name']}", "person", who["name"], " · ".join(x for x in ("고객", about, "/".join(who["roles"])) if x))
        for role in who["roles"]:
            edge(pn, p, role)
    for t in store.team(customer_id, project_id):
        if t.get("name"):
            pn = node(f"person:{t['name']}", "person", t["name"], " · ".join(x for x in ("당사", t.get("org"), t.get("role")) if x))
            edge(pn, p, t.get("role") or "담당")
    for m in meetings:
        for name in m["attendees"]:
            edge(f"person:{name}", f"meeting:{m['no']}", "참석")

    for r in sh["requirements"]:
        rn = node(f"requirement:{r['id']}", "requirement", r["title"], f"{r.get('response_status', '확인 필요')} · {_cut(r.get('response') or '당사 대응 미정', 30)}")
        edge(rn, s, "요구")

    done = sorted((a for a in st["action_items"] if a["status"] == "완료"), key=lambda a: a["meeting"])[-RECENT_DONE:]
    for a in [a for a in st["action_items"] if a["status"] != "완료"] + done:
        an = node(f"action:{a['id']}", "action", a["task"], f"{a['owner']} · {a['due']}",
                  "done" if a["status"] == "완료" else "open", "minutes", a["meeting"])
        edge(an, f"meeting:{a['meeting']}", "발생")
        for name in memory.person_names(a["owner"]):
            edge(an, f"person:{name}", "담당")

    count = lambda rows, key: {k: sum(1 for r in rows if r[key] == k) for k in sorted({r[key] for r in rows})}
    return {"nodes": list(nodes.values()), "edges": edges,
            "summary": {"nodes": len(nodes), "edges": len(edges), "by_type": count(nodes.values(), "type"), "by_kind": count(edges, "kind")}}
