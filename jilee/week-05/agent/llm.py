"""OpenAI 호환 chat completions 호출 (표준 라이브러리만 사용).

프레임워크 없이 HTTP 요청을 직접 보낸다. base_url·키·모델명은 .env에서 읽으므로
Groq ↔ OpenRouter 등 OpenAI 호환 제공자를 값만 바꿔 교체할 수 있다.

라우팅(요청마다 어디로 보낼지 정한다 — LLMClient.route)
  1. 한도 초과 전환 : 1순위가 한도 초과(429)로 오래 막히면(Retry-After가 길거나 재시도 소진) 폴백으로 넘어가
                      그 실행이 끝날 때까지 폴백을 쓴다.
  2. 요청 크기      : 요청이 1순위의 요청 크기 한도(LLM_MAX_INPUT_TOKENS)보다 크면 그 요청만 폴백으로 보낸다.
                      작은 요청은 계속 1순위로 간다. 한도를 모르면 413을 한 번 받고 배운다(.env에 적어 둔다).
  3. 기본           : 그 밖에는 1순위.
요청마다 어느 쪽으로 왜 갔는지를 usage_log에 남긴다.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

LONG_WAIT_SEC = 60  # 이보다 오래 기다리라고 하면(예: 일일 한도) 기다리지 않고 폴백으로 넘어간다
CHARS_PER_TOKEN = 2.0  # 요청 글자 수 ÷ 입력 토큰 수의 첫 어림(한국어가 섞인 요청). 응답을 받을 때마다 실제 값으로 고친다


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


def save_env(updates: dict[str, str], path: Path | None = None) -> None:
    """.env에서 해당 키의 줄만 바꿔 쓰고(없으면 덧붙임) 실행 중인 환경변수에도 반영한다. 주석과 다른 줄은 그대로 둔다."""
    env_path = path or Path(__file__).resolve().parent.parent / ".env"
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    left = dict(updates)
    for i, line in enumerate(lines):
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            key = s.split("=", 1)[0].strip()
            if key in left:
                lines[i] = f"{key}={left.pop(key)}"
    lines += [f"{k}={v}" for k, v in left.items()]
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    os.environ.update(updates)


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
    def __init__(self, wait: float, detail: str, too_large: bool = False):
        super().__init__(detail)
        self.wait, self.detail, self.too_large = wait, detail, too_large


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
        self.usage_log: list[dict] = []  # 호출마다 모델·토큰 사용량·보낸 곳과 까닭(실행 기록용)
        self.max_input = int(os.environ.get("LLM_MAX_INPUT_TOKENS") or 0)  # 1순위가 한 요청에 받는 입력 토큰. 0이면 아직 모른다
        self.chars_per_token = CHARS_PER_TOKEN

    # 하위 호환: 기존 코드가 llm.model 등을 참조한다
    @property
    def model(self) -> str:
        return self.active.model

    @staticmethod
    def _chars(messages: list[dict], tools: list[dict] | None) -> int:
        return len(json.dumps([messages, tools or []], ensure_ascii=False))

    def estimate_tokens(self, messages: list[dict], tools: list[dict] | None = None) -> int:
        return int(self._chars(messages, tools) / self.chars_per_token)

    def route(self, messages: list[dict], tools: list[dict] | None = None) -> tuple[Provider, str]:
        """이 요청을 어디로 보낼지와 그 까닭(모듈 머리말의 세 규칙)."""
        if self.active is self.fallback:
            return self.fallback, "한도 초과 전환"
        est = self.estimate_tokens(messages, tools)
        if self.max_input and est > self.max_input and self.fallback.ok:
            return self.fallback, f"요청 크기(약 {est:,}토큰 > 1순위 한도 {self.max_input:,})"
        return self.primary, "기본"

    def chat(self, messages: list[dict], tools: list[dict] | None = None, max_retries: int = 3) -> dict:
        """메시지 목록을 보내고 assistant 메시지(dict)를 돌려준다.

        분당 한도(429)는 Retry-After만큼 기다렸다 재시도한다. 기다릴 시간이 LONG_WAIT_SEC보다 길거나
        재시도가 소진되면 폴백 제공자로 전환한다. 폴백이 없으면 그대로 오류를 낸다.
        요청 하나가 1순위의 요청 크기 한도보다 크면(413) 기다려도 통과하지 못하므로, 한도를 배워 두고 그 요청만 폴백으로 보낸다.
        """
        attempt = 0
        while True:
            provider, why = self.route(messages, tools)
            try:
                msg = self._post(provider, messages, tools)
                used = self.usage_log[-1]
                used["route"] = why
                if provider is self.primary and used["prompt_tokens"]:  # 글자 수 대비 토큰 수를 실제 값으로 맞춘다
                    self.chars_per_token = self._chars(messages, tools) / used["prompt_tokens"]
                elif why.startswith("요청 크기") and not self.switched_reason:
                    self.switched_reason = f"{why} → 큰 요청만 폴백 {self.fallback.model}로 보냄(작은 요청은 1순위 {self.primary.model})"
                return msg
            except RateLimited as e:
                can_fallback = provider is self.primary and self.fallback.ok
                if e.too_large and can_fallback:
                    self._learn_limit(e.detail, messages, tools)
                    continue  # 이제 route()가 이 요청을 폴백으로 보낸다
                if (e.wait > LONG_WAIT_SEC or attempt == max_retries) and can_fallback:
                    self.switched_reason = f"{self.primary.model}@{self.primary.base_url} 한도 초과({int(e.wait)}초 대기 요구) → 폴백 {self.fallback.model}"
                    self.active = self.fallback
                    attempt = 0  # 폴백은 재시도 횟수를 새로 받는다(무료 공용 풀은 잠깐 막히는 일이 잦다)
                    continue
                if e.too_large:
                    raise RuntimeError(f"LLM 호출 실패 (요청이 {provider.model}의 요청 크기 한도보다 큼): {e.detail[:400]}") from e
                if attempt < max_retries:
                    attempt += 1
                    time.sleep(min(e.wait, LONG_WAIT_SEC))
                    continue
                raise RuntimeError(f"LLM 호출 실패 (429, {provider.model}): {e.detail[:500]}") from e

    def _learn_limit(self, detail: str, messages: list[dict], tools: list[dict] | None) -> None:
        """413 본문의 'Limit 7000, Requested 7763'에서 1순위의 요청 크기 한도와 글자·토큰 비율을 배운다.
        배운 한도는 .env에 적어, 다음 실행부터는 큰 요청을 보내 보지 않고 바로 가른다."""
        limit, requested = (re.search(rf"{k}\D{{0,3}}(\d[\d,]*)", detail) for k in ("Limit", "Requested"))
        if requested:
            self.chars_per_token = self._chars(messages, tools) / int(requested.group(1).replace(",", ""))
        est = self.estimate_tokens(messages, tools)
        self.max_input = min(int(limit.group(1).replace(",", "")) if limit else est - 1, est - 1)  # 이 요청은 반드시 한도 밖이 되게
        try:
            save_env({"LLM_MAX_INPUT_TOKENS": str(self.max_input)})
        except OSError:
            pass

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
            if e.code == 413:
                raise RateLimited(0, detail, too_large=True) from e
            raise RuntimeError(f"LLM 호출 실패 ({e.code}): {detail[:500]}") from e
        if "choices" not in data:  # OpenRouter는 200으로 오류 본문을 주기도 한다
            raise RuntimeError(f"LLM 응답 형식 오류: {json.dumps(data, ensure_ascii=False)[:500]}")
        u = data.get("usage") or {}
        self.usage_log.append({"model": data.get("model") or p.model, "provider": p.name,
                           "prompt_tokens": u.get("prompt_tokens", 0), "completion_tokens": u.get("completion_tokens", 0)})
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
