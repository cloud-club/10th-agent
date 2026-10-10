"""고객 홈페이지 한 장을 읽어 기본정보를 뽑는다(회의 전에 "이 회사가 무엇을 하는 곳인가"를 채우는 용도).

원칙
  - 실제로 가져온 것만 돌려준다. 못 가져오면 오류를 내고, 빈 칸을 지어내 채우지 않는다.
  - LLM이 뽑은 제품명과 사실은 페이지 글과 맞춰 보고, 페이지에 없는 것은 버린다(결과지의 근거 대조와 같은 뜻).
  - 한 번 부르면 그 주소의 페이지 한 장만 가져온다. 링크를 따라가며 긁지 않는다.

안전(서버가 내부망을 긁는 데 쓰이지 않게)
  - http/https, 포트 80·443·8080·8443만. 호스트를 풀어 사설·루프백·링크로컬·예약 대역이면 거부한다.
  - 리디렉션은 자동으로 따르지 않고 최대 3번까지 직접 따라가며, 갈 때마다 같은 검사를 한다.
  - robots.txt가 막은 경로는 가져오지 않는다. User-Agent에 봇임을 밝힌다.

공개 웹 페이지의 내용이라 LLM으로 보낼 때 마스킹하지 않는다(고객사명은 호출하는 쪽이 이미 알고 넘긴 값이다).
이 모듈은 파일을 쓰지 않는다. 저장은 호출하는 쪽이 한다.
"""

from __future__ import annotations

import ipaddress
import json
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from html.parser import HTMLParser

AGENT = "SalesAX-crawler"
USER_AGENT = f"{AGENT}/0.1 (bot; reads one page to confirm a company's public profile)"
ALLOWED_PORTS = {80, 443, 8080, 8443}
REDIRECTS = {301, 302, 303, 307, 308}
MAX_REDIRECTS = 3
MAX_TEXT = 6000     # 본문 글의 상한(LLM 입력 한도)
MAX_IMAGES = 6
SKIP_TAGS = {"script", "style", "noscript", "svg", "nav", "footer"}  # 이 안의 글은 본문으로 보지 않는다
BLOCKED = "이 사이트는 자동 수집을 허용하지 않습니다"


# ---- 안전 검사 -----------------------------------------------------------------

def _resolve(host: str) -> list[str]:
    """호스트가 가리키는 주소들. 숫자 주소면 그대로 돌려준다."""
    try:
        return [str(ipaddress.ip_address(host))]
    except ValueError:
        pass
    try:
        return sorted({info[4][0] for info in socket.getaddrinfo(host, None)})
    except socket.gaierror as e:
        raise ValueError(f"주소를 찾지 못했습니다: {host}") from e


def _check(url: str) -> urllib.parse.SplitResult:
    """가져와도 되는 주소인지 본다. 안 되면 ValueError."""
    u = urllib.parse.urlsplit(url)
    if u.scheme not in ("http", "https"):
        raise ValueError("http 또는 https 주소만 가져올 수 있습니다.")
    if not u.hostname:
        raise ValueError(f"주소가 올바르지 않습니다: {url}")
    try:
        port = u.port or (443 if u.scheme == "https" else 80)
    except ValueError as e:
        raise ValueError(f"주소가 올바르지 않습니다: {url}") from e
    if port not in ALLOWED_PORTS:
        raise ValueError(f"허용하지 않는 포트입니다: {port}")
    for ip in _resolve(u.hostname):
        if not ipaddress.ip_address(ip.split("%")[0]).is_global:  # 사설·루프백·링크로컬·예약·공유 대역
            raise ValueError("내부망이나 이 컴퓨터를 가리키는 주소는 가져오지 않습니다.")
    return u


# ---- 가져오기 ------------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # 리디렉션은 직접 따라가며 갈 곳을 검사한다
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def _open(url: str, timeout: int, max_bytes: int) -> tuple[int, dict, bytes]:
    """주소 하나를 연다. (상태 코드, 소문자 헤더, 본문 앞부분). 리디렉션은 따르지 않고 상태 코드로 돌려준다."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1"})
    try:
        with _opener.open(req, timeout=timeout) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read(max_bytes)
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, b""
    except (urllib.error.URLError, socket.timeout, TimeoutError) as e:
        raise ValueError(f"페이지에 닿지 못했습니다: {getattr(e, 'reason', e)}") from e


def _robots_allows(url: str, timeout: int) -> bool:
    """robots.txt가 이 경로를 막았는가. 파일이 없거나 못 읽으면 허용으로 본다."""
    u = urllib.parse.urlsplit(url)
    try:
        status, _, body = _open(f"{u.scheme}://{u.netloc}/robots.txt", timeout, 200_000)
    except ValueError:
        return True
    if status != 200 or not body:
        return True
    rp = urllib.robotparser.RobotFileParser()
    rp.parse(body.decode("utf-8", errors="ignore").splitlines())
    return rp.can_fetch(AGENT, url)


def _decode(body: bytes, content_type: str) -> str:
    """글자셋: 헤더 → meta charset → utf-8 → cp949 순으로 시도한다."""
    found = []
    m = re.search(r"charset=\s*[\"']?([\w.-]+)", content_type or "", re.I)
    if m:
        found.append(m.group(1))
    m = re.search(rb"charset=\s*[\"']?([\w.-]+)", body[:4096], re.I)
    if m:
        found.append(m.group(1).decode("ascii", errors="ignore"))
    for name in found + ["utf-8", "cp949"]:
        name = "cp949" if name.lower().replace("_", "-") in ("euc-kr", "ks-c-5601-1987", "x-windows-949") else name  # cp949가 euc-kr을 품는다
        try:
            return body.decode(name)
        except (UnicodeDecodeError, LookupError):
            continue
    return body.decode("utf-8", errors="replace")


class _Page(HTMLParser):
    """제목 · 설명 · 본문 글 · 이미지를 모은다."""

    def __init__(self, base: str):
        super().__init__(convert_charrefs=True)
        self.base, self.title, self.meta, self.text, self.images = base, [], {}, [], []
        self._skip, self._in_title = 0, False

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        if tag in SKIP_TAGS:
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag == "base" and a.get("href"):
            self.base = urllib.parse.urljoin(self.base, a["href"])
        elif tag == "meta":
            key = (a.get("name") or a.get("property") or "").lower()
            if key in ("description", "og:title", "og:description", "og:image") and a.get("content", "").strip():
                self.meta.setdefault(key, a["content"].strip())
        elif tag == "img" and not self._skip:
            self._image(a.get("src") or a.get("data-src") or "", a.get("alt", ""), a.get("width", ""), a.get("height", ""))
        if not self._skip:
            self.text.append(" ")  # 태그 경계에서 낱말이 붙지 않게

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag in SKIP_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title.append(data)
        elif not self._skip:
            self.text.append(data)

    def _image(self, src: str, alt: str, width: str = "", height: str = "") -> None:
        src = src.strip()
        tiny = any(re.fullmatch(r"[01](px)?", v.strip()) for v in (width, height) if v)  # 1px 추적 이미지
        if not src or src.startswith("data:") or tiny:
            return
        full = urllib.parse.urljoin(self.base, src)
        u = urllib.parse.urlsplit(full)
        if u.scheme not in ("http", "https") or u.path.lower().endswith(".svg"):  # svg는 대개 아이콘·로고 조각이다
            return
        if all(i["src"] != full for i in self.images):
            self.images.append({"src": full, "alt": " ".join(alt.split())})


def fetch(url: str, timeout: int = 10, max_bytes: int = 1_500_000) -> dict:
    """페이지 한 장을 가져와 제목 · 설명 · 본문 글 · 이미지를 돌려준다. 가져올 수 없으면 ValueError."""
    url = (url or "").strip()
    if "://" not in url:
        url = "https://" + url  # 'www.example.co.kr'처럼 적은 홈페이지 주소
    cur = url
    for _ in range(MAX_REDIRECTS + 1):
        _check(cur)
        if not _robots_allows(cur, timeout):
            raise ValueError(BLOCKED)
        status, headers, body = _open(cur, timeout, max_bytes)
        if status in REDIRECTS and headers.get("location"):
            cur = urllib.parse.urljoin(cur, headers["location"])  # 다음 바퀴에서 갈 곳을 다시 검사한다
            continue
        break
    else:
        raise ValueError("리디렉션이 너무 많습니다.")
    if status != 200:
        raise ValueError(f"페이지를 가져오지 못했습니다(HTTP {status}).")
    ctype = headers.get("content-type", "")
    if "html" not in ctype.lower():
        raise ValueError(f"웹 페이지가 아닙니다({ctype or '형식 없음'}).")

    page = _Page(cur)
    page.feed(_decode(body, ctype))
    page.close()
    squeeze = lambda s: " ".join(s.split())
    images = []
    if page.meta.get("og:image"):
        page_images, page.images = page.images, []
        page._image(page.meta["og:image"], page.meta.get("og:title", ""))  # 대표 이미지를 맨 앞에
        images, page.images = page.images, page_images
    images += [i for i in page.images if all(i["src"] != x["src"] for x in images)]
    return {"url": url, "final_url": cur,
            "title": squeeze("".join(page.title)) or page.meta.get("og:title", ""),
            "description": squeeze(page.meta.get("description") or page.meta.get("og:description", "")),
            "text": squeeze("".join(page.text))[:MAX_TEXT],
            "images": images[:MAX_IMAGES],
            "fetched_at": time.strftime("%Y-%m-%d %H:%M")}


# ---- 요약 ---------------------------------------------------------------------

SUMMARY_PROMPT = """당신은 영업 담당자를 위해 고객 회사의 홈페이지를 읽고 정리한다. 아래 페이지에 적힌 내용만 쓴다. 페이지에 없는 것은 추정하지 않는다.
다음 JSON만 답한다. 다른 글은 쓰지 않는다.

{"what": "무엇을 만들거나 하는 회사인지 두세 문장", "products": ["페이지에 적힌 제품·서비스 이름 그대로"], "facts": {"설립": "", "대표": "", "소재지": "", "인증": "", "주요 고객": ""}}

- products와 facts의 값은 페이지의 표기를 그대로 옮긴다(바꿔 쓰지 않는다).
- facts에는 페이지에 명시된 것만 넣는다. 없는 항목은 키를 아예 넣지 않는다.
- what은 한국어로 쓴다."""


def _json(text: str):
    """모델의 답에서 JSON 객체를 찾아 푼다. 못 풀면 None."""
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def _norm(s: str) -> str:
    return "".join(str(s).split()).lower()


def _in_page(value: str, hay: str) -> bool:
    """값이 페이지 글에 실제로 있는가. 'A, B'처럼 여럿을 묶은 값은 조각마다 본다."""
    v = _norm(value)
    if len(v) < 2:
        return False
    if v in hay:
        return True
    parts = [_norm(p) for p in re.split(r"[,·/、;]| 및 ", str(value)) if _norm(p)]
    return len(parts) > 1 and all(len(p) >= 2 and p in hay for p in parts)


def summarize(page: dict, company: str, llm=None) -> dict:
    """페이지 글에서 '무엇을 하는 회사인가' · 제품 · 명시된 사실을 뽑는다. 페이지에 없는 제품과 사실은 버린다."""
    out = {"what": "", "products": [], "facts": {}, "source": page.get("final_url") or page.get("url", "")}
    if llm is None:
        from .llm import LLMClient
        llm = LLMClient()
    user = (f"회사: {company}\n제목: {page.get('title', '')}\n설명: {page.get('description', '')}\n\n본문:\n{page.get('text', '')}")
    msg = llm.chat([{"role": "system", "content": SUMMARY_PROMPT}, {"role": "user", "content": user}])  # 공개 웹 페이지라 마스킹하지 않는다
    data = _json(msg.get("content") or "")
    if not isinstance(data, dict):
        out["what"] = page.get("description", "")  # 답을 못 풀면 페이지가 스스로 적은 설명만 쓴다
        return out
    hay = _norm(" ".join(str(page.get(k, "")) for k in ("title", "description", "text")))
    out["what"] = " ".join(str(data.get("what") or "").split()) or page.get("description", "")
    products = data.get("products") if isinstance(data.get("products"), list) else []
    out["products"] = list(dict.fromkeys(p.strip() for p in products if isinstance(p, str) and _in_page(p, hay)))
    facts = data.get("facts") if isinstance(data.get("facts"), dict) else {}
    out["facts"] = {str(k): v.strip() for k, v in facts.items() if isinstance(v, str) and v.strip() and _in_page(v, hay)}
    return out


def enrich(customer: dict, llm=None) -> dict:
    """고객 정보의 홈페이지 주소로 기본정보 초안을 만든다. 파일은 쓰지 않는다. 사람이 확인하기 전까지 초안이다."""
    homepage = (customer.get("homepage") or "").strip()
    if not homepage:
        raise ValueError("홈페이지 주소가 없습니다")
    page = fetch(homepage)
    s = summarize(page, customer.get("name", ""), llm)
    return {"what": s["what"], "products": s["products"], "facts": s["facts"], "images": page["images"],
            "title": page["title"], "source": s["source"], "fetched_at": page["fetched_at"], "status": "초안"}
