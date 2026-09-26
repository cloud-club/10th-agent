import asyncio
import os
import json
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field
from mcp.client.stdio import stdio_client, StdioServerParameters
from mcp.client.session import ClientSession
from transformers import AutoTokenizer, AutoModelForCausalLM
import argparse
import re

import torch
from pydantic import BaseModel, Field, field_validator
# ---------------------------------------------------------------------------
# 1. 에이전트 설정
# ---------------------------------------------------------------------------
class EksAgentConfig(BaseModel):
    aws_region: str = Field(default="ap-northeast-2", description="대상 EKS 리전")
    allow_write: bool = Field(default=True, description="클러스터 리소스 수정 권한")
    
    # 앞서 해결한 uvx 명령어와 AWS SSO 의존성 설정
    mcp_command: str = Field(default="uvx")
    mcp_package: str = Field(default="awslabs.eks-mcp-server@latest")
    

    model_name: str = Field(default="meta-llama/Llama-3.2-1B-Instruct")
    max_new_tokens: int = Field(default=512)  # 구조화된 긴 답변을 위해 증가
    chunk_tokens: int = Field(default=800)    # 소형 모델의 문맥 상실 방지를 위해 축소
    model_config = {"frozen": True}



# ---------------------------------------------------------------------------
# 2. 메인 에이전트 클래스
# ---------------------------------------------------------------------------
class EksAddonAgent:
    def __init__(self, config: EksAgentConfig):
        self.config = config
        
        env = os.environ.copy()
        env["AWS_REGION"] = self.config.aws_region

        # uvx에 boto3[crt] 의존성 주입하여 AWS SSO 에러 방지
        args = ["--with", "boto3[crt]", self.config.mcp_package]
        
        if self.config.allow_write:
            args.append("--allow-write")
            args.append("--allow-sensitive-data-access")

        self.server_params = StdioServerParameters(
            command=self.config.mcp_command,
            args=args,
            env=env
        )
        self.config = config or SummarizerConfig()
        self.tokenizer = AutoTokenizer.from_pretrained(self.config.model_name)
        self.model = AutoModelForCausalLM.from_pretrained(
            self.config.model_name,
            device_map="auto",
            dtype=torch.bfloat16,
        )

    def _format_tools_for_llm(self, mcp_tools: List[Any]) -> List[Dict]:
        """MCP 서버에서 가져온 도구 목록을 LLM(OpenAI/Llama 등)이 이해할 수 있는 JSON Schema로 변환"""
        formatted = []
        for tool in mcp_tools:
            formatted.append({
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    # 수정된 부분: inputSchema -> input_schema
                    "parameters": tool.input_schema 
                }
            })
        return formatted

    async def _ask_llm(self, prompt: str, available_tools: List[Dict]) -> Dict:
        """
        실제 로컬 LLM(Hugging Face)을 호출하여 Tool 사용 여부를 결정하는 메서드
        """
        print(f"\n[Agent] LLM에게 다음 요청 분석을 지시합니다: '{prompt}'")
        
        # 1. 시스템 프롬프트 작성 (JSON 응답 강제 및 사용 가능한 도구 목록 주입)
        system_prompt = (
            "You are an AI assistant managing an AWS EKS cluster. "
            "You MUST use the provided tools to fulfill the user's request. "
            "To use a tool, you MUST respond ONLY with a valid JSON object. "
            "CRITICAL: Do NOT include JSON schema keywords like 'properties' or 'required' inside tool_args. "
            "Provide the actual key-value pairs directly.\n\n"
            "Correct Example:\n"
            "{\n"
            '  "action": "call_tool",\n'
            '  "tool_name": "list_k8s_resources",\n'
            '  "tool_args": {\n'
            '    "cluster_name": "my-real-cluster-name",\n'
            '    "kind": "Pod",\n'
            '    "api_version": "v1",\n'
            '    "namespace": "kube-system"\n'
            '  }\n'
            "}\n\n"
            f"Available tools:\n{json.dumps(available_tools, indent=2, ensure_ascii=False)}"
        )
        
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ]
        
        # 2. 토크나이저를 통한 프롬프트 템플릿 적용 및 텐서 변환
        inputs = self.tokenizer.apply_chat_template(
            messages,
            add_generation_prompt=True,
            tokenize=True,
            return_dict=True,
            return_tensors="pt",
        ).to(self.model.device)
        
        # 3. 모델 생성 실행 (도구 호출은 정확성이 생명이므로 무작위성을 끕니다)
        outputs = self.model.generate(
            **inputs,
            max_new_tokens=512,
            do_sample=False,             # Greedy 탐색 (가장 확률 높은 결과만 생성)
            repetition_penalty=1.1,      # 가벼운 반복 억제
            pad_token_id=self.tokenizer.eos_token_id
        )
        
        # 프롬프트 이후에 새롭게 생성된 텍스트만 추출
        response_text = self.tokenizer.decode(
            outputs[0][inputs["input_ids"].shape[-1]:],
            skip_special_tokens=True,
            clean_up_tokenization_spaces=False
        ).strip()
        
        print(f"[LLM Raw Response] {response_text}")
        
        # 4. LLM 응답에서 JSON 파싱 (마크다운 텍스트나 앞뒤 불필요한 공백 제거)
        # 마크다운 블록(```json, ```)을 사전에 제거
        clean_text = response_text.replace("```json", "").replace("```", "").strip()
        
        try:
            # { 로 시작해서 } 로 끝나는 블록 추출
            json_match = re.search(r'\{.*\}', clean_text, re.DOTALL)
            if json_match:
                json_str = json_match.group(0)
                parsed_response = json.loads(json_str)
                return parsed_response
            else:
                raise ValueError("JSON object not found.")
                
        except (json.JSONDecodeError, ValueError) as e:
            print(f"\n[Error] LLM did not return valid JSON: {e}")
            # 파싱 실패 시 폴백(Fallback) 응답
            return {
                "action": "error", 
                "message": f"Failed to parse LLM JSON response. Raw text: {response_text}"
            }

    async def execute_task(self, user_request: str):
        """MCP 세션을 열고 LLM과 통신하며 작업을 수행하는 메인 루프"""
        print(f"[*] Starting EKS Agent for region: {self.config.aws_region}")
        
        async with stdio_client(self.server_params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                print("[*] MCP Server Session Initialized Successfully.\n")

                # 1. 도구 목록 조회 및 LLM용으로 포맷팅
                tools_response = await session.list_tools()
                llm_tools = self._format_tools_for_llm(tools_response.tools)
                
                print(f"[*] Fetched {len(llm_tools)} tools from MCP server.")

                # 2. LLM에게 사용자 요청과 사용 가능한 도구 전달 (Plan 단계)
                llm_decision = await self._ask_llm(user_request, llm_tools)

                # 3. LLM의 결정에 따라 MCP 도구 실행 (Action 단계)
                # 엄격한 "action" 값 검사를 피하고 필수 키워드인 tool_name과 tool_args 존재 여부로 판단합니다.
                if "tool_name" in llm_decision and "tool_args" in llm_decision:
                    tool_name = llm_decision["tool_name"]
                    
                    # LLM이 action과 tool_name 필드 값을 거꾸로 적는 환각 현상에 대한 보정 로직
                    valid_tool_names = [t["function"]["name"] for t in llm_tools]
                    if tool_name not in valid_tool_names:
                        fallback_action = llm_decision.get("action", "")
                        if fallback_action in valid_tool_names:
                            tool_name = fallback_action
                            print(f"[Warn] LLM confused fields. Corrected tool_name to '{tool_name}'")
                        else:
                            print(f"\n[Error] LLM chose an invalid tool: '{tool_name}'")
                            return

                    tool_args = llm_decision["tool_args"]
                    
                    print(f"\n[Agent] Executing tool: '{tool_name}' with args: {json.dumps(tool_args)}")
                    
                    try:
                        # MCP 서버에 실제 도구 실행 요청
                        result = await session.call_tool(tool_name, tool_args)
                        
                        # 4. 결과 관찰 (Observation 단계)
                        print("\n[Agent] Tool Execution Result:")
                        for content in result.content:
                            text_out = content.text
                            # 결과가 너무 길 경우 가독성을 위해 자름
                            print(f"{text_out[:1000]}..." if len(text_out) > 1000 else text_out)
                            
                    except Exception as e:
                        print(f"\n[Error] Tool execution failed: {e}")
                else:
                    print("\n[Error] LLM response did not contain required tool execution parameters.")
                    print(f"Raw Parsed LLM Output: {llm_decision}")

# ---------------------------------------------------------------------------
# 3. 실행 진입점
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    config = EksAgentConfig(
        aws_region="ap-northeast-2",
        allow_write=True
    )
    agent = EksAddonAgent(config)
    
    # 테스트 시나리오: VPC CNI 관련 상태 체크
    test_prompt = "클러스터 이름은 'my-prod-cluster'야. kube-system 네임스페이스에 있는 aws-node 파드들의 상태를 확인해줘."
    asyncio.run(agent.execute_task(test_prompt))