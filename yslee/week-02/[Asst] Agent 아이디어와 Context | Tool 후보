### 1. 만들고 싶은 Agent 한 줄

AWS 비용 최적화 리뷰 에이전트

계정 비용 데이터랑 AWS가 내주는 최적화 권고를 읽어서, 우리 워크로드 상황 반영해서 "이번에 실제로 할 수 있는" 절감 액션 리스트를 뽑아주는 에이전트

- 비용 리뷰할 때 Cost Explorer랑 Compute Optimizer 열어놓고 하나씩 보면서 정리하는 게 매번 반복이라 여기부터 자동화해보고 싶음
- 일단 목표는 리포트까지. 실제 리소스 변경(인스턴스 줄이기, 유휴 리소스 삭제 등)은 사람이 보고 결정하고 에이전트는 판단하고 제안만 함

### 2. Context 후보 2개

- 비용 / 최적화 권고 데이터
  - Cost Explorer 비용 추이, 전월 대비 뭐가 늘었는지, 비용 이상 탐지 결과
  - Compute Optimizer / Cost Optimization Hub의 라이트사이징, 유휴 리소스 권고, Savings Plans/RI 커버리지
  - 이게 없으면 어디서 돈이 새는지 판단 자체가 안 됨. 그리고 비용 계산은 LLM이 하는 게 아니라 AWS가 계산해둔 숫자를 그대로 읽게 하고 싶음. 숫자 추정하다 틀리면 리포트 신뢰도가 바로 깨져서

- 워크로드 컨텍스트 + 과거 결정 기록
  - 리소스 태그(환경, 서비스, 담당자), 예외 목록(월말 배치 때문에 의도적으로 큰 인스턴스, 곧 폐기 예정인 것들)
  - 이전 리뷰에서 권고를 채택했는지 기각했는지, 기각했으면 왜 기각했는지
  - 권고를 그대로 내보내면 오탐이 너무 많음. "이건 이미 알고 있는 거", "이건 건들면 안 되는 거" 걸러내는 데 필요. 예시로 주신 장애 일지랑 비슷한 역할이라고 생각함. 처음엔 마크다운 파일 하나로 관리하고 프롬프트에 그냥 넣을 예정

### 3. Tool 후보 2개

- 비용 데이터 수집 Tool
  - awslabs의 Billing and Cost Management MCP Server 쓸 생각. Cost Explorer, Budgets, 이상 탐지, Compute Optimizer, Cost Optimization Hub, Savings Plans까지 한 서버에 들어있어서 이거 하나면 Context 1은 거의 커버됨
  - https://awslabs.github.io/mcp/servers/billing-cost-management-mcp-server
  - 예전 Cost Explorer MCP Server는 deprecated 되고 이 서버로 합쳐짐 (검색하면 옛날 게 먼저 나옴)
  - Cost Explorer API가 요청당 $0.01 과금이라 호출 횟수는 좀 신경써야 할 듯

- 리소스 정보 조회 + 문서 검색 Tool
  - AWS MCP Server(AWS 관리형 원격 서버, 올해 5월 GA) 붙여볼 예정. call_aws로 태그, 리소스 구성 조회하고 search_documentation으로 권고마다 근거 문서 붙이는 용도
  - 권한은 read-only 롤로 제한. MCP 경유 호출에 aws:ViaAWSMCPService 같은 조건키가 자동으로 붙어서 IAM에서 에이전트 경로만 따로 막을 수 있다고 해서 이걸 활용해볼 예정
  - 여러 API 결과 합쳐야 할 때는 run_script가 있어서 쓸 수 있을 것 같은데 아직 안 써봐서 실제로 편한지는 확인 필요

---

메모

- 구조는 최대한 단순하게. 에이전트 하나, Tool 두 개, "수집 -> 예외 필터 -> 우선순위 -> 리포트" 흐름은 고정(사실 워크플로우에 더 가까움)
- 잘 됐는지는 내가 직접 리뷰한 결과랑 비교해보고, 권고 채택률로 볼 예정
- 구현은 Strands Agents로 해볼 예정(MCP 클라이언트 붙이기 용이하기 때문)
