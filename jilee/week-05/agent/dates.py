"""말로 적힌 기한·시기를 날짜로 푼다(캘린더·타임라인에 올리기 위한 것).

회의록의 기한과 결과지의 계약 예상시기는 고객이 말한 그대로("다음 주 수요일", "10월 말") 저장된다.
원문은 그대로 두고, 화면에 올릴 날짜만 여기서 계산한다. 화면(static/index.html)의 parseDate·approxDate와 같은 규칙이다.

  정확(approx=False)  2026-10-08 · 2026.10.8 · 10월 8일 · 10/8 · 다음 주 수요일 · 내일 · 모레
  어림(approx=True)   이번 주·다음 주·그 다음 주(그 주 금요일) · N월 말(말일)·초(5일)·중순(15일)
                      · N월 첫째~넷째 주(그 주 금요일) · 이달 말

한 글에 둘이 섞여 있으면("다음 주 수요일(10월 22일)") 정확한 쪽을 쓴다. 못 읽으면 date가 None이다.
"""

from __future__ import annotations

import calendar
import re
from datetime import date, timedelta

_WEEKDAYS = "월화수목금토일"
_WEEKS = {"이번": 0, "다음": 1, "그다음": 2, "다다음": 2}
_ORDINAL = {"첫": 0, "둘": 1, "셋": 2, "세": 2, "넷": 3, "네": 3}


def _make(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:  # 2월 30일 같은 없는 날짜
        return None


def _in_year(base: date, month: int, day: int) -> date | None:
    """연도가 없는 날짜는 기준일의 해로 본다. 기준일보다 한 달 넘게 앞이면 해를 넘긴 것으로 본다."""
    d = _make(base.year, month, day)
    prev_month_first = (base.replace(day=1) - timedelta(days=1)).replace(day=1)
    return _make(base.year + 1, month, day) if d and d < prev_month_first else d


def _week_start(d: date) -> date:
    """그 주의 월요일. 주는 일요일에 시작하는 것으로 센다(화면의 달력과 같다). 일요일에 말한 '이번 주'는 다가오는 주다."""
    return d - timedelta(days=(d.weekday() + 1) % 7) + timedelta(days=1)


def _friday(d: date, weeks: int) -> date:
    return _week_start(d) + timedelta(days=4 + 7 * weeks)


def resolve(text: str, base: date) -> dict:
    """글에서 날짜를 읽는다. {"date": "YYYY-MM-DD" | None, "approx": 어림인가, "raw": 원문}"""
    raw = text or ""

    def out(d: date | None, approx: bool) -> dict:
        return {"date": d.isoformat() if d else None, "approx": bool(d) and approx, "raw": raw}

    # ---- 정확한 표현 ----
    m = re.search(r"(\d{4})[-./]\s*(\d{1,2})[-./]\s*(\d{1,2})(?!\d|st|nd|rd|th)", raw)
    if m and (d := _make(int(m.group(1)), int(m.group(2)), int(m.group(3)))):
        return out(d, False)
    m = re.search(r"(\d{1,2})월\s*(\d{1,2})일", raw)
    if m and (d := _in_year(base, int(m.group(1)), int(m.group(2)))):
        return out(d, False)
    m = re.search(r"(그\s*다음|다다음|다음|이번)\s*주\s*([월화수목금토일])요일", raw)
    if m:
        weeks = _WEEKS[re.sub(r"\s", "", m.group(1))]
        return out(_week_start(base) + timedelta(days=_WEEKDAYS.index(m.group(2)) + 7 * weeks), False)
    if "모레" in raw:
        return out(base + timedelta(days=2), False)
    if "내일" in raw:
        return out(base + timedelta(days=1), False)
    m = re.search(r"(?<![\d/])(\d{1,2})/(\d{1,2})(?![\d/])", raw)
    if m and 1 <= int(m.group(1)) <= 12 and (d := _in_year(base, int(m.group(1)), int(m.group(2)))):
        return out(d, False)

    # ---- 어림 ----
    m = re.search(r"(\d{4})[-./]\s*(\d{1,2})[-./\s]\s*([1-5])(?:st|nd|rd|th)\s*week", raw, re.I)  # 모델이 '2026-12-3rd week'처럼 적은 것
    if m and (first := _make(int(m.group(1)), int(m.group(2)), 1)):
        return out(_friday(first, int(m.group(3)) - 1), True)
    m = re.search(r"(\d{1,2})월\s*(첫|둘|셋|넷|세|네)\S*\s*주", raw)
    if m and (first := _in_year(base, int(m.group(1)), 1)):
        return out(_friday(first, _ORDINAL[m.group(2)]), True)
    m = re.search(r"(\d{1,2})월\s*(말|초|중순)", raw)
    if m and (first := _in_year(base, int(m.group(1)), 1)):
        day = {"말": calendar.monthrange(first.year, first.month)[1], "초": 5, "중순": 15}[m.group(2)]
        return out(first.replace(day=day), True)
    if re.search(r"(이달|이번\s*달)\s*말", raw):
        return out(base.replace(day=calendar.monthrange(base.year, base.month)[1]), True)
    m = re.search(r"(그\s*다음|다다음|다음|이번)\s*주", raw)
    if m:
        return out(_friday(base, _WEEKS[re.sub(r"\s", "", m.group(1))]), True)
    return out(None, False)
