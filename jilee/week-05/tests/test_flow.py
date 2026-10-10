"""노드 그래프 데이터(일의 흐름과 관계)가 세 문의 상태를 제대로 옮기는지 LLM 없이 검증한다."""

import shutil
import unittest

from agent import flow, loop as agent_mod, store, tools
from tests.helpers import CID, PID, SHEET_UPDATE, FakeLLM, ids, make_project, minutes


class FlowGraphTest(unittest.TestCase):
    def setUp(self):
        self.tmp, self.patches = make_project()
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def graph(self):
        g = flow.graph(CID, PID)
        self.nodes = {n["id"]: n for n in g["nodes"]}
        self.edges = g["edges"]
        # 어느 단계에서든 그림은 닫혀 있어야 한다: id가 겹치지 않고, 모든 선의 양 끝이 노드에 있다
        self.assertEqual(len(self.nodes), len(g["nodes"]))
        for e in self.edges:
            self.assertIn(e["src"], self.nodes)
            self.assertIn(e["dst"], self.nodes)
            self.assertIn(e["kind"], ("flow", "link"))
        self.assertEqual(g["summary"]["nodes"], len(g["nodes"]))
        self.assertEqual(sum(g["summary"]["by_type"].values()), len(g["nodes"]))
        return g

    def state(self, nid):
        return self.nodes[nid]["state"]

    def edge(self, src, dst):
        return next(e for e in self.edges if (e["src"], e["dst"]) == (src, dst))

    def run_agent(self):
        agent_mod.run_agent(CID, PID, 1, llm=FakeLLM([
            ("update_result_sheet", ids(meeting_no=1, **SHEET_UPDATE)),
            ("save_minutes", ids(meeting_no=1, markdown=minutes()))]))

    def test_before_the_agent_runs(self):
        self.graph()
        self.assertEqual((self.state("meeting:1"), self.state("minutes:1"), self.state("sheet")), ("done", "now", "todo"))
        self.assertIn("녹취 확보", self.nodes["minutes:1"]["sub"])
        self.assertEqual(self.edge("meeting:1", "minutes:1")["state"], "done")
        self.assertEqual(self.edge("minutes:1", "sheet")["state"], "todo")
        self.assertEqual(self.state("item:S1"), "missing")
        self.assertEqual(self.nodes["meeting:1"]["ref"], {"tab": "transcript", "no": 1})
        self.assertEqual(self.nodes["item:S1"]["ref"], {"tab": "sheet", "no": None})
        # 고객 → 프로젝트 → 회의 → 회의록 → 결과지 → 항목이 flow 선으로 이어진다
        flow_pairs = {(e["src"], e["dst"]) for e in self.edges if e["kind"] == "flow"}
        for pair in ((f"customer:{CID}", f"project:{PID}"), (f"project:{PID}", "meeting:1"), ("meeting:1", "minutes:1"),
                     ("minutes:1", "sheet"), ("sheet", "item:B4")):
            self.assertIn(pair, flow_pairs)
        self.assertEqual(len([n for n in self.nodes if n.startswith("item:")]), 11)  # 확정 항목 10 + 경쟁 상황
        # 우리 쪽 담당은 조직 정보에서 오고, 녹취 참석 줄로 회의에 이어진다
        self.assertIn("당사", self.nodes["person:김도현"]["sub"])
        self.assertEqual(self.edge("person:김도현", "meeting:1")["rel"], "참석")
        self.assertEqual(self.edge("person:김도현", f"project:{PID}")["rel"], "주 담당")

    def test_scheduled_meeting_stands_at_the_first_gate(self):
        n = store.register_meeting(CID, PID, "2026-10-30", "14:00", "화상")
        self.graph()
        self.assertEqual((self.state(f"meeting:{n}"), self.state(f"minutes:{n}")), ("now", "todo"))
        self.assertEqual(self.edge(f"meeting:{n}", f"minutes:{n}")["state"], "now")

    def test_minutes_under_review_are_not_reflected_yet(self):
        self.run_agent()
        self.graph()
        self.assertEqual((self.state("meeting:1"), self.state("minutes:1"), self.state("sheet")), ("done", "now", "todo"))
        self.assertIn("회의록 검토", self.nodes["minutes:1"]["sub"])
        self.assertEqual(self.state("item:S1"), "missing")           # 확정 전에는 결과지에 들어가지 않는다
        self.assertNotIn("person:한지은", self.nodes)

    def test_after_minutes_are_confirmed(self):
        self.run_agent()
        tools.confirm_minutes(CID, PID, 1, "이정인")
        self.graph()
        self.assertEqual((self.state("minutes:1"), self.state("sheet")), ("done", "now"))
        self.assertEqual(self.edge("minutes:1", "sheet")["state"], "now")   # 세 번째 문 앞
        self.assertEqual((self.state("item:S1"), self.state("item:D1"), self.state("item:B1")), ("draft", "draft", "missing"))
        self.assertEqual(self.edge("item:S1", "meeting:1")["rel"], "근거")  # 근거가 나온 회차로 이어진다
        self.assertEqual(self.edge("item:D2", "meeting:1")["rel"], "근거")  # 사람에서 읽는 항목도
        self.assertEqual(self.edge("person:한지은", f"project:{PID}")["rel"], "구매")
        self.assertEqual(self.edge("person:한지은", "meeting:1")["rel"], "참석")
        self.assertIn("고객", self.nodes["person:최민호"]["sub"])
        self.assertEqual(self.edge("requirement:R1", "sheet")["rel"], "요구")
        self.assertEqual(self.state("action:A01-1"), "open")
        self.assertEqual(self.edge("action:A01-1", "person:김도현")["rel"], "담당")
        self.assertEqual(self.edge("action:A01-1", "meeting:1")["rel"], "발생")
        self.assertEqual(self.nodes["action:A01-1"]["ref"], {"tab": "minutes", "no": 1})

    def test_after_the_sheet_is_confirmed(self):
        self.run_agent()
        tools.confirm_minutes(CID, PID, 1, "이정인")
        tools.confirm_sheet(CID, PID, "all", "", "이정인")
        self.graph()
        self.assertEqual((self.state("minutes:1"), self.state("sheet")), ("done", "done"))
        self.assertEqual(self.edge("minutes:1", "sheet")["state"], "done")
        self.assertEqual((self.state("item:S1"), self.state("item:S3"), self.state("item:D1")), ("confirmed", "confirmed", "confirmed"))
        self.assertEqual(self.state("item:B1"), "missing")

    def test_deal_terms_add_item_nodes(self):
        store.set_deal(CID, PID, "사업 유형", "정부지원사업(보조금 + 자부담)")
        self.graph()
        self.assertIn("item:C1", self.nodes)      # 정부지원이면 재원·자부담 조달을 확인해야 한다
        self.assertNotIn("item:C3", self.nodes)


if __name__ == "__main__":
    unittest.main()
