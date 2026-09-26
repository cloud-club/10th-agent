"""딜 기억을 그래프(점·선 목록)로 바꾼다. 관계 질문("이 사람이 맡은 일", "이 회의에서 정한 것")을 선을 따라 답한다.

점: 딜 · 고객사 · 인물 · 회의 · 합의 · 액션아이템 · 표준 질문(Q코드)
선: 참석(인물→회의) · 담당(액션→인물) · 발생(액션→회의) · 점검(액션→회의) · 결정(합의→회의) · 확보(Q→회의) · 소속(인물→고객사/당사)

AI가 녹취에서 관계를 새로 뽑는 방식(GraphRAG)이 아니라, 규칙 검사를 통과한 회의록에서 코드가 만든 딜 기억을 그대로 옮긴다.
그래서 비용이 들지 않고, 틀린 관계가 끼어들 여지가 적다. 화면(관계 그래프)과 다른 에이전트가 이 파일을 그대로 읽을 수 있다.
"""

from __future__ import annotations

from .memory import Q_CODES, person_names


def build_graph(st: dict, company: str = "") -> dict:
    nodes: dict[str, dict] = {}
    edges: list[dict] = []

    def node(nid: str, typ: str, label: str, **kw) -> str:
        nodes.setdefault(nid, {"id": nid, "type": typ, "label": label, **kw})
        return nid

    def edge(src: str, dst: str, rel: str, **kw) -> None:
        edges.append({"src": src, "dst": dst, "rel": rel, **kw})

    deal = node(f"deal:{st['deal_id']}", "딜", st["deal_id"])
    cust = node("org:customer", "고객사", company or "고객사")
    ours = node("org:ours", "당사", "당사")
    edge(deal, cust, "고객")
    for n in range(1, st["meeting_no"] + 1):
        m = node(f"meeting:{n}", "회의", f"{n}차", date=st["meeting_dates"].get(str(n), ""))
        edge(m, deal, "소속")
    for a in st["attendees"]:
        p = node(f"person:{a['name']}", "인물", a["name"], title=a.get("title", ""), note=a.get("note", ""))
        edge(p, cust if a.get("side") == "고객" else ours, "소속")
        for n in a.get("meetings", [a.get("first_meeting", 1)]):
            edge(p, f"meeting:{n}", "참석")
    for i, d in enumerate(st["decisions"], start=1):
        dn = node(f"decision:{d['meeting']}-{i}", "합의", d["text"][:60], text=d["text"])
        edge(dn, f"meeting:{d['meeting']}", "결정")
    for a in st["action_items"]:
        an = node(f"action:{a['id']}", "액션아이템", a["task"], due=a["due"], status=a["status"])
        edge(an, f"meeting:{a['meeting']}", "발생")
        if a.get("checked_at"):
            edge(an, f"meeting:{a['checked_at']}", "점검", result=a.get("result_note", ""))
        for name in person_names(a["owner"]):
            edge(an, node(f"person:{name}", "인물", name), "담당")
    for q in Q_CODES:
        v = st["questions"].get(q, {"status": "미확보"})
        qn = node(f"q:{q}", "표준질문", q, status=v["status"])
        if v["status"] == "확보" and v.get("since"):
            edge(qn, f"meeting:{v['since']}", "확보", evidence=v.get("evidence", ""))
    return {"deal_id": st["deal_id"], "meeting_no": st["meeting_no"], "nodes": list(nodes.values()), "edges": edges}


def person_view(g: dict, name: str) -> str:
    """한 인물에 연결된 선을 따라가 요약한다. 검색 결과 앞에 붙는 관계 정보."""
    pid = f"person:{name}"
    by_id = {n["id"]: n for n in g["nodes"]}
    if pid not in by_id:
        return ""
    p = by_id[pid]
    attended = [by_id[e["dst"]]["label"] for e in g["edges"] if e["src"] == pid and e["rel"] == "참석"]
    org = next((by_id[e["dst"]]["label"] for e in g["edges"] if e["src"] == pid and e["rel"] == "소속"), "")
    owned = [by_id[e["src"]] for e in g["edges"] if e["dst"] == pid and e["rel"] == "담당"]
    lines = [f"[관계] {name} · {org} {p.get('title', '')} {('· ' + p['note']) if p.get('note') else ''}".rstrip(),
             f"  참석 회의: {', '.join(attended) or '기록 없음'}"]
    lines += [f"  담당: {a['label']} (기한 {a.get('due', '')}, {a.get('status', '')})" for a in owned]
    return "\n".join(lines)
