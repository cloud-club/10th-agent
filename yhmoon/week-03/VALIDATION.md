# 검증 기록

검증일: 2026-09-19 (KST)

- `pytest -q`: 20개 통과. 네임스페이스 이탈, 명령 주입, Secret 조회, 범위 밖 scale, 미승인 실행 차단, timeout 처리, MCP 조회 Tool 필터링/페이지 처리, LLM Tool 루프와 실행 기록 마스킹을 확인했습니다.
- Kubernetes: `signoz/signoz-mcp` Deployment ready 1/1.
- DNS/TLS: `signozmcp.hoydev.kr` CNAME과 기존 와일드카드 인증서로 HTTPS 접속 확인. 사설 네트워크 주소를 사용합니다.
- 인증: API 키 없는 `/mcp` initialize 요청은 HTTP 401.
- 실제 MCP 조회: 서비스 목록, 최근 로그 3개, 메트릭 목록 5개 조회 성공. 서비스 목록은 빈 배열이었으며 이를 서비스 정상이나 로그 부재로 해석하지 않았습니다.
- kubectl 실제 조치: 일회성 `sre-agent-test/agent-smoke` Deployment를 만들어 restart → rollout 확인 → scale 2 → rollout 확인 → undo → rollout 확인을 모두 성공했습니다. 검증 후 테스트 네임스페이스를 삭제했습니다.
- LLM 통합: 기존 `llm-big.hoydev.kr`의 Qwen이 MCP 서비스 조회 → kubectl Deployment 조회 → 추가 시간 범위 조회 → 한국어 최종 보고서 작성을 완료했습니다. 로컬 보고서: `reports/20260918T160748.364639Z.md`.
- 기존 운영 워크로드에는 장애 조치를 실행하지 않았습니다.

API 키, 로그 원문, 환경별 LLM 실행 기록은 저장소에 포함하지 않습니다. 로컬 `.env`와 `reports/`에서 확인할 수 있습니다.
