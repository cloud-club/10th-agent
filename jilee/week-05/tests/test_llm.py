"""라우팅 규칙(요청 크기 · 한도 초과 전환)을 실제 호출 없이 검증한다."""

import unittest
from unittest import mock

from agent import llm as llm_mod
from agent.llm import LLMClient, RateLimited

ENV = {"LLM_BASE_URL": "https://primary.example/v1", "LLM_API_KEY": "k1", "LLM_MODEL": "small-fast",
       "LLM_FALLBACK_BASE_URL": "https://fallback.example/v1", "LLM_FALLBACK_API_KEY": "k2", "LLM_FALLBACK_MODEL": "big-slow",
       "LLM_MAX_INPUT_TOKENS": ""}
SMALL = [{"role": "user", "content": "안녕"}]
BIG = [{"role": "user", "content": "가" * 20000}]


class RoutingTest(unittest.TestCase):
    def setUp(self):
        self.saved = {}  # 실제 .env를 건드리지 않게 저장을 가로챈다
        self.patches = [mock.patch.dict("os.environ", ENV), mock.patch.object(llm_mod, "load_env", lambda *a: None),
                        mock.patch.object(llm_mod, "save_env", self.saved.update)]
        for p in self.patches:
            p.start()
        self.sent = []

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def client(self, behave):
        c = LLMClient()

        def fake_post(provider, messages, tools):
            self.sent.append(provider.name)
            out = behave(provider, messages)
            if isinstance(out, Exception):
                raise out
            c.usage_log.append({"model": provider.model, "provider": provider.name, "prompt_tokens": out, "completion_tokens": 1})
            return {"role": "assistant", "content": "ok"}

        c._post = fake_post
        return c

    def test_small_requests_stay_on_primary(self):
        c = self.client(lambda p, m: 10)
        c.chat(SMALL)
        self.assertEqual(self.sent, ["primary"])
        self.assertEqual(c.usage_log[-1]["route"], "기본")

    def test_too_large_is_learned_and_only_that_request_goes_to_fallback(self):
        too_big = RateLimited(0, 'Request too large ... Limit 7000, Requested 9000, please reduce', too_large=True)
        c = self.client(lambda p, m: too_big if p.name == "primary" and len(m[0]["content"]) > 1000 else 10)
        c.chat(BIG)
        self.assertEqual(self.sent, ["primary", "fallback"])          # 한 번 부딪혀 한도를 배우고 폴백으로
        self.assertEqual(c.max_input, 7000)
        self.assertEqual(self.saved, {"LLM_MAX_INPUT_TOKENS": "7000"})  # 다음 실행을 위해 적어 둔다
        self.assertIn("요청 크기", c.usage_log[-1]["route"])
        self.assertIn("큰 요청만 폴백", c.switched_reason)
        c.chat(BIG)
        self.assertEqual(self.sent[2:], ["fallback"])                  # 이제는 보내 보지 않고 바로 가른다
        c.chat(SMALL)
        self.assertEqual(self.sent[3:], ["primary"])                   # 작은 요청은 다시 1순위

    def test_known_limit_routes_without_a_wasted_call(self):
        with mock.patch.dict("os.environ", {"LLM_MAX_INPUT_TOKENS": "7000"}):
            c = self.client(lambda p, m: 10)
            c.chat(BIG)
        self.assertEqual(self.sent, ["fallback"])

    def test_long_rate_limit_switches_for_the_rest_of_the_run(self):
        c = self.client(lambda p, m: RateLimited(3600, "daily limit") if p.name == "primary" else 10)
        c.chat(SMALL)
        c.chat(SMALL)
        self.assertEqual(self.sent, ["primary", "fallback", "fallback"])
        self.assertEqual(c.usage_log[-1]["route"], "한도 초과 전환")

    def test_without_fallback_too_large_is_an_error(self):
        with mock.patch.dict("os.environ", {"LLM_FALLBACK_BASE_URL": ""}):
            c = self.client(lambda p, m: RateLimited(0, "Limit 7000, Requested 9000", too_large=True))
            with self.assertRaisesRegex(RuntimeError, "요청 크기 한도"):
                c.chat(BIG)


if __name__ == "__main__":
    unittest.main()
