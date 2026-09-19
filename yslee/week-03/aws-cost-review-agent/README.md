# AWS Cost Review Agent

## 문제

매월 AWS 비용 리뷰에서 비용 데이터와 최적화 권고를 각각 확인하고, 운영상 예외를 수동으로 다시 걸러내는 반복 작업을 줄이기 위한 MVP다. 결과는 사람이 검토할 제안이며 자동 변경 작업이 아니다.

## 해결

Strands Agent가 AWS Billing and Cost Management MCP Server에서 월간 비용 비교와 Cost Optimization Hub 권고를 조회한다. Python이 명시적인 업무 예외를 결정론적으로 적용한 뒤, LLM은 사람이 읽기 쉬운 설명만 생성한다. 수치와 리소스 표는 캡처한 MCP 응답에서 Python이 직접 렌더링한다.

## Architecture

```text
사용자 요청 → Strands Agent → Billing MCP
                           ├─ cost-comparison
                           └─ cost-optimization → AfterToolCallEvent
                                                  → Python exception filtering
                                                  → filtered context
→ LLM structured analysis → Python Markdown renderer → report
```

`--mock`은 AWS/MCP/LLM을 호출하지 않고 fixture에서 동일한 필터·렌더링 경로로 들어간다. Mock 설명은 `ReviewAnalysis`로 검증한 예제용 고정 문구다.

## Context

[`context/exceptions.yaml`](context/exceptions.yaml)은 resource ID 정확 일치와 tag key/value 정확 일치의 `exclude` 규칙만 지원한다. 리소스 규칙을 먼저 확인한다. 다른 규칙은 검증 단계에서 거부하며 fuzzy/semantic matching 또는 LLM의 예외 판단은 없다. Fixture의 ID는 모두 MOCK/example이다.

## Tool Calling

서버 명령은 `uvx awslabs.billing-cost-management-mcp-server@latest`다. 연결 시 실제 server-side tool 이름을 발견·검증하고 Strands `MCPClient`의 `tool_filters.allowed`로 `cost-comparison`, `cost-optimization`만 Agent에 노출한다. 전자는 두 비용 비교 조회 작업을 각각 최대 한 번, 후자는 `list_recommendations`를 최대 한 번 호출하도록 hook에서 제한한다. MCP 연결은 context manager로 닫는다.

## RAG를 사용하지 않은 이유

이번 MVP는 대규모 문서 검색 문제가 아니다. AWS의 구조화된 최신 데이터와 작은 업무 예외 Context를 결합하므로 RAG를 추가하지 않았다.

## Safety

- Read-only 분석이다. AWS 리소스·IAM·계정 설정을 변경하거나 Savings Plans/RI를 구매하지 않는다.
- Cost Explorer 또는 Cost Optimization Hub를 자동으로 활성화하지 않는다.
- AWS가 반환하지 않은 금액은 계산하거나 생성하지 않는다. `ReviewAnalysis`는 독립된 숫자 서술을 거부하고 표는 Python source snapshot에서 만든다.
- 권고의 실제 적용 여부는 사람이 결정한다. Credential·account ID·token을 코드나 Git에 기록하지 않는다.
- Source snapshot은 실행 중 메모리에만 보관한다. `reports/source`에 원본 응답을 저장하지 않는다.

## Run

Python 3.10 이상. 프로젝트 디렉터리에서 아래 명령어를 실행한다.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
python -m pip install uv
```

`uv`는 외부 MCP 서버 실행용 도구이며 프로젝트 패키지 dependency에 서버를 복사하지 않는다. AWS CLI는 mock 실행에 필요하지 않다.

Mock (AWS 자격증명 불필요)

```sh
python app.py --previous 2026-07 --current 2026-08 --mock
```

Live: AWS credential/profile, Billing 조회 권한, Bedrock 모델 접근을 준비한 경우에만 실행한다. Cost Explorer 요청에는 비용이 발생할 수 있다.

```sh
python app.py --previous 2026-07 --current 2026-08 --profile <AWS_PROFILE> --region us-east-1
```

기본 region은 `AWS_REGION` 또는 `us-east-1`이다. `--profile` 대신 shell의 `AWS_PROFILE`을 사용할 수 있다. `MODEL_ID`는 선택 사항이며 지정하지 않으면 Strands 기본 Bedrock 모델을 사용한다. 비용 API 없이 연결과 도구 이름만 검증하려면 같은 월 인자에 `--discover-only`를 추가한다.

## Output

`reports/cost_review_YYYY-MM.md`를 생성한다. Mock 리포트에는 `MOCK`과 fixture 출처를 명시한다. 리포트와 가상환경은 Git에서 제외한다.

## Test

```sh
pytest -q
```

Fixture 필터링, hook의 JSON/text 처리와 fail-closed 동작, mock의 무접속 실행, 리포트의 원본 금액·정렬·제외 구역을 검증한다.

## Known limitations

- 이 환경에서는 AWS 자격증명을 찾지 못해 live 비용 조회와 Bedrock 호출을 실행하지 않았다. MCP 연결·tool discovery만 성공했고 mock 실행은 성공했다. 계정 권한과 Cost Explorer/Cost Optimization Hub 준비 상태는 확인되지 않았다. 자동 enable은 하지 않는다.
- 확인한 Billing MCP 서버 버전은 `list_recommendations`를 `data.recommendations`의 snake_case로 정규화하면서 `tags`, `restartNeeded`, `rollbackPossible`, `source` 등 일부 AWS 필드를 전달하지 않는다. 누락 값은 `N/A`이고, 태그가 전달되지 않은 live 권고의 태그 예외 적용 여부는 확인 불가라고 리포트에 표시한다.
- Mock fixture는 지정된 2026-07/2026-08 비교 전용이며, mock 설명은 LLM 출력이 아닌 고정 문구다. 실제 비용이나 권고를 나타내지 않는다.

## Future work

- AWS MCP Server를 통한 상세 리소스 구성 확인
- 이전 리뷰의 권고 채택·기각 기록 반영
- decisions context 피드백 루프
- 필요할 경우 documentation 근거 연결
