"""문서에 넣는 참고 사진.

고객사 사진이 없을 때 솔루션과 현장을 이해하도록 돕는 보조 자료다. 고객사의 사진이 아니므로 문서에는
"참고 사진"이라고 적고 출처 · 저작자 · 라이선스를 함께 보인다. 고르는 것은 사람이다.

  search(query)   위키미디어 공용(자유 라이선스 사진)에서 찾는다. 키가 필요 없다
  from_page(url)  사람이 준 주소의 페이지에 실린 사진을 가져온다(crawl.fetch의 안전 장치를 그대로 쓴다)
  clean(rows)     저장하기 전에 모양을 맞춘다
  news(company)   회사 이름으로 뉴스 기사 제목 · 언론사 · 날짜 · 주소를 가져온다(구글 뉴스 RSS)
  for_industry(text)  업종 글에서 낱말을 뽑아 참고 사진을 찾는다

회사 이름으로 찾은 기사는 이름이 같은 다른 회사의 것일 수 있다. 문서에는 "확인 필요"로 적고 사람이 지울 수 있게 한다.
"""

from __future__ import annotations

import html
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

from . import crawl

API = "https://commons.wikimedia.org/w/api.php"
USER_AGENT = "SalesAX-meeting-agent/0.5 (study project; reference photo lookup)"
MAX_PHOTOS = 6          # 한 프로젝트에 두는 참고 사진 수
PHOTO_MIME = ("image/jpeg", "image/png")
FIELDS = ("thumb", "url", "title", "source", "license", "credit", "origin", "date", "kind", "query")


def _text(value: str) -> str:
    """메타데이터에 섞인 HTML을 글로."""
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", value or "")).split())


def parse(data: dict) -> list[dict]:
    """위키미디어 응답 → 사진 목록. 사진(JPEG · PNG)만, 찾은 순서대로."""
    pages = sorted((data.get("query") or {}).get("pages", {}).values(), key=lambda p: p.get("index", 0))
    out = []
    for p in pages:
        info = (p.get("imageinfo") or [{}])[0]
        if info.get("mime") not in PHOTO_MIME or not info.get("thumburl"):
            continue
        meta = info.get("extmetadata") or {}
        out.append({"thumb": info["thumburl"], "url": info.get("url", ""), "title": p.get("title", "").removeprefix("File:").rsplit(".", 1)[0],
                    "source": info.get("descriptionurl", ""), "license": _text((meta.get("LicenseShortName") or {}).get("value", "")),
                    "credit": _text((meta.get("Artist") or {}).get("value", ""))[:80], "origin": "Wikimedia Commons",
                    "date": (info.get("timestamp") or "")[:10], "kind": "직접", "query": ""})
    return out


_cache: dict[str, list[dict]] = {}
_last = [0.0]


def search(query: str, limit: int = 12, timeout: int = 10) -> list[dict]:
    """위키미디어 공용에서 사진을 찾는다. 요청 사이를 1초 띄우고, 너무 잦다는 답(429)이면 잠시 쉬었다 한 번 더 한다."""
    query = " ".join((query or "").split())
    if not query:
        raise ValueError("찾을 말을 넣으세요.")
    if query in _cache:
        return _cache[query][:limit]
    params = {"action": "query", "generator": "search", "gsrsearch": query + " filetype:bitmap", "gsrnamespace": 6, "gsrlimit": 20,
              "prop": "imageinfo", "iiprop": "url|extmetadata|mime|timestamp", "iiurlwidth": 480, "format": "json"}
    req = urllib.request.Request(API + "?" + urllib.parse.urlencode(params), headers={"User-Agent": USER_AGENT})
    for attempt in (0, 1):
        time.sleep(max(0.0, 1.0 - (time.time() - _last[0])))
        _last[0] = time.time()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                _cache[query] = parse(json.loads(r.read().decode("utf-8")))
                return _cache[query][:limit]
        except urllib.error.HTTPError as e:
            if e.code != 429 or attempt:
                raise
            time.sleep(6)
    return []


def search_loose(query: str, limit: int = 8) -> list[dict]:
    """긴 검색어로 안 나오면 낱말을 줄여 다시 찾는다(앞 두 낱말 → 뒤 두 낱말)."""
    words = query.split()
    for q in dict.fromkeys([query, " ".join(words[:2]), " ".join(words[-2:])]):
        found = search(q, limit) if q else []
        if found:
            return found
    return []


def from_page(url: str) -> list[dict]:
    page = crawl.fetch(url)
    src = page.get("final_url") or url
    host = urllib.parse.urlsplit(src).netloc
    return [{"thumb": u, "url": u, "title": page.get("title", "")[:60], "source": src, "license": "", "credit": host, "origin": host}
            for u in map(_src, page.get("images", [])) if u]


def clean(rows, who: str = "담당자") -> list[dict]:
    """화면에서 고른 사진 목록을 저장할 모양으로. 주소가 http(s)가 아니면 받지 않는다."""
    out = []
    for r in (rows or [])[:MAX_PHOTOS]:
        row = {f: str(r.get(f, "")).strip()[:500] for f in FIELDS}
        if not all(re.match(r"https?://", row[f]) for f in ("thumb", "source")):
            raise ValueError("사진 주소와 출처 주소가 있어야 합니다.")
        out.append({**row, "added_by": r.get("added_by") or who, "added_at": r.get("added_at") or time.strftime("%Y-%m-%d %H:%M")})
    return out


NEWS = "https://news.google.com/rss/search"
BING = "https://www.bing.com/news/search"
MAX_NEWS = 8
RECENT_DAYS = 365      # 자동으로 가져오는 기사 · 사진은 최근 1년 것만


def _recent(date: str, days: int = RECENT_DAYS) -> bool:
    try:
        return (time.time() - time.mktime(time.strptime(date, "%Y-%m-%d"))) <= days * 86400
    except (TypeError, ValueError):
        return False     # 날짜를 모르는 것은 최근 것으로 치지 않는다


def _date(text: str) -> str:
    try:
        return parsedate_to_datetime(text or "").strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return ""


def parse_news(xml_text: str) -> list[dict]:
    """구글 뉴스 RSS → 기사 목록. 제목 끝의 " - 언론사"는 떼어 출처 칸으로 옮긴다."""
    out = []
    for item in ET.fromstring(xml_text).iter("item"):
        title, link = (item.findtext("title") or "").strip(), (item.findtext("link") or "").strip()
        source = (item.findtext("source") or "").strip()
        if not title or not re.match(r"https?://", link):
            continue
        if source and title.endswith(" - " + source):
            title = title[: -len(source) - 3]
        out.append({"title": title, "link": link, "source": source, "date": _date(item.findtext("pubDate")), "image": ""})
    return sorted(out, key=lambda r: r["date"], reverse=True)


def parse_bing(xml_text: str) -> list[dict]:
    """빙 뉴스 RSS → 기사 목록. 기사 주소는 추적 주소에서 원래 주소를 꺼내고, 기사 사진이 있으면 함께 담는다."""
    ns = {"n": "https://www.bing.com/news/search?q=&format=rss"}
    out = []
    for item in ET.fromstring(xml_text).iter("item"):
        title, link = (item.findtext("title") or "").strip(), (item.findtext("link") or "").strip()
        real = urllib.parse.parse_qs(urllib.parse.urlsplit(link).query).get("url", [link])[0]
        image = source = ""
        for child in item:
            tag = child.tag.rsplit("}", 1)[-1]
            if tag == "Image":
                image = (child.text or "").strip()
            elif tag == "Source":
                source = (child.text or "").strip()
        if not title or not re.match(r"https?://", real):
            continue
        if image and "w=" not in image:
            image += "&w=480"
        out.append({"title": title, "link": real, "source": source, "date": _date(item.findtext("pubDate")), "image": image.replace("http://", "https://", 1)})
    return sorted(out, key=lambda r: r["date"], reverse=True)


def _get(url: str, params: dict, timeout: int = 10) -> str:
    req = urllib.request.Request(url + "?" + urllib.parse.urlencode(params), headers={"User-Agent": "Mozilla/5.0 (compatible; " + USER_AGENT + ")"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def bing_news(query: str) -> list[dict]:
    return parse_bing(_get(BING, {"q": query, "format": "rss", "setlang": "ko", "cc": "KR"}))


def news(company: str, hint: str = "") -> list[dict]:
    """회사 이름으로 기사를 찾는다. 제목에 회사 이름이 있고 최근 1년 안의 것만 남긴다."""
    name = re.sub(r"\(주\)|주식회사|㈜", "", company or "").strip()
    if not name:
        raise ValueError("회사 이름이 없습니다.")
    rows, errors = [], []
    for fetch in (lambda: bing_news(f'"{name}"'),
                  lambda: parse_news(_get(NEWS, {"q": f'"{name}"' + (f" {hint}" if hint else ""), "hl": "ko", "gl": "KR", "ceid": "KR:ko"}))):
        try:
            rows += fetch()
        except Exception as e:  # 한 곳이 막혀도 다른 곳의 결과는 쓴다
            errors.append(str(e))
    if not rows and errors:
        raise RuntimeError("기사를 가져오지 못했습니다: " + errors[0])
    seen, out = set(), []
    for r in sorted(rows, key=lambda r: (r["date"], bool(r["image"])), reverse=True):
        key = re.sub(r"\W", "", r["title"])[:24]
        if name.replace(" ", "") in r["title"].replace(" ", "") and _recent(r["date"]) and key not in seen:
            seen.add(key)
            out.append(r)
    return out[:MAX_NEWS]


def _photo(r: dict, kind: str, query: str = "") -> dict:
    return {"thumb": r["image"], "url": r["image"], "title": r["title"], "source": r["link"], "license": "", "credit": r["source"],
            "origin": r["source"] or urllib.parse.urlsplit(r["link"]).netloc, "date": r["date"], "kind": kind, "query": query}


def _src(image) -> str:
    """crawl.fetch가 주는 사진은 {"src", "alt"} 꼴이다."""
    return (image.get("src", "") if isinstance(image, dict) else str(image or "")).strip()


def company_photos(items: list[dict], used: set[str] | None = None, limit: int = 3) -> list[dict]:
    """회사 기사(최근 1년)에 실린 사진. 기사 페이지의 대표 사진을 읽어 오고, 못 읽으면 검색 결과의 작은 사진을 쓴다."""
    used, out = set(used or ()), []
    for r in items:
        if len(out) >= limit:
            break
        if not _recent(r.get("date", "")):
            continue
        image = ""
        try:  # 기사 페이지의 대표 사진(og:image)이 검색 결과의 축소판보다 크고 선명하다
            image = _src((crawl.fetch(r["link"]).get("images") or [""])[0])
        except Exception:
            pass
        image = image or r.get("image", "")
        if image and image not in used:
            used.add(image)
            out.append(_photo({**r, "image": image}, "기사"))
    return out


def homepage_photos(web: dict | None, limit: int = 3) -> list[dict]:
    """회사 홈페이지에서 가져온 사진."""
    web = web or {}
    return [{"thumb": u, "url": u, "title": web.get("title", ""), "source": web.get("source", ""), "license": "", "credit": "회사 홈페이지",
             "origin": "회사 홈페이지", "date": (web.get("fetched_at") or "")[:10], "kind": "홈페이지", "query": ""}
            for u in map(_src, (web.get("images") or [])[:limit]) if u and web.get("source")]


KEYWORD_PROMPT = """회의 내용을 읽고, 그 회의를 이해하는 데 도움이 될 사진이 실린 기사를 찾을 한국어 검색어를 고른다.
- "이번 회의의 주제"에 적힌 것 가운데 사진으로 보일 수 있는 사물 · 설비 · 공정 · 기술을 고른다. 회의마다 주제가 다르므로 그 회의에만 있는 것을 먼저 고른다.
- 금액 · 일정 · 계약 조건 · 사람은 사진으로 찾을 것이 아니다. 그런 것만 다룬 회의면 고객의 제품과 공정에서 고른다.
- 회사 이름은 넣지 않는다. 검색어 하나는 뉴스 제목에 나올 법한 2낱말(길어도 3낱말). 서로 다른 것으로 5개.
답은 JSON 배열 하나만."""


def keywords(text: str, llm=None) -> list[str]:
    """회의 내용 → 사진을 찾을 검색어. 모델이 답을 못 하면 빈 목록."""
    if llm is None:
        from .llm import LLMClient
        llm = LLMClient()
    msg = llm.chat([{"role": "system", "content": KEYWORD_PROMPT}, {"role": "user", "content": text[:3000]}])
    m = re.search(r"\[.*?\]", msg.get("content") or "", re.S)
    try:
        rows = json.loads(m.group(0)) if m else []
    except ValueError:
        rows = []
    return [" ".join(str(q).split())[:40] for q in rows if isinstance(q, str) and len(str(q).strip()) >= 3][:5]


def relevant(row: dict, query: str) -> bool:
    """제목에 검색어의 낱말이 둘 이상(낱말이 하나뿐이면 그 하나) 들어 있어야 관련 있는 것으로 본다."""
    title = row.get("title", "").lower().replace(" ", "")
    words = [w[:5] if re.match(r"[a-z]", w) else w[:3] for w in query.lower().split() if len(w) >= 2]
    return bool(words) and sum(1 for w in words if w in title) >= min(2, len(words))


STOP = {"자동차", "부품", "제조", "주력", "품목", "대부분", "불량", "유출", "생산", "제품", "업체", "회사", "그중"}


def anchors(*texts: str) -> list[str]:
    """고객의 업종 · 생산품 글에서 뽑은 제품 낱말(예: 알루미늄, 다이캐스팅, 하우징). 자동으로 넣는 사진은 제목에 이 가운데 하나가 있어야 한다."""
    words = []
    for w in re.findall(r"[가-힣]{3,}", " ".join(t or "" for t in texts)):
        w = re.sub(r"(류|들|으로|에서)$", "", w)
        if len(w) >= 3 and w not in STOP and w not in words:
            words.append(w)
    return words


def news_photos(query: str) -> list[dict]:
    """검색어로 찾은 최근 1년 기사 가운데 사진이 실린 것."""
    return [_photo(r, "주제", query) for r in bing_news(query) if r["image"] and _recent(r["date"])]


def for_text(text: str, used: set[str] | None = None, llm=None, limit: int = 3, must: list[str] | None = None) -> tuple[list[dict], list[str]]:
    """글의 내용으로 사진을 찾는다: 검색어로 최근 1년 기사를 찾아, 제목에 고객의 제품 낱말(must)이 있고 사진이 실린 것만.

    맞는 것이 없으면 넣지 않는다. 엉뚱한 사진을 넣느니 비워 두고 사람이 고르게 한다.
    """
    used, out = set(used or ()), []
    qs = keywords(text, llm)
    for q in qs:
        if len(out) >= limit:
            break
        try:
            rows = news_photos(q)
        except Exception:
            continue
        for r in rows:
            title = r["title"].replace(" ", "")
            if r["thumb"] not in used and (not must or any(m in title for m in must)) and relevant(r, q):
                used.add(r["thumb"])
                out.append(r)
                break
        time.sleep(0.5)
    return out, qs
