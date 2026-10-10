"""회의 품질과 회의록 품질을 LLM 없이 코드로 잰다.

두 층으로 나눈다.
  회의 품질   : 입력의 품질. 회의 자체가 얼마나 쓸모 있었나(머리말·화자 구분·고객 발언 비중·새로 확보한 항목·다음 행동)
  회의록 품질 : 출력의 품질. 회의록이 녹취를 제대로 옮겼나(형식·숫자 대조·누락 후보·인물·논의 요지 구조·핵심 요약)

점수는 통과·거부를 가르지 않는다. 담당자가 회의록을 확정하기 전에 무엇을 다시 볼지 알려 주는 용도다.
저장 전에 막는 일은 memory.validate(형식)와 sheet.Grounder(근거 대조)가 맡는다.

검사 하나는 {"key", "label", "value", "ok", "detail"}이고, ok가 None이면 잴 수 없었다는 뜻이라 점수의 분모에서 뺀다.
"""

from __future__ import annotations

import re

from . import memory, retrieval, sheet, store

CUSTOMER_SHARE_MIN = 0.4   # 고객 측 발화가 이 비율 이상이면 '고객의 말을 들은 회의'로 본다
NUMBER_MATCH_MIN = 0.8     # 회의록 숫자 가운데 이 비율 이상이 녹취에서 확인되어야 한다
LONG_UTTERANCE = 40        # 이보다 긴 고객 발언만 누락 후보로 따진다(짧은 맞장구는 뺀다)
REFLECTED_MIN = 0.25       # 발언의 2글자 조각 가운데 이 비율 이상이 회의록에 있어야 반영된 것으로 본다

_HEAD_DATE = re.compile(r"^\[\d{4}-\d{2}-\d{2}")  # store.load_meeting이 읽는 머리말과 같은 뜻


# ---- 한글로 읽은 숫자 ----------------------------------------------------------
# 녹취는 음성인식 결과라 숫자가 "일억 이천 사백만", "영 점 칠"처럼 한글로 적힌다.
# 회의록의 아라비아 숫자와 맞춰 보려고 한글 숫자를 아라비아 숫자 후보로 바꾼다. 놓치는 표현이 있어도 된다(못 맞춘 것은 사람이 본다).

_DIG = {"영": 0, "공": 0, "일": 1, "이": 2, "삼": 3, "사": 4, "오": 5, "육": 6, "칠": 7, "팔": 8, "구": 9}
_SMALL = {"십": 10, "백": 100, "천": 1000}
_BIG = {"만": 10 ** 4, "억": 10 ** 8}
_SINO = "".join(_DIG) + "".join(_SMALL) + "".join(_BIG)
_RUN = re.compile(f"[{_SINO}]+(?: [{_SINO}]+)*")
_DECIMAL = re.compile(r"([영공일이삼사오육칠팔구십백천]+)\s*점\s*((?:[영공일이삼사오육칠팔구]\s?)+)")
_SECONDS = re.compile(r"([일이삼사오육칠팔구십]+)\s*초\s*([일이삼사오육칠팔구])(?!내)")  # "이 초 팔" = 2.8초
_NATIVE_TENS = {"열": 10, "스물": 20, "스무": 20, "서른": 30, "마흔": 40, "쉰": 50}
_NATIVE_ONES = {"하나": 1, "한": 1, "둘": 2, "두": 2, "셋": 3, "세": 3, "넷": 4, "네": 4,
                "다섯": 5, "여섯": 6, "일곱": 7, "여덟": 8, "아홉": 9}
_NATIVE = re.compile(f"({'|'.join(_NATIVE_TENS)})({'|'.join(_NATIVE_ONES)})?")
_MONTHS = {"시월": "10", "유월": "6"}


def _sino_all(run: str) -> list[tuple[int, list[int]]]:
    """한자어 숫자 글자열 → [(전체 값, 억·만 앞에 놓인 마디 값들), …]. '일억이천사백만' → [(124000000, [1, 2400])].

    숫자 글자 뒤에 단위 없이 또 숫자 글자가 오면 수가 끝난 것으로 본다. '이십구일이'는 29 · 1 · 2다
    ('일'은 날짜의 일, '이'는 조사일 수 있어 앞 수에 붙이면 값이 틀어진다).
    """
    out: list[tuple[int, list[int]]] = []
    total = section = num = 0
    segs: list[int] = []
    open_, after_digit = False, False
    for ch in run:
        if ch in _DIG:
            if after_digit:
                out.append((total + section + num, segs))
                total = section = 0
                segs = []
            num, after_digit = _DIG[ch], True
        elif ch in _SMALL:
            section += (num or 1) * _SMALL[ch]
            num, after_digit = 0, False
        else:  # 만 · 억
            section += num
            segs.append(section or 1)
            total += (section or 1) * _BIG[ch]
            section = num = 0
            after_digit = False
        open_ = True
    if open_:
        out.append((total + section + num, segs))
    return out


def _sino(run: str) -> tuple[int, list[int]]:
    """한 수로만 이루어진 글자열의 값."""
    return _sino_all(run)[0] if run else (0, [])


def korean_numbers(text: str) -> set[str]:
    """글 속의 한글 숫자를 아라비아 숫자 문자열 후보로 바꾼다.

    "일억 이천 사백만" → {"124000000", "1", "2400"} (회의록이 "1억 2,400만"으로 적으므로 마디 값도 함께 넣는다)
    "영 점 칠" → "0.7" · "이 초 팔" → "2.8" · "이십구일" → "29" · "열두 명" → "12" · "시월" → "10"
    """
    out: set[str] = set()
    for run in _RUN.findall(text):
        pieces = run.split(" ")
        # 띄어 쓴 숫자가 한 수인지 따로인지 알 수 없어, 이어 붙인 모든 묶음을 후보로 넣는다
        for i in range(len(pieces)):
            for j in range(i + 1, min(len(pieces), i + 6) + 1):
                for total, segs in _sino_all("".join(pieces[i:j])):
                    out.update(str(v) for v in [total, *segs])
    for whole, frac in _DECIMAL.findall(text):
        out.add(f"{_sino(whole)[0]}.{''.join(str(_DIG[c]) for c in frac if c in _DIG)}")
    for whole, tenth in _SECONDS.findall(text):
        out.add(f"{_sino(whole)[0]}.{_DIG[tenth]}")
    for tens, ones in _NATIVE.findall(text):
        out.add(str(_NATIVE_TENS[tens] + _NATIVE_ONES.get(ones, 0)))
    out.update(v for k, v in _MONTHS.items() if k in text)
    return out


def _arabic(text: str) -> list[str]:
    """글 속의 아라비아 숫자를 나온 순서대로. 쉼표를 빼고 앞의 0을 뗀다. "2026.10.8"처럼 점이 여럿이면 조각으로 나눈다."""
    out: list[str] = []
    for raw in re.findall(r"\d[\d,.]*", text):
        t = raw.replace(",", "").strip(".")
        parts = [t] if re.fullmatch(r"\d+(\.\d+)?", t) else t.split(".")
        for p in parts:
            if not p:
                continue
            whole, _, frac = p.partition(".")
            out.append(f"{int(whole)}.{frac}" if frac else str(int(whole)))
    return out


# ---- 공용 ---------------------------------------------------------------------

def _check(key: str, label: str, value, ok, detail: str = "") -> dict:
    return {"key": key, "label": label, "value": value, "ok": ok, "detail": detail}


def _scored(checks: list[dict]) -> dict:
    judged = [c for c in checks if c["ok"] is not None]
    score = round(sum(1 for c in judged if c["ok"]) / len(judged) * 100) if judged else 0
    return {"score": score, "checks": checks}


def _sides(transcript: str) -> dict[str, str]:
    """참석 줄의 (고객)·(당사) 구분 → {이름: 구분}. '참석: (고객) 정수연 품질팀장 / (당사) 김도현'"""
    row = next((l.split(":", 1)[1] for l in transcript.splitlines()[:3] if l.startswith("참석") and ":" in l), "")
    sides: dict[str, str] = {}
    for seg in row.split("/"):
        m = re.search(r"\((고객|당사)\)", seg)
        if not m:
            continue
        for part in re.split(r"[,·]", seg.replace(m.group(0), " ")):
            words = part.split()
            if words and re.fullmatch(r"[가-힣]{2,4}", words[0]):
                sides[words[0]] = m.group(1)
    return sides


# ---- 회의 품질 (입력) -----------------------------------------------------------

def meeting_quality(transcript: str, prev_sheet: dict, new_sheet: dict, deal: dict | None = None) -> dict:
    """회의 자체가 얼마나 쓸모 있었나. 녹취의 모양과, 이 회의로 결과지가 얼마나 채워졌는지를 본다."""
    lines = transcript.splitlines()[:3]
    has_date = any(_HEAD_DATE.match(l) for l in lines)
    has_attendees = any(l.startswith("참석") and ":" in l for l in lines)
    checks = [_check("header", "머리말", "일시·참석 있음" if has_date and has_attendees else "빠짐", has_date and has_attendees,
                     "" if has_date and has_attendees else "녹취 첫 줄에 [일시 / 방식 / 장소]와 '참석:' 줄이 "
                     + " · ".join(x for x, ok in (("일시", has_date), ("참석", has_attendees)) if not ok) + " 없이 올라왔다")]

    utts = store.utterances(transcript)
    speakers = sorted({u["speaker"] for u in utts})
    checks.append(_check("speakers", "화자 구분", f"발화 {len(utts)}개 · 화자 {len(speakers)}명", len(speakers) >= 2,
                         ", ".join(speakers) if speakers else "'이름: 발언' 형식의 줄이 없다"))

    sides = _sides(transcript)
    chars = {"고객": 0, "당사": 0}
    for u in utts:
        if sides.get(u["speaker"]) in chars:
            chars[sides[u["speaker"]]] += len(u["text"])
    known = chars["고객"] + chars["당사"]
    share = chars["고객"] / known if known else None
    checks.append(_check("customer_share", "고객 발언 비중", None if share is None else round(share * 100),
                         None if share is None else share >= CUSTOMER_SHARE_MIN,
                         "참석 줄에 (고객)·(당사) 구분이 없어 잴 수 없다" if share is None
                         else f"고객 측 {chars['고객']:,}자 · 당사 {chars['당사']:,}자"))

    # 이 회의로 새로 채워진 항목. 거래 조건에 따라 늘어난 확인 항목도 함께 센다
    codes = list(sheet.GATE) + [c for c in sheet.required_extras(deal) if c not in sheet.GATE]
    before, after = sheet.gate(prev_sheet)["by_code"], sheet.gate(new_sheet)["by_code"]
    name = lambda c: f"{c} {sheet.ITEMS[c][1]}"
    gained = [name(c) for c in codes if before[c] == sheet.MISSING and after[c] != sheet.MISSING]
    missing = [name(c) for c in codes if after[c] == sheet.MISSING]
    had_room = any(before[c] == sheet.MISSING for c in codes)
    checks.append(_check("gained", "새로 확보한 항목", len(gained), bool(gained) if had_room else None,
                         (f"확보: {', '.join(gained)}" if gained else "이 회의로 새로 채워진 항목이 없다")
                         + (f" · 아직 미확보: {', '.join(missing)}" if missing else " · 남은 미확보 없음")))

    questions = [q for q in new_sheet.get("next_questions") or [] if (q or "").strip()]
    checks.append(_check("next_action", "다음 행동", len(questions), (bool(questions) if missing else None),
                         "미확보 항목이 남았는데 다음 회의에서 물을 질문이 없다" if missing and not questions
                         else f"다음 회의 질문 {len(questions)}건" if missing else "남은 미확보 항목이 없다"))
    return _scored(checks)


# ---- 회의록 품질 (출력) ---------------------------------------------------------

def _number_text(markdown: str) -> str:
    """숫자 대조에서 볼 회의록 글. 절 번호·회차와, 지난 회의에서 넘어온 2절(액션아이템 점검)은 뺀다."""
    secs = memory.split_sections(markdown)
    text = markdown.replace(secs.get(2, "\x00"), "") if 2 in secs else markdown
    text = re.sub(r"^##\s*\d+\.", "##", text, flags=re.M)
    return re.sub(r"\d+\s*차", "", text)


def minutes_quality(markdown: str, transcript: str, known_names: list[str] | None = None) -> dict:
    """회의록이 녹취를 제대로 옮겼나. 지어낸 숫자·빠뜨린 고객 발언·읽기 어려운 구조를 찾는다."""
    known_names = known_names or []
    errs = memory.validate(markdown, transcript + " " + " ".join(known_names))
    checks = [_check("format", "형식", len(errs), not errs, " / ".join(errs))]

    # 숫자 대조: 회의록의 숫자가 녹취에 그대로, 또는 한글로 읽은 형태로 있는가
    heard = set(_arabic(transcript)) | korean_numbers(transcript)
    tokens = list(dict.fromkeys(t for t in _arabic(_number_text(markdown)) if len(t) > 1))
    unseen = [t for t in tokens if t not in heard]
    ratio = (len(tokens) - len(unseen)) / len(tokens) if tokens else None
    checks.append(_check("numbers", "숫자 대조", None if ratio is None else round(ratio * 100),
                         None if ratio is None else ratio >= NUMBER_MATCH_MIN,
                         "회의록에 대조할 숫자가 없다" if ratio is None
                         else f"{len(tokens)}개 가운데 {len(tokens) - len(unseen)}개 확인"
                         + (f" · 녹취에서 확인 못 함: {', '.join(unseen[:12])}" if unseen else "")))

    # 누락 후보: 길게 말한 고객 발언인데 회의록에 흔적이 거의 없는 것
    sides = _sides(transcript)
    utts = store.utterances(transcript)
    theirs = [u for u in utts if sides.get(u["speaker"]) == "고객"] if "고객" in sides.values() else utts
    targets = [u for u in theirs if len(u["text"]) >= LONG_UTTERANCE]
    written = set(retrieval.tokenize(markdown))
    dropped = []
    for u in targets:
        grams = set(retrieval.tokenize(u["text"]))
        if grams and len(grams & written) / len(grams) < REFLECTED_MIN:
            dropped.append(u)
    checks.append(_check("coverage", "고객 발언 반영", round((1 - len(dropped) / len(targets)) * 100) if targets else None,
                         (not dropped) if targets else None,
                         "따져 볼 만큼 긴 고객 발언이 없다" if not targets
                         else "회의록에 반영되지 않은 것으로 보이는 고객 발언: "
                         + " / ".join(f"#{u['pos']} {u['speaker']}: {u['text'][:60]}" for u in dropped[:5]) if dropped
                         else f"긴 고객 발언 {len(targets)}건이 모두 반영됐다"))

    secs = memory.split_sections(markdown)
    names = [r[1] for r in memory.parse_table(secs.get(1, ""))[1] if len(r) > 1]
    names += [n for r in memory.parse_table(secs.get(7, ""))[1] if len(r) > 1 for n in memory.person_names(r[1])]
    strangers = sorted({n for n in names if re.fullmatch(r"[가-힣]{2,4}", n) and n not in transcript and n not in known_names})
    checks.append(_check("people", "인물", len(strangers), not strangers,
                         f"녹취·담당자 정보에 없는 이름: {', '.join(strangers)}" if strangers else ""))

    topics = len(re.findall(r"^###\s+\S", secs.get(3, ""), re.M))
    marked = "[중요]" in secs.get(3, "")
    checks.append(_check("structure", "논의 요지 구조", f"주제 {topics}개 · [중요] {'있음' if marked else '없음'}", topics >= 2 and marked,
                         "" if topics >= 2 and marked else "3절을 주제 두 개 이상으로 묶고 결론을 가른 논의에 [중요]를 붙여야 요지가 드러난다"))

    gist = bool(re.search(r"^\s*[-*]\s*핵심 요약\s*[:：]\s*\S", markdown, re.M))
    checks.append(_check("gist", "핵심 요약", "있음" if gist else "없음", gist, "" if gist else "머리의 '- 핵심 요약:' 줄이 없다"))
    return _scored(checks)


# ---- 저장된 회의 한 건 ----------------------------------------------------------

def report(customer_id: str, project_id: str, meeting_no: int) -> dict:
    """저장된 녹취·회의록·결과지에서 읽어 두 품질을 함께 돌려준다. 회의록이 아직 없으면 minutes는 None."""
    d, n = store.project_dir(customer_id, project_id), int(meeting_no)
    tp = store.transcript_path(customer_id, project_id, n)
    if not tp.exists():
        raise ValueError(f"{n}차 녹취가 없어 품질을 잴 수 없습니다.")
    transcript = tp.read_text(encoding="utf-8")
    prev = memory.load_before(store.key(customer_id, project_id), d, n)["sheet"]
    # 에이전트가 만든 초안이 있으면 그것을, 없으면 확정된 것을 본다
    snap = memory._read_json(memory.draft_path(d, n)) or memory._read_json(memory.state_path(d, n)) or {}
    deal = store.load_project(customer_id, project_id).get("deal")
    mp = store.minutes_path(customer_id, project_id, n)
    return {"meeting": meeting_quality(transcript, prev, snap.get("sheet") or prev, deal),
            "minutes": minutes_quality(mp.read_text(encoding="utf-8"), transcript, store.known_names(customer_id, project_id))
            if mp.exists() else None}
