# 10th Agent Study

LLM의 기초부터 Agent의 동작 방식과 설계 패턴까지 함께 공부하고, 직접 Agent를 구현해 보는 8주 스터디입니다.

## 스터디 개요

## 대상

Agent를 직접 만들어 보고 싶고, 함께 공부하며 시행착오를 나누고 싶은 분들을 대상으로 합니다.

## 스터디원

| 이름 | 제출 디렉터리 |
| --- | --- |
| 문영호 | [`yhmoon`](./yhmoon) |
| 이상원 | [`swlee`](./swlee) |
| 이은지 | [`ejlee`](./ejlee) |
| 윤서영 | [`syyoon`](./syyoon) |
| 이정인 | [`jilee`](./jilee) |
| 이예승 | [`yslee`](./yslee) |
| 박정우 | [`jwpark`](./jwpark) |

## 커리큘럼

| 주차 | 주제 | 핵심 질문 |
| --- | --- | --- |
| Week 1 | LLM에서 Agent까지 | LLM과 Agent는 무엇이 다른가? |
| Week 2 | Agent Loop & Tool 실습 | Agent는 실제로 어떻게 행동하는가? |
| Week 3 | Context Engineering | Agent에게 무엇을 보여줘야 하는가? |
| Week 4 | Context & Memory 실습 | 긴 작업에서 무엇을 기억해야 하는가? |
| Week 5 | Agent Architecture & Reliability | Agent를 어떻게 설계해야 하는가? |
| Week 6 | Agent Harness & Project Kick-off | 실제 Agent를 어떻게 제품처럼 구성하는가? |
| Week 7 | Project Build / Design Review | 내 Agent 구조는 적절한가? |
| Week 8 | Demo / Evaluation | 만든 Agent가 실제로 잘 동작하는가? |

## 진행 방식

- 주차별 과제는 GitHub Issue로 공지합니다.
- 실습 결과는 Pull Request로 공유하고 서로 리뷰합니다.
- 질문과 논의가 필요한 내용은 GitHub Issue 또는 Slack을 활용합니다.
- 3회 이상 결석하면 수료할 수 없습니다.

## 과제 제출 방법

1. 과제 Issue를 확인합니다.
2. `main` 브랜치를 기준으로 과제별 브랜치를 생성합니다.
3. 자신의 디렉터리 아래에 주차별 과제를 작성합니다.
4. 과제 Issue를 연결한 Pull Request를 생성합니다.
5. 리뷰를 반영한 뒤 `main` 브랜치에 병합합니다.

### 제출 규칙

Week 1 과제를 `yhmoon`이 제출하는 경우 다음 규칙을 사용합니다.

| 구분 | 형식 | 예시 |
| --- | --- | --- |
| 브랜치 | `week-NN/{제출 디렉터리}` | `week-01/yhmoon` |
| 제출 경로 | `{제출 디렉터리}/week-NN/` | `yhmoon/week-01/` |
| PR 제목 | `[Week N] {제출 디렉터리} - {과제명}` | `[Week 1] yhmoon - Agent 개념 정리` |
| PR 본문 | `Refs #{과제 Issue 번호}` | `Refs #1` |

과제 Issue는 모든 스터디원이 함께 사용하므로, 첫 번째 PR에서 Issue가 닫히지 않도록 `Closes` 대신 `Refs`를 사용합니다. 병합이 끝난 과제 브랜치는 삭제합니다.

## 참고 자료

- [메인 교재](https://product.kyobobook.co.kr/detail/S000219751990)
- [Anthropic - Building Effective Agents](https://www.anthropic.com/engineering/building-effective-agents)
- [Anthropic - Effective Context Engineering for AI Agents](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
- [OpenAI - A Practical Guide to Building Agents](https://openai.com/business/guides-and-resources/a-practical-guide-to-building-ai-agents/)
- [LangChain - Context Engineering](https://docs.langchain.com/oss/python/langchain/context-engineering)
- [LangChain - Deep Agents](https://docs.langchain.com/oss/python/deepagents/overview)
- [LY Corporation - Grafana에서 자연어로 장애 원인을 분석하기](https://techblog.lycorp.co.jp/ko/analyzing-incident-root-causes-in-grafana-using-natural-language-with-llm-agent)
- [ReAct](https://arxiv.org/abs/2210.03629)
- [MemGPT](https://arxiv.org/abs/2310.08560)

## 실습 리소스

- [A.X 4.0](https://huggingface.co/skt/A.X-4.0)
- [Sionic AI Embedding API v2](https://huggingface.co/sionic-ai/sionic-ai-v2)
