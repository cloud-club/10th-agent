"""고객 홈페이지 읽기(안전 검사 · robots · 글자셋 · 본문과 이미지 추출 · 요약의 근거 대조)를 네트워크와 LLM 없이 검증한다."""

import json
import unittest
from unittest import mock

from agent import crawl

PUBLIC, PRIVATE = "93.184.216.34", "10.0.0.5"
HTML = {"content-type": "text/html; charset=utf-8"}

PAGE = """<html><head><title> 대성정밀 | 정밀 부품 가공 </title>
<meta name="description" content="자동차 변속기 부품을 깎는 회사입니다.">
<meta property="og:image" content="/img/main.jpg"><meta property="og:title" content="대성정밀">
<script>var secret = "스크립트 글";</script><style>.x{}</style></head>
<body><nav>메뉴 회사소개 오시는길</nav>
<h1>싱크로나이저 링과 기어 샤프트</h1>
<p>대성정밀은 1998년 설립되어 경기도 화성에서 싱크로나이저 링, 기어 샤프트를 생산합니다. IATF 16949 인증.</p>
<img src="products/ring.png" alt="싱크로나이저   링"><img src="//cdn.example.com/shaft.jpg" alt="기어 샤프트">
<img src="/t.gif" width="1" height="1"><img src="data:image/png;base64,AAAA"><img src="/logo.svg" alt="로고">
<noscript>자바스크립트를 켜세요</noscript><footer>사업자등록번호 000-00-00000</footer></body></html>"""


class Net:
    """가짜 네트워크: 주소 → (상태, 헤더, 본문). robots.txt는 따로 정하지 않으면 404."""

    def __init__(self, pages, hosts=None):
        self.pages, self.hosts, self.opened = pages, hosts or {}, []

    def open(self, url, timeout, max_bytes):
        self.opened.append(url)
        if url in self.pages:
            status, headers, body = self.pages[url]
            return status, headers, (body.encode("utf-8") if isinstance(body, str) else body)[:max_bytes]
        return 404, {}, b""

    def getaddrinfo(self, host, *args, **kwargs):  # 이름 풀이만 가짜로. 숫자 주소는 crawl._resolve가 직접 본다
        return [(None, None, None, "", (self.hosts.get(host, PUBLIC), 0))]

    def __enter__(self):
        self.patches = [mock.patch.object(crawl, "_open", self.open), mock.patch.object(crawl.socket, "getaddrinfo", self.getaddrinfo)]
        for p in self.patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self.patches:
            p.stop()


class SafetyTest(unittest.TestCase):
    def test_only_public_web_addresses_are_fetched(self):
        with Net({}, {"intra.example": PRIVATE, "localhost": "127.0.0.1"}) as net:
            for bad in ("file:///etc/passwd", "ftp://a.example/x", "http://intra.example/", "http://localhost/",
                        "http://192.168.0.10/", "http://169.254.169.254/latest/", "http://[::1]/", "https://a.example:22/", "https:///nohost"):
                with self.assertRaises(ValueError, msg=bad):
                    crawl.fetch(bad)
            self.assertEqual(net.opened, [])  # 검사에서 걸러져 아무 데도 요청하지 않았다

    def test_redirect_to_private_address_is_refused(self):
        with Net({"https://a.example/": (302, {"location": "http://intra.example/admin"}, "")}, {"intra.example": PRIVATE}) as net:
            with self.assertRaisesRegex(ValueError, "내부망"):
                crawl.fetch("https://a.example/")
            self.assertFalse(any("intra.example" in u for u in net.opened))

    def test_redirects_are_followed_with_a_limit(self):
        pages = {"https://a.example/": (301, {"location": "/ko/"}, ""), "https://a.example/ko/": (200, HTML, PAGE)}
        with Net(pages):
            self.assertEqual(crawl.fetch("a.example/")["final_url"], "https://a.example/ko/")  # 주소에 https://가 없으면 붙인다
        loop = {f"https://a.example/{i}": (302, {"location": f"/{i + 1}"}, "") for i in range(6)}
        with Net(loop):
            with self.assertRaisesRegex(ValueError, "리디렉션"):
                crawl.fetch("https://a.example/0")

    def test_robots_txt_is_respected(self):
        robots = lambda body: {"https://a.example/robots.txt": (200, {"content-type": "text/plain"}, body), "https://a.example/": (200, HTML, PAGE)}
        with Net(robots("User-agent: *\nDisallow: /")) as net:
            with self.assertRaisesRegex(ValueError, "자동 수집을 허용하지 않습니다"):
                crawl.fetch("https://a.example/")
            self.assertNotIn("https://a.example/", net.opened)
        with Net(robots("User-agent: *\nDisallow: /\n\nUser-agent: SalesAX-crawler\nAllow: /")):
            self.assertTrue(crawl.fetch("https://a.example/")["title"])  # 우리 봇 이름으로 연 곳은 가져온다
        with Net(robots("User-agent: *\nDisallow: /admin")):
            self.assertTrue(crawl.fetch("https://a.example/")["title"])

    def test_non_html_and_errors_are_refused(self):
        with Net({"https://a.example/a.pdf": (200, {"content-type": "application/pdf"}, "%PDF")}):
            with self.assertRaisesRegex(ValueError, "웹 페이지가 아닙니다"):
                crawl.fetch("https://a.example/a.pdf")
            with self.assertRaisesRegex(ValueError, "HTTP 404"):
                crawl.fetch("https://a.example/none")


class ExtractTest(unittest.TestCase):
    def test_title_description_text_and_images(self):
        with Net({"https://a.example/ko/": (200, HTML, PAGE)}):
            p = crawl.fetch("https://a.example/ko/")
        self.assertEqual((p["title"], p["description"]), ("대성정밀 | 정밀 부품 가공", "자동차 변속기 부품을 깎는 회사입니다."))
        self.assertIn("싱크로나이저 링, 기어 샤프트를 생산합니다", p["text"])
        for gone in ("스크립트 글", "메뉴 회사소개", "사업자등록번호", "자바스크립트를 켜세요"):
            self.assertNotIn(gone, p["text"])  # script · nav · footer · noscript의 글은 본문이 아니다
        self.assertEqual([i["src"] for i in p["images"]], ["https://a.example/img/main.jpg", "https://a.example/ko/products/ring.png", "https://cdn.example.com/shaft.jpg"])
        self.assertEqual(p["images"][1]["alt"], "싱크로나이저 링")  # 1px 추적 이미지 · data: · svg는 빠졌다
        self.assertRegex(p["fetched_at"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")

    def test_limits(self):
        body = "<html><body>" + "<p>" + "가나다라 " * 3000 + "</p>" + "".join(f'<img src="/p{i}.jpg">' for i in range(20)) + "</body></html>"
        with Net({"https://a.example/": (200, HTML, body)}):
            p = crawl.fetch("https://a.example/")
        self.assertEqual((len(p["text"]), len(p["images"])), (crawl.MAX_TEXT, crawl.MAX_IMAGES))

    def test_cp949_page_is_decoded(self):
        body = '<html><head><meta http-equiv="Content-Type" content="text/html; charset=euc-kr"><title>한빛정밀</title></head><body>다이캐스팅 부품</body></html>'.encode("cp949")
        with Net({"https://a.example/": (200, {"content-type": "text/html"}, body)}):
            p = crawl.fetch("https://a.example/")
        self.assertEqual((p["title"], p["text"]), ("한빛정밀", "다이캐스팅 부품"))
        with Net({"https://a.example/": (200, {"content-type": "text/html"}, "<title>표기 없는 완성형</title>".encode("cp949"))}):
            self.assertEqual(crawl.fetch("https://a.example/")["title"], "표기 없는 완성형")  # 표기가 없어도 utf-8 → cp949 순으로 시도


class FakeLLM:
    def __init__(self, content):
        self.content, self.seen = content, []

    def chat(self, messages, tools=None):
        self.seen.append(messages)
        return {"role": "assistant", "content": self.content}


class SummarizeTest(unittest.TestCase):
    def setUp(self):
        with Net({"https://a.example/": (200, HTML, PAGE)}):
            self.page = crawl.fetch("https://a.example/")

    def test_values_not_on_the_page_are_dropped(self):
        answer = {"what": "자동차 변속기 부품을 가공하는 회사다.", "products": ["싱크로나이저 링", "기어 샤프트", "전기차 모터 하우징"],
                  "facts": {"설립": "1998년", "소재지": "경기도 화성", "인증": "IATF 16949, ISO 9001", "대표": "김대성", "주요 고객": ""}}
        llm = FakeLLM("```json\n" + json.dumps(answer, ensure_ascii=False) + "\n```")
        s = crawl.summarize(self.page, "대성정밀", llm)
        self.assertEqual(s["products"], ["싱크로나이저 링", "기어 샤프트"])              # 페이지에 없는 제품은 버린다
        self.assertEqual(s["facts"], {"설립": "1998년", "소재지": "경기도 화성"})        # 페이지에 없는 대표 · ISO 9001이 낀 인증은 버린다
        self.assertEqual((s["what"], s["source"]), ("자동차 변속기 부품을 가공하는 회사다.", "https://a.example/"))
        self.assertIn("대성정밀", llm.seen[0][1]["content"])

    def test_unparseable_answer_falls_back_to_the_page_description(self):
        s = crawl.summarize(self.page, "대성정밀", FakeLLM("죄송하지만 정리할 수 없습니다."))
        self.assertEqual(s, {"what": "자동차 변속기 부품을 깎는 회사입니다.", "products": [], "facts": {}, "source": "https://a.example/"})

    def test_enrich(self):
        with self.assertRaisesRegex(ValueError, "홈페이지 주소가 없습니다"):
            crawl.enrich({"name": "한빛정밀(주)"})
        llm = FakeLLM(json.dumps({"what": "부품 가공 회사", "products": ["기어 샤프트"], "facts": {}}, ensure_ascii=False))
        with Net({"https://a.example/": (200, HTML, PAGE)}):
            out = crawl.enrich({"name": "대성정밀", "homepage": "https://a.example/"}, llm)
        self.assertEqual((out["status"], out["products"], out["title"], out["source"]), ("초안", ["기어 샤프트"], "대성정밀 | 정밀 부품 가공", "https://a.example/"))
        self.assertEqual(len(out["images"]), 3)
        self.assertEqual(set(out), {"what", "products", "facts", "images", "title", "source", "fetched_at", "status"})


if __name__ == "__main__":
    unittest.main()
