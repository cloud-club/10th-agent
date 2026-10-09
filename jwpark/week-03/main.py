"""
문서 요약 에이전트 (Llama-3.2-1B-Instruct 기반, Pydantic 적용)

사용법:
    python summarize_agent.py my_document.txt
    python summarize_agent.py my_document.txt --max_new_tokens 300

필요 패키지:
    pip install transformers torch pydantic
"""

import argparse
from typing import List, Optional

import torch
from pydantic import BaseModel, Field, field_validator
from transformers import AutoTokenizer, AutoModelForCausalLM


# ---------------------------------------------------------------------------
# 설정 / 데이터 모델
# ---------------------------------------------------------------------------

class SummarizerConfig(BaseModel):
    model_name: str = Field(default="meta-llama/Llama-3.3-70B-Instruct")
    max_new_tokens: int = Field(default=512)  # 구조화된 긴 답변을 위해 증가
    chunk_tokens: int = Field(default=800)    # 소형 모델의 문맥 상실 방지를 위해 축소
    system_prompt: str = Field(
        default=(
            "당신은 IT 기술 및 개발 관련 문서를 분석하고 구조화하는 전문 AI입니다. "
            "주어진 텍스트에서 주요 기술적 주제, 트러블슈팅 해결법, 핵심 개념을 추출하여 "
            "명확한 글머리 기호(Bullet points) 형태로 요약하세요. 절대 원문에 없는 정보를 지어내지 마세요."
        )
    )
    model_config = {"frozen": True}

    model_config = {"frozen": True}  # 생성 이후 설정 변경 방지


class DocumentInput(BaseModel):
    text: str = Field(min_length=1)

    @field_validator("text")
    @classmethod
    def strip_and_check(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("문서 내용이 비어 있습니다.")
        return v


class SummaryResult(BaseModel):
    summary: str
    num_chunks: int
    partial_summaries: List[str] = Field(default_factory=list)

# ---------------------------------------------------------------------------
# 에이전트
# ---------------------------------------------------------------------------

class DocumentSummarizerAgent:
    def __init__(self, config: Optional[SummarizerConfig] = None):
        self.config = config or SummarizerConfig()
        self.tokenizer = AutoTokenizer.from_pretrained(self.config.model_name)
        self.model = AutoModelForCausalLM.from_pretrained(
            self.config.model_name,
            device_map="auto",
            dtype=torch.bfloat16,
        )

    def _generate(self, prompt: str) -> str:
        messages = [
            {"role": "system", "content": self.config.system_prompt},
            {"role": "user", "content": prompt},
        ]
        inputs = self.tokenizer.apply_chat_template(
            messages,
            add_generation_prompt=True,
            tokenize=True,
            return_dict=True,
            return_tensors="pt",
        ).to(self.model.device)

        # 소형 모델을 위한 보수적 생성 파라미터 적용
        outputs = self.model.generate(
            **inputs,
            max_new_tokens=self.config.max_new_tokens,
            do_sample=True,             # 무한 루프 탈출을 위한 샘플링
            temperature=0.2,            # 환각 방지를 위해 창의성 억제
            top_p=0.9,
            repetition_penalty=1.2,     # 단어 반복 억제
            no_repeat_ngram_size=4,     # 구문 반복 원천 차단
            pad_token_id=self.tokenizer.eos_token_id
        )
        
        text = self.tokenizer.decode(
            outputs[0][inputs["input_ids"].shape[-1]:],
            skip_special_tokens=True,
            clean_up_tokenization_spaces=False
        )
        return text.strip()

    def _split_into_chunks(self, text: str) -> List[str]:
        # 특수 토큰 자동 추가 방지 및 BPE 디코딩 오류 방지 처리 완료
        token_ids = self.tokenizer.encode(text, add_special_tokens=False)
        chunk_size = self.config.chunk_tokens
        
        return [
            self.tokenizer.decode(
                token_ids[i:i + chunk_size],
                skip_special_tokens=True,
                clean_up_tokenization_spaces=False
            )
            for i in range(0, len(token_ids), chunk_size)
        ]

    def summarize(self, document: DocumentInput) -> SummaryResult:
        chunks = self._split_into_chunks(document.text)

        if len(chunks) == 1:
            prompt = "다음 문서의 주요 기술 주제와 핵심 내용을 글머리 기호를 사용해 요약해줘:\n\n" + document.text
            summary = self._generate(prompt)
            return SummaryResult(summary=summary, num_chunks=1)

        partial_summaries = []
        for idx, chunk in enumerate(chunks, 1):
            prompt = (
                f"다음 텍스트에서 기술적인 핵심 주제와 해결 방법을 추출하여 3문장 이내로 요약해줘:\n\n{chunk}"
            )
            partial_summaries.append(self._generate(prompt))

        combined = "\n\n".join(partial_summaries)
        
        # 3. 최종 프롬프트: 출력 형식을 강제로 지정하여 소형 모델의 길 잃음 방지
        final_prompt = (
            "다음은 기술 관련 질의응답 문서들을 부분별로 요약한 내용입니다. "
            "이 내용들만 종합하여, 절대 새로운 정보를 지어내지 말고 아래 형식에 맞춰 최종 요약문을 작성해줘:\n\n"
            "- 핵심 주제 요약 (1~2문장)\n"
            "- 주요 카테고리별 요약 (글머리 기호 사용)\n\n"
            f"부분 요약 내용:\n{combined}"
        )
        final_summary = self._generate(final_prompt)

        return SummaryResult(
            summary=final_summary,
            num_chunks=len(chunks),
            partial_summaries=partial_summaries,
        )

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="문서 요약 에이전트")
    parser.add_argument("file", help="요약할 텍스트 파일 경로 (.txt)")
    parser.add_argument("--model", default="meta-llama/Llama-3.2-1B-Instruct")
    parser.add_argument("--max_new_tokens", type=int, default=512)
    parser.add_argument("--chunk_tokens", type=int, default=800)
    args = parser.parse_args()

    with open(args.file, "r", encoding="utf-8") as f:
        raw_text = f.read()

    config = SummarizerConfig(
        model_name=args.model,
        max_new_tokens=args.max_new_tokens,
        chunk_tokens=args.chunk_tokens,
    )
    document = DocumentInput(text=raw_text)

    agent = DocumentSummarizerAgent(config=config)
    result = agent.summarize(document)

    print("\n=== 요약 결과 ===")
    print(result.summary)
    print(f"\n(청크 수: {result.num_chunks})")


if __name__ == "__main__":
    main()