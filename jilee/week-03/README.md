# [Week 3] 영업 회의록 에이전트

회의 녹취(STT 결과)를 넣으면 영업 SOP 표준 양식의 회의록과 액션아이템을 만들고, 직전 회의의 액션아이템을 이어받아 점검하는 에이전트다. Week 2 기획서(`jilee/week-02`)의 1단계(MVP)를 파이썬으로 구현했다. 범위·의사결정·완료 조건은 [작업정의서.md](./작업정의서.md), 설계 근거·실험·평가 지표는 [기술정의서.md](./기술정의서.md)에 있다.

```text
녹취(transcript_N.txt) ─▶ Agent Loop ─▶ 회의록(minutes_N.md) ─▶ 다음 회의의 입력(기억)
                          ├─ LLM: Groq 무료 · qwen/qwen3.8-27b (OpenAI 호환, 표준 라이브러리로 직접 호출)
                          │        한도 초과 시 OpenRouter 무료 모델로 자동 폴백
                          └─ Tool: read_deal_context · read_transcript · consult_sop(SOP 서브에이전트) · save_minutes
```

## 실행

Python 3.12. 외부 패키지 설치 없음.

```bash
cd jilee/week-03
cp .env.example .env        # Groq 키 입력 (https://console.groq.com, 무료·카드 불필요). 폴백용 OpenRouter 키는 선택

python app.py               # 로컬 앱 → http://localhost:8765
python run.py D001 1        # 터미널 실행 (딜 코드, 회차)
python run.py D001 1 --no-knowledge   # 비교 실험: 지식베이스 없이 → experiments/no_knowledge/
python -m unittest          # 가짜 LLM으로 루프·정지조건·오류 처리 + SOP 검색 검증 (6건)
```

로컬 앱에서는 딜·회차를 고르고 녹취를 불러오거나 붙여넣은 뒤 "에이전트 실행"을 누르면, 도구 호출 과정이 진행 막대와 함께 단계별로 표시되고 완성된 회의록이 오른쪽에 뜬다. 무료 요금제라 한 번에 30초~2분 걸린다.

## 구성

| 경로 | 내용 |
| --- | --- |
| `agent/llm.py` | OpenAI 호환 chat completions 호출. 1순위 제공자가 한도 초과(429)로 오래 막히면 `.env`의 폴백 제공자로 전환 |
| `agent/tools.py` | Tool 4종과 스키마. 모델은 판단만 하고 파일 읽기·쓰기는 이 함수들이 한다 |
| `agent/sop_agent.py` | SOP 서브에이전트. 질문에 맞는 SOP 문단을 검색(드문 단어 가중)해 그 발췌만 근거로 답하는 별도 LLM 호출 |
| `agent/loop.py` | Agent Loop(모델이 도구를 고르면 실행해 결과를 되돌려주는 반복)와 시스템 프롬프트. 저장 성공이 정지조건 |
| `knowledge/` | 지식베이스 — 표준 질문 리스트(Q01~Q11)·회의록 표준 양식. `sop/`에는 사내 영업 SOP 정본 10편을 두는데, 사내 문서라 저장소에는 안내문만 올린다 |
| `deals/D001/` | 샘플 딜: 고객정보, 녹취 3회차(STT 오류 주입), 생성된 회의록 3회차 |
| `experiments/` | 지식베이스 없이 돌린 결과, STT 오류 넣기 전 원문 녹취 |
| `static/index.html` | 로컬 앱 화면 |
| `tests/` | 단위 테스트 |

## 결과 요약

| 회차 | 녹취 특징 | 확보율 | 승계 | 액션아이템 |
| --- | --- | --- | --- | --- |
| 1차 | 첫 회의, STT 오류 8건 | 64% (7/11) | — | 3건 |
| 2차 | 공장장 참석, 예산·일정 확정 | 91% (10/11) | 1차 3건 전량 완료 판정 | 4건 |
| 3차 | 긴 문장·자기정정·끼어들기 | 91% (10/11) | 2차 4건(완료 2·진행 중 2) | 5건 |

지식베이스(양식·질문 리스트)를 빼고 돌리면 회의록이 절반 길이로 줄고 형식이 실행마다 달라지며, 지난 액션아이템 승계와 미합의 사항 구분이 사라진다(`experiments/no_knowledge/`).

## 막힌 것

- 파이썬 `urllib` 기본 User-Agent로 Groq를 부르면 Cloudflare가 403(1010)으로 막는다 → User-Agent 지정.
- 저장 후에도 도구를 열어두면 모델이 `save_minutes`를 반복 호출했다 → 저장 성공 시 도구 없이 요약만 받고 루프를 닫는 정지조건 추가. `max_tokens` 미지정 시 회의록이 잘렸고 4,096에서도 3차 회의록이 잘려 8,192로 올렸다.
- 같은 녹취를 다시 돌리면 확보율 판정이 흔들린다(1차 27~64%) → 판정 규칙을 코드로 검사하는 것이 다음 과제.
- Groq 무료 일일 한도(20만 토큰)를 하루 실험으로 소진해 폴백 라우팅을 넣었다. OpenRouter의 Qwen 무료판은 공용 풀이라 자주 막혀 폴백 모델은 DeepSeek V4 Flash로 두었고, 추론형 모델은 응답의 `reasoning_details`를 되돌려 줘야 도구 흐름이 이어졌다.
- 기한 없는 일을 "확인 불가"로 액션아이템에 올리는 규칙 위반이 3차에서 나왔다(양식 규칙 4번).
