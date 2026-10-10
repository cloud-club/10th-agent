"""테스트 공용: 임시 폴더에 고객 → 프로젝트 구조를 만들고, 가짜 LLM과 통과하는 회의록을 준다."""

import json
import tempfile
from pathlib import Path
from unittest import mock

from agent import loop as agent_mod
from agent import store

CID, PID = "T1", "P1"

TRANSCRIPT = """[2026-10-01 10:00 / 방문 / 테스트상사 본사]
참석: (고객) 정수연 품질팀장, 한지은 구매팀 대리 / (당사) 김도현
김도현: 오늘은 검사 공정에서 겪고 계신 문제를 듣고 싶습니다.
정수연: 저희는 외관 검사를 사람이 눈으로 합니다 검사원이 열두 명이에요.
정수연: 분기마다 불량 유출이 두세 건 나와서 클레임 비용이 사천만 원 넘게 나왔어요.
정수연: 유출을 분기 한 건 이하로 줄이는 게 목표입니다.
김도현: 그 두 품목부터 검사 자동화 장비로 시작하는 걸 제안드립니다.
한지은: 계약이랑 발주는 구매팀인 제가 맡습니다 제 번호는 010-1234-5678입니다.
정수연: 최종 결재는 최민호 공장장님이 하세요."""


class FakeLLM:
    """정해진 순서대로 tool_calls를 내고, 도구가 없으면 보고 문장을 낸다."""

    def __init__(self, plan):
        self.plan = list(plan)
        self.calls = 0

    def chat(self, messages, tools=None):
        self.calls += 1
        if tools is None or not self.plan:
            return {"role": "assistant", "content": "보고: 저장 완료"}
        name, args = self.plan.pop(0)
        return {"role": "assistant", "content": "", "tool_calls": [
            {"id": f"c{self.calls}", "type": "function",
             "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}]}


def ids(**extra) -> dict:
    return {"customer_id": CID, "project_id": PID, **extra}


def make_project(transcript: str = TRANSCRIPT) -> tuple[Path, list]:
    """(임시 폴더, 시작해야 할 patch 목록). 고객 T1 아래 프로젝트 P1·P2, 우리 조직에 김도현 한 명."""
    tmp = Path(tempfile.mkdtemp())
    c = tmp / "customers" / CID
    (c / "projects" / PID).mkdir(parents=True)
    (c / "projects" / "P2").mkdir()
    (c / "customer.json").write_text(json.dumps({
        "customer_id": CID, "name": "테스트상사(주)", "업종": "제조",
        "contacts": [{"id": "CT001", "name": "정수연", "dept": "품질팀", "title": "팀장"}]}, ensure_ascii=False), encoding="utf-8")
    (c / "projects" / PID / "project.json").write_text(json.dumps({
        "project_id": PID, "name": "검사 자동화", "team": [{"member_id": "E1", "role": "주 담당"}]}, ensure_ascii=False), encoding="utf-8")
    (c / "projects" / "P2" / "project.json").write_text(json.dumps({"project_id": "P2", "name": "MES 연동", "stage": "발굴"}, ensure_ascii=False), encoding="utf-8")
    (c / "projects" / PID / "transcript_01.txt").write_text(transcript, encoding="utf-8")
    (tmp / "org.json").write_text(json.dumps({"members": [
        {"id": "E1", "name": "김도현", "org": "영업1팀", "email": "kim@example.com"}]}, ensure_ascii=False), encoding="utf-8")
    patches = [mock.patch.object(store, "CUSTOMERS_DIR", tmp / "customers"),
               mock.patch.object(store, "ORG_FILE", tmp / "org.json"),
               mock.patch.object(agent_mod, "RUNS_DIR", tmp / "runs")]
    return tmp, patches


def minutes(no=1, action_due="10월 8일", owner="김도현(당사)", review=""):
    return f"""# 회의록 — 테스트상사 {no}차 회의

- 일시: 2026-10-0{no} 10:00

## 1. 참석자
| 구분 | 이름 | 직위/역할 | 비고(결정권 등) |
| --- | --- | --- | --- |
| 고객 | 정수연 | 품질팀장 | 최초 접점 |
| 당사 | 김도현 | 영업 | |

## 2. 지난 회의 액션아이템 점검
| 항목 | 담당 | 기한 | 결과(완료 · 진행 중 · 미착수) |
| --- | --- | --- | --- |
{review}

## 3. 논의 요지
### 검사 방식 — 사람이 눈으로 검사해 유출이 난다
- **[중요]** 분기마다 불량 유출 2~3건
  - 검사원 12명

## 4. 고객 요구·문제점
| 구분 | 내용(고객 표현 그대로) | 중요도 | 시급성 |
| --- | --- | --- | --- |
| 문제점 | 불량 유출 | 높음 | 보통 |

## 5. 합의 사항
- {no}차 합의

## 6. 미합의 사항 · 다음 행동
- 예산 → 다음 회의 확인

## 7. 액션아이템
| 항목 | 담당 | 기한 |
| --- | --- | --- |
| 견적서 제공 | {owner} | {action_due} |
"""


SHEET_UPDATE = {  # 근거가 모두 TRANSCRIPT의 실제 발언과 맞는 갱신
    "summary": "외관 검사를 사람이 하고 있어 불량 유출이 분기마다 난다.",
    "items": [{"code": "S1", "value": "외관 검사를 검사원 12명이 눈으로 한다", "evidence": "외관 검사를 사람이 눈으로 합니다 검사원이 열두 명이에요"},
              {"code": "S2", "value": "분기마다 불량 유출 2~3건, 클레임 비용 4,000만 원 이상", "evidence": "분기마다 불량 유출이 두세 건 나와서 클레임 비용이 사천만 원 넘게"}],
    "goals": [{"goal": "불량 유출 줄이기", "current": "분기 2~3건", "target": "분기 1건 이하", "evidence": "유출을 분기 한 건 이하로 줄이는 게 목표입니다"}],
    "requirements": [{"title": "외관 검사 자동화", "background": "사람마다 기준이 다르다", "response": "두 품목부터 검사 자동화 장비 적용",
                      "response_status": "검토 중", "evidence": "그 두 품목부터 검사 자동화 장비로 시작하는 걸 제안드립니다"}],
    "stakeholders": [{"name": "한지은", "side": "고객", "dept": "구매팀", "title": "대리", "roles": ["구매"], "phone": "010-1234-5678",
                      "evidence": "계약이랑 발주는 구매팀인 제가 맡습니다"},
                     {"name": "최민호", "side": "고객", "title": "공장장", "roles": ["의사결정자"], "evidence": "최종 결재는 최민호 공장장님이 하세요"}],
    "next_questions": ["예산은 확보되어 있습니까"],
}
