# SigNoz + kubectl 인프라 장애 대응 에이전트

3주차 과제 · 문영호 · [Issue #9](https://github.com/cloud-club/10th-agent/issues/9)

파이썬 LLM 루프가 공식 SigNoz MCP 서버에서 관측 정보를 가져오고, kubectl로 Kubernetes 상태를 확인하거나 장애 조치를 수행합니다. 조회 결과를 다음 LLM 입력으로 전달하고 최대 단계 수 안에서 분석을 반복합니다.

```text
사용자 → Python Agent → LLM (기존 llama.cpp의 chat completions API)
              ├─ SigNoz MCP (HTTPS /mcp) → SigNoz API → 로그·메트릭·트레이스
              └─ kubectl → 상태 확인 / Deployment restart·scale·undo
```

## 설치 및 실행

Python 3.11 이상, kubectl, 접근 가능한 kubeconfig가 필요합니다. `uv`를 사용하면 Python 설치와 의존성 고정을 함께 관리할 수 있습니다.

```bash
cd yhmoon/week-03
uv sync --python 3.12 --extra dev
cp .env.example .env  # 이미 .env가 있다면 덮어쓰지 않습니다.
chmod 600 .env
```

`.env`에 SigNoz 조회 API 키를 입력하고 `KUBE_CONTEXT`, `KUBE_NAMESPACES`를 본인 환경에 맞게 지정합니다. LLM은 기존 `https://llm-big.hoydev.kr/v1`, 모델 `qwen`을 기본으로 사용합니다. 같은 chat completions 및 function calling 인터페이스를 지원하는 다른 모델도 설정할 수 있습니다. `LLM_CHAT_TEMPLATE_KWARGS`는 llama.cpp에서 thinking을 끄는 옵션이며 다른 제공자 사용 시 제거합니다.

```bash
uv run sre-agent check
uv run sre-agent tools
uv run sre-agent run '최근 15분간 signoz 네임스페이스의 오류 로그와 Deployment 상태를 확인해줘.'
uv run sre-agent run 'ai 네임스페이스의 장애를 분석하고 필요한 조치를 제안해줘.' --max-steps 10
uv run sre-agent run 'ai 네임스페이스의 장애를 분석하고 필요한 경우 조치해줘.' --execute
```

`--execute`가 없으면 조치는 제안으로만 반환합니다. `--execute` 모드에서는 정확한 kubectl 명령과 이유를 표시하며 사용자가 터미널에서 `execute`를 입력한 건만 실행합니다. 비대화형 실행에서는 조치를 거부합니다. 대상 네임스페이스를 제한하고 context를 매 호출마다 고정합니다. 임의 shell, exec, apply, delete, Secret 조회는 지원하지 않습니다. 현재 조치 범위는 Deployment restart, scale(1~10), undo입니다.

조치 후 rollout 상태와 새 SigNoz 관측 정보를 확인하도록 프롬프트에 지정했습니다. 이는 LLM의 분석 절차이며 자동 복구 성공을 보장하지 않습니다. timeout은 결과 불명으로 처리하고 재조회하게 합니다.

분석 보고서는 `reports/*.md`, Tool 호출 및 결과는 `reports/*.jsonl`에 저장됩니다. 파일 권한은 600이며 API 키는 마스킹합니다. 로그 자체에는 업무 데이터가 포함될 수 있으므로 실행 기록은 Git에서 제외합니다. 단계 수, 조회 응답 크기와 kubectl 실행 시간을 제한합니다.

## 구성

| 파일 | 역할 |
|---|---|
| `sre_agent/signoz.py` | 공식 MCP Python SDK로 연결·Tool 목록 조회, 조회 Tool 15종 허용 |
| `sre_agent/kubernetes.py` | 구조화한 인자로 kubectl 실행, 대상 제한과 조치 확인 |
| `sre_agent/agent.py` | LLM function calling 루프, 입력 검증, 실행 기록·보고서 |
| `sre_agent/cli.py` | check/tools/run 명령과 환경 설정 |
| `infra/signoz-mcp.yaml` | MCP Deployment, Service, Istio HTTPS 라우팅 |
| `tests/` | 위험 입력, 승인/거부, timeout, 에이전트 루프 검증 |

SigNoz 조회 Tool은 런타임의 MCP 스키마를 읽어 사용합니다. 로그/메트릭/트레이스/알림/서비스 조회를 허용하며 SigNoz 수정 Tool과 범용 쿼리 실행 Tool은 에이전트에 노출하지 않습니다. API 키 자체에도 `signoz-viewer` 역할을 부여합니다.

## MCP 배포

- 이미지: `signoz/signoz-mcp-server:v0.14.0`
- Namespace / Deployment: `signoz` / `signoz-mcp`
- 내부 Service: `signoz-mcp.signoz.svc.cluster.local:8000`
- 접속 URL: `https://signozmcp.hoydev.kr/mcp`
- 연결 대상: `http://signoz.signoz.svc.cluster.local:8080` (SigNoz v0.137.0)
- 서버에는 API 키를 저장하지 않고 클라이언트가 `SIGNOZ-API-KEY` 헤더로 전달합니다.
- SigNoz backend URL allowlist로 다른 서버로의 프록시 사용을 제한합니다.
- 기존 `istio-system/home-gateway`와 와일드카드 인증서를 사용합니다.
- DNS: `signozmcp.hoydev.kr` → `signoz.hoydev.kr` CNAME. 기존 도메인은 사설 IP로 연결되므로 같은 네트워크/VPN에서 접근해야 합니다.

```bash
kubectl apply --dry-run=server -f infra/signoz-mcp.yaml
kubectl apply -f infra/signoz-mcp.yaml
kubectl rollout status deployment/signoz-mcp -n signoz
```

별도 환경에서는 도메인, 게이트웨이, SigNoz 주소를 수정합니다. DNS 레코드는 클러스터 매니페스트에 포함되지 않습니다. SigNoz Settings → Service Accounts에서 조회 역할의 계정과 API 키를 발급해 `.env`에 저장합니다. 이 환경의 계정명은 `week03-sre-agent`이며 키 만료/갱신은 해당 화면에서 관리합니다.

일시적으로 도메인 연결이 불가능하면 다음 포트 포워딩 후 `SIGNOZ_MCP_URL=http://127.0.0.1:18000/mcp`를 사용합니다.

```bash
kubectl port-forward -n signoz svc/signoz-mcp 18000:8000
```

MCP만 제거하려면 `kubectl delete -f infra/signoz-mcp.yaml`을 사용합니다. 기존 SigNoz에는 영향을 주지 않습니다. DNS와 서비스 계정/API 키는 필요에 따라 별도로 제거합니다.

## 검증

```bash
uv run pytest -q
uv run sre-agent check
```

실제 설치 환경에서 인증 없는 MCP 요청 거부, 조회 키를 통한 서비스/로그/메트릭 조회, LLM의 MCP와 kubectl 호출 및 최종 보고서 생성을 검증했습니다. 상세 결과는 `VALIDATION.md`에 정리했습니다. 실제 서비스에 장애를 주입하지 않으며 조치 실행 검증에는 일회성 테스트 네임스페이스를 사용합니다. 실제 관측값과 실행 기록은 로컬 `reports/`에서 확인할 수 있습니다.

## 참고

- [공식 SigNoz MCP 서버](https://github.com/SigNoz/signoz-mcp-server)
- [SigNoz MCP 설정](https://signoz.io/docs/ai/signoz-mcp-server/)
- [SigNoz 서비스 계정](https://signoz.io/docs/manage/administrator-guide/iam/service-accounts/)
