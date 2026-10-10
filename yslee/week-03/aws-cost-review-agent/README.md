# AWS Cost Review Agent

매달 반복하는 AWS 비용 리뷰를 줄이기 위해 만든 Python 에이전트다.

### [동작 방식]
비교할 두 달을 입력하면 AWS Billing MCP에서 비용 변화와 주요 증가·감소 요인(cost driver), 최적화 권고를 가져와 Markdown 리포트로 정리한다.

### [배경]
비용 절감 권고가 있어도 월말 배치 때문에 서버 사양을 유지해야 하는 경우가 있다. 이런 업무 예외를 YAML에 적어 두고, Python이 리소스 ID와 태그를 정확히 비교해 권고를 걸러낸다. LLM은 조회 결과에 대한 설명을 작성하고, 금액과 비율이 들어가는 표는 Python이 원본 응답에서 만든다.

**읽기 전용이다.** (AWS 설정이나 리소스를 변경하지 않으며, 권고를 적용할지는 사람이 결정)

## 동작 방식

```text
비교할 두 달 입력
  → Strands Agent + Bedrock
  → Billing MCP로 비용 비교·cost driver·최적화 권고 조회
  → Python이 응답 검증과 업무 예외 적용
  → LLM이 설명 작성
  → Python이 수치 표와 설명을 합쳐 Markdown 리포트 저장
```

Agent에는 `cost-comparison`과 `cost-optimization` 두 도구만 제공한다. 비용 비교와 cost driver 조회는 각각 한 번, 최적화 권고 조회도 한 번만 허용한다. 비교 기간이 입력한 월과 다르면 호출을 취소하고, 필요한 세 조회가 모두 성공해야 리포트를 생성한다.

업무 예외는 [context/exceptions.yaml](context/exceptions.yaml)에 둔다. 리소스 ID 규칙을 먼저 적용하고, 해당 규칙이 없으면 태그 key/value를 확인한다. 제외된 권고도 리포트에 사유와 함께 남긴다. 저장소의 리소스 ID는 예제 값이다.

## 현재까지 한 작업 (~W5)

월간 리뷰에 필요한 MVP 기능은 구현했다. 구현 내용은 `5주차` 커밋(`a4542f8`)에 반영되어 있다.

- 전월·당월 비용, 항목별 변화, 주요 cost driver를 보여주는 비교 표
- 조회 기간 검증, 허용 도구·작업과 호출 횟수 제한
- 필수 조회의 성공 여부를 확인하는 완료 검증
- YAML 업무 예외 필터와 제외 사유 표시
- LLM 설명의 금액 표기를 검사하는 출력 검증기
- live 실행 전 계정·권한·모델 접근을 확인하는 `check_readiness.py`

오프라인 테스트 **36개가 통과**했다. 잘못된 기간의 호출을 취소한 뒤 다시 시도하는 흐름, 필수 조회가 빠졌을 때 리포트를 만들지 않는 동작, 원본 수치와 제외 사유가 리포트에 남는지를 확인했다.

| 검증 항목 | 현재 상태 |
| --- | --- |
| 실제 MCP 서버 연결과 도구 목록 조회 | 확인 |
| 샘플 데이터로 예외 처리와 리포트 생성 | 확인 |
| Strands 이벤트 루프 | 가짜 모델·도구를 사용한 오프라인 테스트로 확인 |
| 실제 AWS 데이터와 실제 Bedrock 모델을 함께 사용하는 전체 실행 | 아직 미완료 |

`--mock`은 AWS·MCP·LLM을 호출하지 않는다. 합성 데이터와 고정 설명을 사용한다. Strands 루프 테스트도 모델 응답을 미리 지정해 실행하므로, 실제 LLM의 판단이나 설명 정확도까지 검증한 것은 아니다.

## 남은 작업 (~W5)

현재는 Bedrock 모델 호출에서 `Error 002`가 발생해 첫 live 실행을 끝내지 못했다. 계정 접근 문제와 서비스 준비 상태를 먼저 확인해야 한다.

1. **Bedrock 접근 문제 해결**: AWS Support를 통해 `Error 002`의 계정 제한을 확인한다. 계정 플랜이 사용하려는 추론 방식을 지원하는지도 확인하고, 필요하면 모델 설정이나 플랜을 조정한다.
2. **비용 데이터와 권고 준비**: Cost Explorer와 Cost Optimization Hub를 활성화하고 데이터가 들어오는지 확인한다. Cost Explorer의 당월 데이터는 약 24시간, 과거 데이터는 며칠 더 걸릴 수 있다. Cost Optimization Hub의 권고 수집은 최대 24시간이 걸릴 수 있다. Compute Optimizer 기반 권고가 필요하면 해당 서비스도 등록한다.
3. **모델 접근 준비**: Anthropic 모델을 사용하려면 use case form을 제출하고 모델 사용 가능 상태를 확인한다. 제출 후에도 계약 상태가 대기 중일 수 있다.
4. **사전 점검 후 live 실행**: 점검의 실패 항목을 해결하고, 경고와 생략된 항목도 확인한다. 실제 Cost Explorer 조회와 모델 호출이 성공하면 리뷰를 실행하고, 같은 기간·metric·필터로 조회한 AWS 콘솔 값과 리포트를 대조한다.

## 실행

Python 3.10 이상이 필요하다. 프로젝트 디렉터리에서 설치한다.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
python -m pip install uv
```

샘플 데이터로 실행하려면 다음 명령을 사용한다. fixture는 2026년 7월과 8월 비교용이다.

```sh
python app.py --previous 2026-07 --current 2026-08 --mock
```

리포트는 `reports/cost_review_2026-08.md`에 저장된다. 다른 디렉터리에 저장하려면 `--output-dir`을 지정한다. 같은 디렉터리에서 같은 현재 월로 다시 실행하면 기존 파일을 덮어쓴다.

MCP 연결과 도구 목록만 확인하려면:

```sh
python app.py --previous 2026-08 --current 2026-09 --discover-only
```

사전 점검은 사용할 AWS profile로 실행한다. 아래의 `YOUR_AWS_PROFILE`은 실제 profile 이름으로 바꾼다.

```sh
python check_readiness.py --profile YOUR_AWS_PROFILE --region us-east-1
```

기본 점검은 실제 Cost Explorer 비교 조회와 Bedrock 모델 호출을 생략한다. 두 항목까지 확인하려면 `--ce`와 `--invoke`를 추가한다. 이 요청에는 API·모델 사용 비용이 발생한다.

```sh
python check_readiness.py --profile YOUR_AWS_PROFILE --region us-east-1 \
  --ce --previous 2026-08 --current 2026-09 --invoke
```

점검이 끝나면 같은 profile과 모델 설정으로 live 리뷰를 실행한다.

```sh
python app.py --previous 2026-08 --current 2026-09 \
  --profile YOUR_AWS_PROFILE --region us-east-1
```

`MODEL_ID`로 Bedrock 모델을 지정할 수 있다. 지정하지 않으면 Strands 기본 모델을 사용한다. 자격증명은 AWS 기본 credential chain을 사용하며, `--profile` 대신 `AWS_PROFILE`을 설정해도 된다.

테스트:

```sh
pytest -q
```

## 아직 확인할 부분

- 확인한 MCP 서버 버전(`0.0.37`)은 권고 목록에 태그·재시작 여부 등 일부 필드를 전달하지 않는다. 누락 값은 `N/A`로 표시하고, 태그 예외 적용 여부는 확인 불가로 남긴다.
- 출력 검증기는 일부 금액 표기를 걸러내지만, 설명의 사실성까지 보장하지는 않는다. 실제 모델의 설명이 조회 결과에 근거하는지는 live 검증에서 확인해야 한다.
- 도구 호출 횟수는 제한하지만 전체 모델 호출·토큰 상한은 아직 설정하지 않았다. 실행 메트릭 기록과 MCP 서버 버전 고정도 남아 있다.

다음 목표는 실제 데이터로 첫 리포트를 생성하고 콘솔 값과 대조하는 것이다. 추가 코드 보완 범위는 이 실행에서 확인한 결과를 바탕으로 정할 예정이다.
