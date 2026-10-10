# AWS Cost Review Agent

매달 AWS 비용을 확인하고 절감할 항목을 정리하는 일을 줄이려고 만든 Python 에이전트다.

비교할 두 달을 입력하면 비용 변화와 절감 권고를 가져와 리포트로 만든다. 월말 배치용 서버처럼 유지해야 하는 항목은 미리 적어 둔 업무 예외를 반영해 제외한다.

Python이 원본 데이터로 수치 표를 만들고, AI가 설명을 붙인다. AWS 리소스를 직접 변경하지는 않는다.

## 현재까지 구현한 것

비용 비교 표, 업무 예외 처리, 조회 기간·횟수 제한, 필수 조회의 완료 확인, 출력 검증, 사전 점검 스크립트를 구현했다.

MCP 서버 연결과 도구 목록 조회는 확인했다. 에이전트의 실행 흐름은 가짜 모델과 샘플 데이터로 검증했으며, 실제 AWS 데이터와 실제 AI 모델을 함께 사용하는 전체 실행은 아직 완료하지 못했다.

## 남은 작업

현재 Bedrock 모델 호출에서 `Error 002`가 발생해 실제 실행이 막혀 있다.

1. Bedrock 계정 접근 문제 해결
2. Cost Explorer·Cost Optimization Hub 활성화와 데이터 수집 확인, Anthropic 모델 접근 준비
3. 사전 점검 후 실제 실행하고 AWS 콘솔 값과 리포트 대조

전체 모델 호출·토큰 상한, 실행 기록, MCP 서버 버전 고정도 추가로 보완할 예정이다.

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
