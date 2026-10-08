# AWS Cost Review Agent

매월 반복되는 AWS 비용 리뷰 작업을 줄이는 읽기 전용 Python 에이전트다. 두 달을 지정하면 AWS Billing and Cost Management MCP Server를 통해 월간 비용 비교, 주요 cost driver, Cost Optimization Hub 권고를 가져오고, 팀의 업무 예외를 YAML 규칙으로 걸러낸 뒤, LLM이 설명을 붙인 Markdown 리포트를 만든다. 결과는 사람이 검토할 제안이며 에이전트는 AWS를 변경하지 않는다.

## 1. 문제와 목표

비용 리뷰어는 매월 Cost Explorer에서 전월 대비 변화를 확인하고, Cost Optimization Hub 권고를 열어 보고, "월말 배치용 서버는 유지" 같은 운영 예외를 손으로 다시 걸러낸다. 이 MVP는 그 반복을 한 번의 실행으로 줄인다.

지키는 경계:

- 읽기 전용 분석이다. 권고의 적용 여부는 사람이 결정한다.
- 업무 예외의 정확 일치 판정과 모든 수치 표는 Python이 담당한다.
- LLM은 근거를 설명만 하고 비용·절감액·원인을 지어내지 않는다.
- RAG, 메모리, 다중 에이전트는 필요가 증명되기 전까지 넣지 않는다.

## 2. 동작 흐름

```text
python app.py --previous 2026-08 --current 2026-09
   │
   ├─ 1) context/exceptions.yaml 로드 (Pydantic 검증, 미지원 규칙은 거부)
   ├─ 2) Billing MCP 서버 기동 (uvx) → tool 이름 발견·검증 → cost-comparison, cost-optimization만 노출
   ├─ 3) Strands Agent 실행 (Bedrock 모델)
   │       cost-comparison  getCostAndUsageComparisons ┐ BeforeToolCallEvent: 기간·metric·횟수 검사
   │       cost-comparison  getCostComparisonDrivers   ┘ AfterToolCallEvent: 응답 형식 검증 후 snapshot 보관
   │       cost-optimization list_recommendations      → AfterToolCallEvent: Python이 업무 예외 적용 후
   │                                                     필터된 결과만 모델에 전달
   │       ReviewAnalysis (structured output)          → 금액 없는 서술만 허용
   ├─ 4) 완료 검증: 세 조회가 모두 성공했는지 확인 (아니면 exit 2, 리포트 미생성)
   └─ 5) Python Markdown 렌더링 → reports/cost_review_2026-09.md
```

`--mock`은 AWS, MCP, 모델을 전혀 호출하지 않고 `fixtures/`의 합성 데이터를 같은 요청 검증·필터·렌더링 경로로 통과시킨다. `--discover-only`는 MCP 연결과 tool 이름 검증까지만 수행한다.

## 3. 역할 분담

| 작업 | 담당 | 근거 위치 |
|---|---|---|
| 조회 기간을 요청한 두 달의 1일~익월 1일로 고정 | Python hook | `hooks.py` `accept_comparison_request` |
| tool·operation 허용 목록, 호출 횟수 제한 | Python hook | `billing_mcp.py`, `hooks.py` |
| 업무 예외 적용 (resource ID·tag 정확 일치) | Python | `filters.py` |
| 비용 비교 표, 권고 표의 모든 수치 | Python (source 값 복사) | `comparison.py`, `report.py` |
| 비용 변화 설명, 검토 우선순위 서술, 한계 서술 | LLM | `agent.py`, `models.py` `ReviewAnalysis` |
| 권고 적용 여부 결정 | 사람 | 리포트 |

LLM이 보는 데이터도 Python이 먼저 거른다. 권고 목록은 업무 예외를 적용한 `included/excluded` 형태로만 모델에 전달되고, 모델은 이를 재분류하지 말라는 지시를 받는다. 비용 비교 응답은 원본 그대로 모델에 전달되지만 수치 표는 모델 출력이 아니라 snapshot에서 렌더링한다.

## 4. 구성 요소

| 파일 | 역할 |
|---|---|
| `app.py` | CLI 진입점. 인자 검증, mock/discover-only/live 분기, 완료 검증, 리포트 저장 |
| `agent.py` | Strands Agent 생성(Bedrock), system prompt, 리뷰 요청문 |
| `billing_mcp.py` | Billing MCP stdio 연결, tool 이름 발견·검증, 허용 목록 필터 |
| `hooks.py` | BeforeToolCallEvent 가드(기간·metric·횟수), AfterToolCallEvent 처리(검증·필터·snapshot), 완료 판정 |
| `filters.py` | 권고 응답 파싱, 업무 예외 정확 일치, 절감액 내림차순 정렬 |
| `comparison.py` | cost-comparison 응답 형식 검증, 표 행 추출, selector 라벨 |
| `models.py` | 예외 규칙 모델, `ReviewPeriod`(월 경계 계산), `ReviewAnalysis`(금액 금지 검증) |
| `report.py` | Markdown 리포트 렌더링 |
| `check_readiness.py` | live 실행 전 읽기 전용 사전 점검 |
| `context/exceptions.yaml` | 팀 업무 예외 규칙 |
| `fixtures/` | mock용 합성 데이터 (서버 응답 형식) |
| `tests/` | 오프라인 테스트 (AWS 호출 없음) |

의존성은 `strands-agents`, `mcp`, `boto3`, `pydantic`, `pyyaml`이며 Python 3.10 이상이 필요하다. MCP 서버는 `uvx awslabs.billing-cost-management-mcp-server@latest`로 실행한다.

## 5. 업무 예외 Context

`context/exceptions.yaml`은 두 종류의 `exclude` 규칙만 지원한다.

```yaml
resources:
  - resource_id: i-example-batch
    action: exclude
    reason: 월말 배치 처리를 위해 현재 사양 유지
tag_rules:
  - key: OptimizationExempt
    value: "true"
    action: exclude
    reason: 비용 최적화 제외 태그
```

- resource ID 규칙을 먼저 보고, 없으면 tag key/value 규칙을 본다. 모두 대소문자까지 정확 일치다.
- 지원하지 않는 키나 `exclude` 외의 action은 로드 단계에서 거부한다(exit 2). fuzzy 매칭이나 LLM의 예외 판단은 없다.
- 제외된 권고는 리포트의 별도 표에 사유와 함께 남는다.
- 실제 계정의 리소스 ID를 Git에 커밋하지 않는다. 저장소의 ID는 모두 example이다.

## 6. Tool 호출 가드

연결 시 서버가 실제로 제공하는 tool 이름을 확인하고 Strands `MCPClient`의 `tool_filters.allowed`로 `cost-comparison`, `cost-optimization`만 Agent에 노출한다. Agent의 tool 목록이 허용 목록과 다르면 실행을 중단한다.

Hook이 코드로 강제하는 규칙:

- `cost-comparison`은 `getCostAndUsageComparisons`와 `getCostComparisonDrivers`를 각각 최대 한 번 호출한다.
- 네 날짜 인자가 `--previous`/`--current`의 1일~익월 1일(YYYY-MM-DD)과 정확히 같아야 한다. 다르면 호출을 취소하고 올바른 값을 취소 메시지로 돌려주어 모델이 재시도하게 한다. 취소된 호출은 호출 예산을 소비하지 않는다.
- `metric_for_comparison`은 문자열로 지정해야 하며, 기록해서 리포트의 실행 정보에 표시한다. 요청문은 `UnblendedCost`와 SERVICE 기준 group_by를 제안하지만 값 자체를 강제하지는 않는다.
- `cost-optimization`은 `list_recommendations`만 최대 한 번 호출한다. 그 외 operation은 취소한다.
- 응답이 서버 형식(`{"status": "success", "data": {...}}`)이 아니거나 오류 상태면 기록 후 실패로 처리한다. 잘못된 데이터를 모델이나 리포트에 넘기지 않는다.
- LIVE 리포트는 두 비교 작업과 `list_recommendations`가 모두 성공해야 생성된다. 하나라도 없으면 누락 항목을 출력하고 exit 2로 끝나며 파일을 만들지 않는다.

MCP 호출 한 번이 AWS 요청 한 번은 아니다. 서버가 내부에서 페이지를 반복 조회하므로 Cost Explorer 요청은 여러 건이 될 수 있고, 요청마다 과금된다.

## 7. 안전 원칙

- AWS 리소스, IAM, 계정 설정을 변경하지 않으며 Savings Plans나 RI를 구매하지 않는다. 호출하는 API는 `ce:GetCostAndUsageComparisons`, `ce:GetCostComparisonDrivers`, `cost-optimization-hub:ListRecommendations`와 Bedrock 모델 호출뿐이다.
- Cost Explorer나 Cost Optimization Hub를 자동으로 활성화하지 않는다.
- AWS가 반환하지 않은 금액을 계산하거나 생성하지 않는다. `ReviewAnalysis`는 통화 표기가 붙은 숫자(`USD 120`, `$120`, `120달러`)와 소수점·천 단위 구분이 있는 숫자(`140.0`, `1,200`)를 거부하고, `2026-07`, `8월` 같은 월 표기는 허용한다. 검증에 실패하면 모델에 오류를 돌려주고 재시도시킨다.
- 모든 표는 Python이 source snapshot의 값을 그대로 옮겨 만든다. 비율이나 합계를 새로 계산하지 않는다.
- Credential, account ID, token을 코드나 Git에 기록하지 않는다. source snapshot은 실행 중 메모리에만 있고 원본 응답 파일을 남기지 않는다.

## 8. 리포트 구성

`reports/cost_review_<current>.md` (또는 `--output-dir`)에 저장한다. 같은 월은 덮어쓴다.

1. 실행 정보: 비교 월, 현재 월, mode(LIVE/MOCK)
2. 비용 변화 요약: LLM 서술
3. 월간 비용 비교: 비교 기간과 metric, 총 비용 표, 항목별 변화 표(|difference| 내림차순 상위 20건, 생략 건수 표기), 주요 cost driver 표(AWS 반환 순서)
4. 검토할 비용 최적화 후보: 절감액 내림차순, 절감액 없는 항목은 뒤에
5. 업무 예외로 제외된 권고: 사유 포함
6. 분석: LLM의 검토 우선순위 서술
7. 한계 및 주의사항: LLM 서술 + 응답에 없던 필드(N/A) 안내 + 처리 오류
8. 데이터 출처

## 9. 설치

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
python -m pip install uv
```

`uv`는 MCP 서버 실행용이며 서버 패키지를 프로젝트 의존성에 넣지 않는다. mock 실행에는 AWS CLI나 자격증명이 필요 없다.

## 10. 실행

| 옵션 | 설명 |
|---|---|
| `--previous YYYY-MM`, `--current YYYY-MM` | 필수. previous가 current보다 이전 월이어야 한다 |
| `--mock` | fixture로 오프라인 실행 (2026-07/2026-08 전용) |
| `--discover-only` | MCP 연결과 tool 이름 검증만 수행, AWS API·모델 호출 없음 |
| `--profile`, `--region` | AWS profile과 Bedrock region. region 기본값은 `AWS_REGION` 또는 `us-east-1` |
| `--output-dir DIR` | 리포트 디렉터리. 기본값 `reports/` |

환경변수: `MODEL_ID`(미지정 시 Strands 기본 Bedrock 모델), `AWS_PROFILE`, `AWS_REGION`. Cost Explorer와 Cost Optimization Hub는 서버가 항상 `us-east-1`로 호출하므로 `--region`은 Bedrock에만 적용된다.

```sh
# mock
python app.py --previous 2026-07 --current 2026-08 --mock

# MCP 연결만 확인
python app.py --previous 2026-08 --current 2026-09 --discover-only

# live (완료된 최근 두 달 권장)
python app.py --previous 2026-08 --current 2026-09 --profile <AWS_PROFILE> --region us-east-1
```

종료 코드는 성공 0, 설정·검증·조회 실패 2다. live 실행 전제: Cost Explorer 활성화, Cost Optimization Hub opt-in, Bedrock 모델 접근, 위 세 API와 `bedrock:InvokeModel`, `bedrock:InvokeModelWithResponseStream` 권한. 비교 월은 최근 13개월 안이어야 한다.

## 11. 사전 점검

`check_readiness.py`는 live 전에 자격증명, IAM 권한(policy simulation), Cost Optimization Hub 등록, Compute Optimizer 등록, Bedrock 모델 접근을 읽기 전용으로 점검한다. 기본 실행은 무료 API만 호출한다.

```sh
python check_readiness.py --profile <AWS_PROFILE> --region us-east-1
```

`--ce --previous YYYY-MM --current YYYY-MM`은 Cost Explorer 비교 요청 1건(약 USD 0.01)으로 활성화와 기간을 확인하고, `--invoke`는 짧은 Bedrock Converse 요청 1건으로 모델 호출을 확인한다. `--dry-run`은 네트워크 없이 boto3 API 존재만 본다. 줄마다 `OK`/`WARN`/`SKIP`/`FAIL`을 출력하고 FAIL이 있으면 exit 1이다.

## 12. 테스트

```sh
pytest -q
```

모두 오프라인이며 AWS를 호출하지 않고 `tmp_path`에만 기록한다. 검증 범위:

- 업무 예외 정확 일치, 정렬, 원본 불변 (`test_filters.py`)
- hook의 기간·metric·중복 가드, JSON/text 처리, fail-closed, 완료 판정 (`test_hooks.py`)
- cost-comparison 응답 형식 검증과 표 행 추출 (`test_comparison.py`)
- 리포트 구역, 원본 금액, 서술 검증기의 금액 거부·월 허용 (`test_report.py`)
- CLI 검증, mock 무접속 실행, discover-only 연결 종료 (`test_app.py`, `test_billing_mcp.py`)
- scripted fake model로 실제 Strands 이벤트 루프 구동: 잘못된 월 호출 취소 후 재시도 성공, drivers 누락 시 리포트 거부, 완전한 실행의 LIVE 리포트 생성 (`test_agent_loop.py`)
- 사전 점검 스크립트의 helper와 dry-run (`test_readiness.py`)

## 13. 알려진 한계

- 개발 환경에 AWS 자격증명이 없어 실제 Bedrock 모델과 실제 비용 데이터로 end-to-end 실행한 증거는 없다. MCP 연결·tool discovery, mock, scripted 루프 테스트까지만 확인했다.
- 확인한 Billing MCP 서버 버전(0.0.37)은 `list_recommendations` 결과를 snake_case로 정규화하면서 `tags`, `restartNeeded`, `rollbackPossible`, `source`를 전달하지 않는다. 누락 값은 `N/A`로 표시하고, 태그 규칙은 live에서 적용할 수 없어 리포트에 "확인 불가"로 남긴다. 서버 명령이 `@latest`라 버전에 따라 전달 필드가 달라질 수 있다.
- 모델의 호출 횟수나 토큰 상한을 두지 않았다. hook이 tool 호출을 제한하지만 모델이 취소 메시지에 반복 응답하면 모델 호출은 계속될 수 있다.
- 항목별 변화 표는 상위 20건만 표시한다. 값은 바꾸지 않는다.
- Mock fixture는 2026-07/2026-08 비교 전용이며 mock 설명은 LLM 출력이 아닌 고정 문구다.
- 진행 중인 달을 비교 월로 쓸 수 있는지는 확인하지 않았다. 완료된 두 달을 권장한다.

## 14. 향후 과제

- 모델 호출 상한(Strands `Limits`)과 실행 메트릭(토큰, 호출 수, 모델 ID, 서버 버전) 기록
- tool 호출과 취소를 출력하는 `--verbose` 옵션
- 이전 리뷰의 권고 채택·기각 기록 반영
- AWS MCP Server를 통한 상세 리소스 구성 확인
