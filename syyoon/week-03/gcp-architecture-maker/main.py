import json
import os
import google.generativeai as genai
from dotenv import load_dotenv

load_dotenv()

# 1. Google AI Studio API 키 설정
# Google AI Studio(https://aistudio.google.com/)에서 발급받은 API 키를 넣으세요.
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
genai.configure(api_key=GEMINI_API_KEY)

# 2. 데이터 및 지식베이스 로드
print("[1/4] 데이터 및 RAG 지식베이스 로드 중...")
with open("gcp_architecture.json", "r", encoding="utf-8") as f:
    gcp_data = json.load(f)

with open("gcp_styles_kb.json", "r", encoding="utf-8") as f:
    styles_kb = json.load(f)

with open("drawio_rules_kb.md", "r", encoding="utf-8") as f:
    layout_rules = f.read()

# 3. RAG 검색 (Retrieval): 필요한 GCP 리소스 스타일만 추출
print("[2/4] RAG 검색(Retrieval) 수행 중...")
retrieved_styles = {}
for res in gcp_data.get("resources", []):
    res_type = res.get("type")
    if res_type in styles_kb:
        retrieved_styles[res_type] = styles_kb[res_type]

retrieved_styles["default_edge"] = styles_kb.get("default_edge", {})
if "project_zone" in styles_kb:
    retrieved_styles["project_zone"] = styles_kb["project_zone"]

# 4. 프롬프트 합성 (Context Engineering)
print("[3/4] RAG 컨텍스트 합성 및 Gemini 프롬프트 구성 중...")
with open("prompts/prompt_template.txt", "r", encoding="utf-8") as f:
    prompt_template = f.read()

prompt = (
    prompt_template
    .replace("{gcp_data}", json.dumps(gcp_data, indent=2, ensure_ascii=False))
    .replace("{retrieved_styles}", json.dumps(retrieved_styles, indent=2, ensure_ascii=False))
    .replace("{layout_rules}", layout_rules)
)

# 5. Gemini 모델 호출 및 생성 (Generation)
print("[4/4] Gemini 모델로 draw.io XML 생성 중...")
with open("prompts/system_prompt.txt", "r", encoding="utf-8") as f:
    system_instruction = f.read().strip()

model = genai.GenerativeModel(
    model_name="gemini-2.5-flash",
    system_instruction=system_instruction
)

response = model.generate_content(
    prompt,
    generation_config=genai.types.GenerationConfig(
        temperature=0.2
    )
)

xml_content = response.text.strip()

# 마크다운 코드 블록 태그 제거
if xml_content.startswith("```"):
    lines = xml_content.split("\n")
    if lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].startswith("```"):
        lines = lines[:-1]
    xml_content = "\n".join(lines)

# 6. 파일 저장
output_filename = "gcp_architecture.drawio"
with open(output_filename, "w", encoding="utf-8") as f:
    f.write(xml_content)

print(f"성공적으로 완성되었습니다! 파일 생성됨: {output_filename}")