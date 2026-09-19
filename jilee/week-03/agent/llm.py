"""OpenAI 호환 chat completions 호출 (표준 라이브러리만 사용).

프레임워크 없이 HTTP 요청을 직접 보낸다. base_url·키·모델명은 .env에서 읽으므로
Groq ↔ OpenRouter 등 OpenAI 호환 제공자를 값만 바꿔 교체할 수 있다.

라우팅: 1순위 제공자가 한도 초과(429)로 오래 막히면(Retry-After가 길거나 재시도 소진)
.env의 LLM_FALLBACK_* 제공자로 전환하고, 그 실행이 끝날 때까지 폴백을 유지한다.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

LONG_WAIT_SEC = 60  # 이보다 오래 기다리라고 하면(예: 일일 한도) 기다리지 않고 폴백으로 넘어간다


def load_env(path: Path | None = None) -> None:
    """.env 파일을 읽어 환경변수로 올린다. 이미 있는 변수는 덮어쓰지 않는다."""
    env_path = path or Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


@dataclass
class Provider:
    name: str
    base_url: str
    api_key: str
    model: str

    @property
    def ok(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)


class RateLimited(Exception):
    def __init__(self, wait: float, detail: str):
        super().__init__(detail)
        self.wait, self.detail = wait, detail


class LLMClient:
    def __init__(self, base_url: str | None = None, api_key: str | None = None, model: str | None = None):
        load_env()
        self.primary = Provider(
            "primary",
            (base_url or os.environ.get("LLM_BASE_URL", "")).rstrip("/"),
            api_key or os.environ.get("LLM_API_KEY", ""),
            model or os.environ.get("LLM_MODEL", ""),
        )
        self.fallback = Provider(
            "fallback",
            os.environ.get("LLM_FALLBACK_BASE_URL", "").rstrip("/"),
            os.environ.get("LLM_FALLBACK_API_KEY", ""),
            os.environ.get("LLM_FALLBACK_MODEL", ""),
        )
        if not self.primary.ok:
            raise RuntimeError(".env에 LLM_BASE_URL, LLM_API_KEY, LLM_MODEL을 설정해야 합니다.")
        self.active = self.primary
        self.switched_reason: str | None = None  # 폴백으로 넘어갔다면 그 이유(화면·로그 표시용)

    # 하위 호환: 기존 코드가 llm.model 등을 참조한다
    @property
    def model(self) -> str:
        return self.active.model

    def chat(self, messages: list[dict], tools: list[dict] | None = None, max_retries: int = 3) -> dict:
        """메시지 목록을 보내고 assistant 메시지(dict)를 돌려준다.

        분당 한도(429)는 Retry-After만큼 기다렸다 재시도한다. 기다릴 시간이 LONG_WAIT_SEC보다 길거나
        재시도가 소진되면 폴백 제공자로 전환한다. 폴백이 없으면 그대로 오류를 낸다.
        """
        for attempt in range(max_retries + 1):
            try:
                return self._post(self.active, messages, tools)
            except RateLimited as e:
                can_fallback = self.active is self.primary and self.fallback.ok
                if (e.wait > LONG_WAIT_SEC or attempt == max_retries) and can_fallback:
                    self.switched_reason = f"{self.primary.model}@{self.primary.base_url} 한도 초과({int(e.wait)}초 대기 요구) → 폴백 {self.fallback.model}"
                    self.active = self.fallback
                    continue  # 폴백도 같은 재시도 규칙을 탄다(무료 공용 풀은 잠깐 막히는 일이 잦다)
                if attempt < max_retries:
                    time.sleep(min(e.wait, LONG_WAIT_SEC))
                    continue
                raise RuntimeError(f"LLM 호출 실패 (429): {e.detail[:500]}") from e
        raise RuntimeError("LLM 호출 재시도 한도 초과")

    def _post(self, p: Provider, messages: list[dict], tools: list[dict] | None) -> dict:
        body: dict = {"model": p.model, "messages": messages, "temperature": 0.2, "max_tokens": 8192}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        req = urllib.request.Request(
            f"{p.base_url}/chat/completions",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {p.api_key}",
                "Content-Type": "application/json",
                "User-Agent": "meeting-agent/0.1",  # 기본 urllib UA는 Cloudflare(1010)에 막힌다
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            if e.code == 429:
                raise RateLimited(_retry_after(e.headers, detail), detail) from e
            raise RuntimeError(f"LLM 호출 실패 ({e.code}): {detail[:500]}") from e
        if "choices" not in data:  # OpenRouter는 200으로 오류 본문을 주기도 한다
            raise RuntimeError(f"LLM 응답 형식 오류: {json.dumps(data, ensure_ascii=False)[:500]}")
        return data["choices"][0]["message"]


def _retry_after(headers, detail: str) -> float:
    """Retry-After 헤더가 없으면 오류 본문의 'try again in 46m38s' 같은 문구에서 초를 뽑는다."""
    if headers.get("Retry-After"):
        try:
            return float(headers["Retry-After"])
        except ValueError:
            pass
    import re
    m = re.search(r"try again in (?:(\d+)h)?(?:(\d+)m)?(?:([\d.]+)s)?", detail)
    if m and any(m.groups()):
        h, mi, s = (float(x or 0) for x in m.groups())
        return h * 3600 + mi * 60 + s
    return 15.0
