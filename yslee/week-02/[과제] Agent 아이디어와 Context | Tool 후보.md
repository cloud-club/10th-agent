### 1. 만들고 싶은 Agent 한 줄

AWS 비용 최적화 리뷰 에이전트

계정 비용 데이터와 AWS가 생성한 최적화 권고를 읽고, 우리 워크로드 상황을 반영해 이번 리뷰에서 실제로 실행 가능한 절감 액션 목록을 산출하는 에이전트

- 비용 리뷰 시 Cost Explorer와 Compute Optimizer를 각각 열어 항목별로 확인하고 정리하는 작업이 매 회 반복되므로 이 구간을 우선 자동화 대상으로 선정
- 범위는 리포트 생성까지로 한정하고, 실제 리소스 변경(인스턴스 축소, 유휴 리소스 삭제 등)은 사람이 검토해 결정하며 에이전트는 판단과 제안만 수행

### 2. Context 후보 2개

- 비용 / 최적화 권고 데이터
  - Cost Explorer 비용 추이, 전월 대비 증가 항목, 비용 이상 탐지 결과
  - Compute Optimizer 및 Cost Optimization Hub의 라이트사이징 권고, 유휴 리소스 권고, Savings Plans / RI 커버리지
  - 이 데이터가 없으면 비용이 증가한 지점을 특정할 수 없어 판단 자체가 성립하지 않음
  - 비용 계산은 LLM이 수행하지 않고 AWS가 산출한 수치를 그대로 인용하도록 설계하며, 추정값을 생성하다 오차가 발생하면 리포트 신뢰도가 즉시 훼손되기 때문
  - 사전 조건으로 Compute Optimizer와 Cost Optimization Hub 옵트인 필요
    (Cost Optimization Hub가 Compute Optimizer 권고를 가져오려면 Compute Optimizer 옵트인이 선행)

- 워크로드 컨텍스트 + 과거 결정 기록
  - 리소스 태그(환경, 서비스, 담당자), 예외 목록(월말 배치용으로 의도적으로 크게 잡은 인스턴스, 폐기 예정 리소스 등)
  - 직전 리뷰에서 권고를 채택했는지 기각했는지, 기각한 경우 그 사유
  - 권고를 그대로 전달하면 오탐 비율이 높아, 이미 인지하고 있는 항목과 변경 금지 항목을 걸러내는 데 필요
  - 예시로 제시된 장애 일지와 동일한 역할로 보고, 초기에는 마크다운 파일 한 개로 관리하며 프롬프트에 직접 주입

### 3. Tool 후보 2개

- 비용 데이터 수집 Tool
  - awslabs의 Billing and Cost Management MCP Server 사용 예정이며, Cost Explorer, Budgets, 비용 이상 탐지, Compute Optimizer, Cost Optimization Hub, Savings Plans가 한 서버에 포함되어 Context 1의 대부분을 커버
  - 기존 Cost Explorer MCP Server는 deprecated 처리되어 이 서버로 통합 (검색 시 구버전이 상위에 노출되므로 주의 필요)
  - 로컬에서 호출자의 AWS 자격 증명으로 동작하는 오픈소스 서버이므로 전용 읽기 전용 IAM 롤을 별도로 부여
  - Cost Explorer API는 페이지네이션 요청 1건당 $0.01 과금이므로 호출 횟수 관리 필요 (커스텀 빌링 뷰는 소스 수만큼 배수 과금)

- 리소스 정보 조회 + 문서 검색 Tool
  - AWS MCP Server(AWS 관리형 원격 서버, 2026년 5월 GA) 연동 예정이며, run_script로 태그와 리소스 구성을 조회하고 search_documentation으로 권고별 근거 문서를 연결하는 용도
  - call_aws는 2026년 7월 15일 deprecated, 8월 31일 제거 예정으로 공지되어 run_script를 기본 경로로 사용하며, 동일한 AWS API 접근을 제공하므로 기능 손실 없음
  - 권한은 read-only 롤로 제한하고, MCP 경유 호출에 `aws:ViaAWSMCPService`와 `aws:CalledViaAWSMCP` 조건 키가 자동 부착되므로 IAM 및 SCP에서 에이전트 경로만 선별 차단
  - run_script는 여러 API 결과를 취합하는 용도로도 사용 가능하나 아직 미사용 상태이므로 실제 사용성은 확인 필요

---

메모

- 구조는 단순하게 유지하며, 에이전트 1개, Tool 2개, "수집 → 예외 필터 → 우선순위 → 리포트" 흐름을 고정 (실질적으로는 워크플로에 가까움)
- 평가는 직접 리뷰한 결과와 비교하고, 권고 채택률을 지표로 사용
- 구현은 Strands Agents 사용 예정 (MCP 클라이언트 연결이 간단하고, tool_filters로 노출 툴을 선별할 수 있음)
- 두 MCP 서버의 툴을 전부 노출하면 선택이 모호해지므로 리뷰 흐름에 사용하는 툴만 노출
