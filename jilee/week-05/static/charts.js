/* 문서(회의록 · 결과지) 안에 넣는 SVG 도식.
 *
 * 모든 함수는 SVG 문자열을 돌려주는 순수 함수다(DOM을 만지지 않는다). 받은 데이터만 그리고,
 * 값이 없으면 빈 상태를 그린다. 보기 좋으라고 값을 지어내지 않는다.
 * 워드로 쓴 사내 문서 안에 들어가므로 검정·회색에 강조색(주황) 하나만 쓰고, 인쇄해도 읽히도록
 * 색만으로 구분하지 않는다(확정 = 채운 칸, AI 초안 = 빗금, 미확보 = 점선 빈 칸).
 * 스타일은 요소의 속성으로 직접 넣는다. 문서를 파일로 내보낼 때 도식이 그대로 따라간다.
 *
 *   SalesCharts.completeness({ total, confirmed, filled, items })   결과지 완결성
 *   SalesCharts.trend(timeline)                                     회차별 채움 추이
 *   SalesCharts.orgMap({ customer, people, team })                  의사결정 조직도
 *   SalesCharts.schedule({ today, rows })                           일정 막대
 *   SalesCharts.dueline({ meeting, items })                         회의일과 기한의 한 줄 타임라인
 *   SalesCharts.share(rows)                                         발언 비중
 */
(function () {
  'use strict';

  var FONT = 'Malgun Gothic, 맑은 고딕, sans-serif';
  var INK = '#161a20', ORANGE = '#f06d08', PALE = '#fff4e8', GRAY = '#6b7280', LINE = '#9aa0a8', LIGHT = '#d5d9df', BAND = '#f1f3f6';
  var W = 720;  // 종이 안쪽 폭(794px 종이 - 여백)과 같게 잡아 글자가 문서 본문과 같은 크기로 보인다
  var ROLES = ['의사결정자', '영향자', '구매', '기술 평가자', '챔피언', '사용자', '게이트키퍼', '법무·계약'];
  var seq = 0;  // 한 문서에 도식이 여럿 들어가도 무늬 id가 겹치지 않게

  // ---- 공통 ------------------------------------------------------------------

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function num(v) { v = Number(v); return isFinite(v) ? v : 0; }
  function r1(v) { return Math.round(num(v) * 10) / 10; }

  // 글자 폭 어림: 한글·한자는 글자 크기만큼, 영문·숫자는 그 절반 남짓
  function tw(s, fs) {
    var w = 0; s = String(s == null ? '' : s);
    for (var i = 0; i < s.length; i++) {
      var c = s.charCodeAt(i);
      w += c >= 0x2e80 ? fs : c >= 0x2000 ? fs * 0.9 : c === 32 ? fs * 0.3 : fs * 0.58;
    }
    return w;
  }
  function clip(s, maxW, fs) {
    s = String(s == null ? '' : s);
    if (tw(s, fs) <= maxW) return s;
    while (s.length > 1 && tw(s + '…', fs) > maxW) s = s.slice(0, -1);
    return s + '…';
  }
  // 좁은 칸 아래의 이름은 두 줄까지 나눠 쓴다(가운뎃점·빈칸에서 끊기를 먼저 본다)
  function wrap2(s, maxW, fs) {
    s = String(s == null ? '' : s);
    if (tw(s, fs) <= maxW) return [s];
    var cut = 1;
    while (cut < s.length && tw(s.slice(0, cut + 1), fs) <= maxW) cut++;
    for (var i = cut; i > 1; i--) if (/[\s·]/.test(s.charAt(i - 1)) || /[\s·]/.test(s.charAt(i))) { cut = /[\s]/.test(s.charAt(i - 1)) ? i - 1 : i; break; }
    return [s.slice(0, cut).trim(), clip(s.slice(cut).trim(), maxW, fs)];
  }
  function text(x, y, s, o) {
    o = o || {};
    return '<text x="' + r1(x) + '" y="' + r1(y) + '" font-size="' + (o.size || 11) + '" fill="' + (o.fill || INK) + '"'
      + (o.bold ? ' font-weight="700"' : '') + (o.anchor ? ' text-anchor="' + o.anchor + '"' : '') + '>' + esc(s) + '</text>';
  }
  function rect(x, y, w, h, o) {
    o = o || {};
    return '<rect x="' + r1(x) + '" y="' + r1(y) + '" width="' + r1(Math.max(0, w)) + '" height="' + r1(Math.max(0, h)) + '" fill="' + (o.fill || 'none') + '"'
      + (o.stroke ? ' stroke="' + o.stroke + '" stroke-width="' + (o.sw || 1) + '"' : '') + (o.dash ? ' stroke-dasharray="' + o.dash + '"' : '') + '/>';
  }
  function line(x1, y1, x2, y2, o) {
    o = o || {};
    return '<line x1="' + r1(x1) + '" y1="' + r1(y1) + '" x2="' + r1(x2) + '" y2="' + r1(y2) + '" stroke="' + (o.stroke || LINE) + '" stroke-width="' + (o.sw || 1) + '"'
      + (o.dash ? ' stroke-dasharray="' + o.dash + '"' : '') + '/>';
  }
  function svg(h, body, label) {
    return '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ' + W + ' ' + Math.ceil(h) + '" width="100%" font-family="' + FONT + '" role="img" aria-label="' + esc(label) + '">'
      + body + '</svg>';
  }
  function empty(message, label) {
    return svg(40, rect(0.5, 0.5, W - 1, 39, { stroke: LIGHT, dash: '4 3' }) + text(W / 2, 24, message, { size: 11.5, fill: GRAY, anchor: 'middle' }), label || message);
  }
  // AI 초안을 나타내는 빗금(색을 못 쓰는 인쇄에서도 구분된다)
  function hatch(id) {
    return '<defs><pattern id="' + id + '" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">'
      + '<rect width="6" height="6" fill="' + PALE + '"/><line x1="0" y1="0" x2="0" y2="6" stroke="' + ORANGE + '" stroke-width="2"/></pattern></defs>';
  }
  // 'YYYY-MM-DD' → 날짜 번호(하루 = 1). 못 읽으면 null
  function day(s) {
    var m = /^(\d{4})-(\d{1,2})-(\d{1,2})/.exec(String(s == null ? '' : s));
    if (!m) return null;
    var t = Date.UTC(+m[1], +m[2] - 1, +m[3]);
    return isFinite(t) ? Math.round(t / 864e5) : null;
  }
  function parts(d) { var x = new Date(d * 864e5); return { y: x.getUTCFullYear(), m: x.getUTCMonth(), d: x.getUTCDate() }; }
  function md(d) { var p = parts(d); return (p.m + 1) + '/' + (p.d < 10 ? '0' : '') + p.d; }
  function two(n) { return (n < 10 ? '0' : '') + n; }

  // ---- 1. 결과지 완결성 --------------------------------------------------------

  function completeness(d) {
    d = d || {};
    var items = (d.items || []).filter(Boolean);
    var total = num(d.total) || items.length;
    if (!total) return empty('결과지 항목이 없습니다', '결과지 완결성');
    var count = function (s) { return items.filter(function (it) { return it.status === s; }).length; };
    var confirmed = d.confirmed != null ? num(d.confirmed) : count('확인');
    var filled = d.filled != null ? num(d.filled) : items.length - count('미확보');
    var pct = Math.round(confirmed / total * 100), fpct = Math.round(filled / total * 100);
    var id = 'sc-h' + (++seq);

    var left = 172, areaW = W - left, n = items.length, gap = 4;
    var rows = n > 12 ? 2 : 1, perRow = n > 12 ? Math.ceil(n / 2) : Math.max(n, 1);
    var cw = Math.min(62, (areaW - gap * (perRow - 1)) / perRow), ch = 28, rowH = ch + 34, top = 8;
    var b = hatch(id);
    b += text(0, 44, pct + '%', { size: 38, bold: true });
    b += text(1, 64, '확정 ' + confirmed + ' / ' + total, { size: 12, bold: true });
    b += text(1, 80, 'AI 초안까지 ' + filled + ' / ' + total + ' (' + fpct + '%)', { size: 10.5, fill: GRAY });

    items.forEach(function (it, i) {
      var x = left + (i % perRow) * (cw + gap), y = top + Math.floor(i / perRow) * rowH, s = it.status;
      if (s === '확인') b += rect(x, y, cw, ch, { fill: INK, stroke: INK });
      else if (s === '초안') b += rect(x + 0.5, y + 0.5, cw - 1, ch - 1, { fill: 'url(#' + id + ')', stroke: ORANGE });
      else b += rect(x + 0.5, y + 0.5, cw - 1, ch - 1, { fill: '#fff', stroke: LINE, dash: '3 2' });
      var code = String(it.code == null ? '' : it.code);
      if (s === '초안') b += rect(x + cw / 2 - tw(code, 11) / 2 - 3, y + 7, tw(code, 11) + 6, 15, { fill: '#fff' });  // 빗금 위에서도 코드가 읽히게
      b += text(x + cw / 2, y + 18.5, (s === '확인' ? '✓ ' : '') + code, { size: 11, bold: true, anchor: 'middle', fill: s === '확인' ? '#fff' : s === '초안' ? INK : GRAY });
      wrap2(it.label, cw + gap - 2, 9.5).forEach(function (part, k) {
        b += text(x + cw / 2, y + ch + 12 + k * 11.5, part, { size: 9.5, anchor: 'middle', fill: s === '미확보' ? GRAY : INK });
      });
    });

    var ly = Math.max(top + rows * rowH + 6, 98), lx = left;
    b += rect(lx, ly - 9, 12, 10, { fill: INK }) + text(lx + 17, ly, '확정 — 사람이 확인함', { size: 10 });
    lx += 17 + tw('확정 — 사람이 확인함', 10) + 18;
    b += rect(lx + 0.5, ly - 8.5, 11, 9, { fill: 'url(#' + id + ')', stroke: ORANGE }) + text(lx + 17, ly, 'AI 초안 — 확인 전', { size: 10 });
    lx += 17 + tw('AI 초안 — 확인 전', 10) + 18;
    b += rect(lx + 0.5, ly - 8.5, 11, 9, { fill: '#fff', stroke: LINE, dash: '3 2' }) + text(lx + 17, ly, '미확보', { size: 10 });
    return svg(ly + 8, b, '결과지 완결성 ' + pct + '% (확정 ' + confirmed + ' / ' + total + ')');
  }

  // ---- 2. 회차별 채움 추이 ------------------------------------------------------

  function trend(timeline) {
    var pts = (timeline || []).filter(Boolean);
    if (!pts.length) return empty('확정된 회의가 없습니다', '회차별 채움 추이');
    var x0 = 70, x1 = W - 50, yTop = 22, yBot = 112, H = 166;
    var px = function (i) { return pts.length === 1 ? (x0 + x1) / 2 : x0 + i * (x1 - x0) / (pts.length - 1); };
    var py = function (p) { return yBot - Math.max(0, Math.min(100, num(p))) / 100 * (yBot - yTop); };
    var b = '';
    [0, 50, 100].forEach(function (v) {
      b += line(40, py(v), W - 10, py(v), { stroke: v ? LIGHT : LINE, dash: v ? '2 3' : '' }) + text(34, py(v) + 3.5, v + '%', { size: 9.5, fill: GRAY, anchor: 'end' });
    });
    // 계단형: 회의록이 확정되는 순간에 값이 뛴다
    var step = function (key) {
      var p = 'M' + r1(px(0)) + ' ' + r1(py(pts[0][key]));
      for (var i = 1; i < pts.length; i++) p += ' H' + r1(px(i)) + ' V' + r1(py(pts[i][key]));
      return p;
    };
    b += '<path d="' + step('filled_pct') + '" fill="none" stroke="' + ORANGE + '" stroke-width="1.5" stroke-dasharray="5 3"/>';
    b += '<path d="' + step('confirmed_pct') + '" fill="none" stroke="' + INK + '" stroke-width="1.8"/>';
    pts.forEach(function (p, i) {
      var x = px(i), yf = py(p.filled_pct), yc = py(p.confirmed_pct);
      b += '<circle cx="' + r1(x) + '" cy="' + r1(yf) + '" r="3.6" fill="#fff" stroke="' + ORANGE + '" stroke-width="1.6"/>';
      b += '<circle cx="' + r1(x) + '" cy="' + r1(yc) + '" r="3.2" fill="' + INK + '"/>';
      b += text(x, yf - 8, Math.round(num(p.filled_pct)) + '%', { size: 10, fill: ORANGE, bold: true, anchor: 'middle' });
      b += text(x, yc + 15, Math.round(num(p.confirmed_pct)) + '%', { size: 10, bold: true, anchor: 'middle' });
      var dt = String(p.date || '');
      b += text(x, yBot + 34, (p.no != null ? p.no + '차' : '') + (dt.length >= 10 ? ' ' + dt.slice(5) : ''), { size: 10, fill: GRAY, anchor: 'middle' });
    });
    var ly = H - 6;
    b += line(40, ly - 3.5, 62, ly - 3.5, { stroke: ORANGE, sw: 1.5, dash: '5 3' }) + text(68, ly, '채움 — AI 초안 포함', { size: 10, fill: GRAY });
    var lx = 68 + tw('채움 — AI 초안 포함', 10) + 18;
    b += line(lx, ly - 3.5, lx + 22, ly - 3.5, { stroke: INK, sw: 1.8 }) + text(lx + 28, ly, '완결성 — 사람이 확인', { size: 10, fill: GRAY });
    return svg(H, b, '회의록이 확정될 때마다 결과지가 찬 정도');
  }

  // ---- 3. 의사결정 조직도 -------------------------------------------------------

  function orgMap(d) {
    d = d || {};
    var people = (d.people || []).filter(function (p) { return p && p.name; });
    var team = (d.team || []).filter(function (t) { return t && t.name; });
    var has = function (p, role) { return (p.roles || []).indexOf(role) >= 0; };

    // 고객 측: 역할 순서대로. 여러 역할을 가진 사람은 가장 앞선 역할 자리에 한 번만 놓는다. 아무도 없는 역할은 빈자리로 그린다
    var placed = {}, rows = [];
    ROLES.forEach(function (role) {
      var anyone = false;
      people.forEach(function (p) {
        if (!has(p, role)) return;
        anyone = true;
        if (!placed[p.name]) { placed[p.name] = 1; rows.push({ p: p }); }
      });
      if (!anyone) rows.push({ blank: role });
    });
    people.forEach(function (p) { if (!placed[p.name]) rows.push({ p: p }); });

    var pitch = 46, bh = 38, pad = 6;
    var H = Math.max(rows.length, team.length, 1) * pitch + pad * 2 - (pitch - bh);
    var cy = H / 2, cx = 262, cw = 180, ch = 46, rx = 492, rw = W - rx, lw = 204;
    var b = '';

    // 가운데: 고객사
    b += rect(cx, cy - ch / 2, cw, ch, { fill: BAND, stroke: INK, sw: 1.5 });
    b += text(cx + cw / 2, cy - 3, clip(d.customer || '고객사', cw - 16, 13), { size: 13, bold: true, anchor: 'middle' });
    b += text(cx + cw / 2, cy + 13, '의사결정 조직', { size: 9.5, fill: GRAY, anchor: 'middle' });

    // 오른쪽: 고객 측 사람과 빈자리
    var mid = (cx + cw + rx) / 2;
    rows.forEach(function (row, i) {
      var y = pad + i * pitch, ym = y + bh / 2, blank = !!row.blank;
      b += '<path d="M' + (cx + cw) + ' ' + r1(cy) + ' H' + r1(mid) + ' V' + r1(ym) + ' H' + rx + '" fill="none" stroke="' + LINE + '" stroke-width="1"' + (blank ? ' stroke-dasharray="3 3"' : '') + '/>';
      if (blank) {
        b += rect(rx + 0.5, y + 0.5, rw - 1, bh - 1, { fill: '#fff', stroke: LINE, dash: '4 3' });
        b += text(rx + 10, ym + 4, row.blank, { size: 11, fill: GRAY });
        b += text(rx + rw - 10, ym + 4, '미확인', { size: 10.5, fill: GRAY, anchor: 'end' });
        return;
      }
      var p = row.p, draft = p.status === '초안', done = p.status === '확인';
      b += rect(rx + 0.5, y + 0.5, rw - 1, bh - 1, { fill: '#fff', stroke: draft ? ORANGE : INK, sw: done ? 1.6 : 1 });
      var mark = done ? '확정' : draft ? 'AI 초안' : '';
      var markW = mark ? tw(mark, 9.5) + 8 : 0;
      var name = clip(p.name, 70, 12), sub = [p.dept, p.title].filter(Boolean).join(' ');
      b += text(rx + 10, y + 15.5, name, { size: 12, bold: true });
      b += text(rx + 14 + tw(name, 12), y + 15.5, clip(sub, rw - 28 - tw(name, 12) - markW, 10), { size: 10, fill: GRAY });
      if (mark) b += text(rx + rw - 8, y + 15, mark, { size: 9.5, fill: draft ? ORANGE : INK, bold: done, anchor: 'end' });
      var roles = (p.roles || []).join(' · ');
      b += text(rx + 10, y + 30.5, clip(roles || '역할 미확인', rw - 20, 10.5), { size: 10.5, fill: roles ? ORANGE : GRAY, bold: !!roles });
    });

    // 왼쪽: 당사
    var lmid = (lw + cx) / 2, ty0 = cy - (team.length * pitch - (pitch - bh)) / 2;
    team.forEach(function (t, i) {
      var y = ty0 + i * pitch, ym = y + bh / 2;
      b += '<path d="M' + cx + ' ' + r1(cy) + ' H' + r1(lmid) + ' V' + r1(ym) + ' H' + lw + '" fill="none" stroke="' + LINE + '" stroke-width="1"/>';
      b += rect(0, y, lw, bh, { fill: INK });
      b += text(10, y + 15.5, clip(t.name, lw - 20, 12), { size: 12, bold: true, fill: '#fff' });
      b += text(10, y + 30.5, clip([t.org, t.role].filter(Boolean).join(' · ') || '역할 미정', lw - 20, 10.5), { size: 10.5, fill: LIGHT });
    });
    if (!team.length) {
      b += '<path d="M' + cx + ' ' + r1(cy) + ' H' + lw + '" fill="none" stroke="' + LINE + '" stroke-width="1" stroke-dasharray="3 3"/>';
      b += rect(0.5, cy - bh / 2 + 0.5, lw - 1, bh - 1, { fill: '#fff', stroke: LINE, dash: '4 3' }) + text(10, cy + 4, '당사 담당 미배정', { size: 11, fill: GRAY });
    }
    return svg(H, b, '의사결정 조직도');
  }

  // ---- 4. 일정 막대 -------------------------------------------------------------

  function schedule(d) {
    d = d || {};
    var rows = (d.rows || []).map(function (r) {
      var s = day(r && r.start), e = day(r && r.end);
      return s == null ? null : { label: r.label, s: s, e: e != null && e >= s ? e : s, approx: !!r.approx, note: r.note };
    }).filter(Boolean);
    if (!rows.length) return empty('날짜가 정해진 일정이 없습니다', '일정');

    var minD = Math.min.apply(null, rows.map(function (r) { return r.s; })), maxD = Math.max.apply(null, rows.map(function (r) { return r.e; }));
    var a = parts(minD), z = parts(maxD);
    var months = (z.y - a.y) * 12 + (z.m - a.m) + 1;
    if (months < 3) months = 3;
    var d0 = Math.round(Date.UTC(a.y, a.m, 1) / 864e5), d1 = Math.round(Date.UTC(a.y, a.m + months, 1) / 864e5);
    var bx0 = 250, bx1 = 590, top = 22, rh = 26;
    var X = function (dd) { return bx0 + (dd - d0) / (d1 - d0) * (bx1 - bx0); };
    var H = top + rows.length * rh + 18;
    var b = '', every = Math.ceil(months / 12);

    for (var i = 0; i <= months; i++) {
      var ms = Math.round(Date.UTC(a.y, a.m + i, 1) / 864e5), x = X(ms);
      b += line(x, top - 4, x, H - 16, { stroke: LIGHT, dash: i === 0 || i === months ? '' : '2 3' });
      if (i < months && i % every === 0) {
        var p = parts(ms);
        b += text(x + 4, 12, (i === 0 || p.m === 0 ? String(p.y).slice(2) + '.' : '') + two(p.m + 1), { size: 10, fill: GRAY });
      }
    }
    b += line(bx0, top - 4, bx1, top - 4, { stroke: LINE });

    rows.forEach(function (r, i) {
      var y = top + i * rh, x = X(r.s), w = Math.max(5, X(r.e + 1) - x);
      b += text(0, y + 15, clip(r.label, bx0 - 12, 11.5), { size: 11.5 });
      if (r.approx) b += rect(x + 0.5, y + 5.5, w - 1, 11, { fill: '#fff', stroke: ORANGE, sw: 1.4, dash: '3 2' });
      else b += rect(x, y + 5, w, 12, { fill: INK });
      var when = md(r.s) + (r.e > r.s ? ' ~ ' + md(r.e) : '') + (r.approx ? ' 쯤' : '');
      b += text(bx1 + 8, y + 15, clip(when + (r.note ? ' · ' + r.note : ''), W - bx1 - 8, 10.5), { size: 10.5, fill: GRAY });
    });

    var t = day(d.today);
    if (t != null && t >= d0 && t < d1) {
      var tx = X(t + 0.5);
      b += line(tx, top - 4, tx, H - 16, { stroke: ORANGE, sw: 1.4 }) + text(tx, H - 4, '오늘 ' + md(t), { size: 9.5, fill: ORANGE, bold: true, anchor: 'middle' });
    }
    return svg(H, b, '일정');
  }

  // ---- 5. 회의일과 기한의 한 줄 타임라인 ------------------------------------------

  function dueline(d) {
    d = d || {};
    var meet = day(d.meeting);
    var list = (d.items || []).map(function (it) {
      var dd = day(it && it.date);
      return dd == null ? null : { d: dd, text: md(dd) + ' ' + String(it.label == null ? '' : it.label) + (it.who ? '(' + it.who + ')' : '') + (it.approx ? ' 쯤' : ''), approx: !!it.approx };
    }).filter(Boolean);
    if (meet != null) list.push({ d: meet, text: md(meet) + ' 회의', meeting: true });
    if (!list.length) return empty('날짜가 정해진 기한이 없습니다', '회의일과 기한');

    // 같은 날은 한 점에 묶는다(회의가 맨 앞)
    list.sort(function (p, q) { return p.d - q.d || (q.meeting ? 1 : 0) - (p.meeting ? 1 : 0); });
    var groups = [];
    list.forEach(function (it) {
      var g = groups[groups.length - 1];
      if (g && g.d === it.d) g.items.push(it); else groups.push({ d: it.d, items: [it] });
    });

    // 점의 자리: 날짜 간격과 순서를 반씩 섞는다. 날짜 간격만 쓰면 회의 직후의 기한들이 한 점에 몰려 읽을 수 없다(눈금 없는 순서도다)
    var x0 = 24, x1 = W - 24, minD = groups[0].d, maxD = groups[groups.length - 1].d, n = groups.length;
    groups.forEach(function (g, i) {
      var byRank = n === 1 ? 0.5 : i / (n - 1), byDate = maxD === minD ? 0.5 : (g.d - minD) / (maxD - minD);
      g.x = x0 + (byRank * 0.5 + byDate * 0.5) * (x1 - x0);
    });

    // 꼬리표를 위아래 단에 놓는다. 겹치면 다음 단으로 올린다
    var fs = 10.5, lh = 15, lanes = { up: [], down: [] };
    groups.forEach(function (g, gi) {
      var side = gi % 2 === 0 ? 'up' : 'down', other = side === 'up' ? 'down' : 'up';
      var width = Math.max.apply(null, g.items.map(function (it) { return tw(clip(it.text, 250, fs), fs); }));
      var left = Math.min(Math.max(0, g.x - 6), W - width), right = left + width;
      var need = g.items.length;
      // 이 묶음이 통째로 들어갈 가장 낮은 연속 단을 찾는다
      var fit = function (s) {
        for (var start = 0; start < 12; start++) {
          var ok = true;
          for (var k = 0; k < need; k++) if ((lanes[s][start + k] != null ? lanes[s][start + k] : -1e9) > left - 8) { ok = false; break; }
          if (ok) return start;
        }
        return 12;
      };
      var a = fit(side), o = fit(other);
      if (o < a) { side = other; a = o; }
      g.side = side; g.lane = a; g.left = left;
      for (var k = 0; k < need; k++) lanes[side][a + k] = right;
    });

    var upH = lanes.up.length * lh, downH = lanes.down.length * lh;
    var axis = upH + 16, H = axis + downH + 18, b = '';
    b += line(8, axis, W - 8, axis, { stroke: INK, sw: 1.2 });
    groups.forEach(function (g) {
      var up = g.side === 'up', far = g.lane + g.items.length;
      var yEnd = up ? axis - 8 - far * lh + 4 : axis + 8 + far * lh - 4;
      b += line(g.x, axis, g.x, yEnd, { stroke: LIGHT });
      g.items.forEach(function (it, k) {
        var lane = g.lane + k;
        var y = up ? axis - 12 - lane * lh : axis + 20 + lane * lh;
        b += text(g.left, y, clip(it.text, 250, fs), { size: fs, bold: !!it.meeting, fill: it.meeting ? INK : it.approx ? GRAY : INK });
      });
      var m = g.items.some(function (it) { return it.meeting; }), hard = g.items.some(function (it) { return !it.meeting && !it.approx; });
      if (m) b += '<circle cx="' + r1(g.x) + '" cy="' + axis + '" r="5.5" fill="' + INK + '"/>';
      else if (hard) b += '<circle cx="' + r1(g.x) + '" cy="' + axis + '" r="4.5" fill="' + ORANGE + '" stroke="#fff" stroke-width="1"/>';
      else b += '<circle cx="' + r1(g.x) + '" cy="' + axis + '" r="4" fill="#fff" stroke="' + ORANGE + '" stroke-width="1.5" stroke-dasharray="2 1.5"/>';
      if (m && hard) b += '<circle cx="' + r1(g.x) + '" cy="' + axis + '" r="2.2" fill="' + ORANGE + '"/>';  // 회의 당일이 기한인 일이 있다
    });
    return svg(H, b, '회의일과 액션아이템 기한');
  }

  // ---- 6. 발언 비중 -------------------------------------------------------------

  function share(rows) {
    rows = (rows || []).filter(function (r) { return r && r.name && num(r.pct) > 0; });
    var sum = rows.reduce(function (s, r) { return s + num(r.pct); }, 0);
    if (!rows.length || !sum) return empty('화자를 나눌 수 없는 녹취입니다', '발언 비중');
    var b = '', x = 0, bh = 18, fs = 10.5;
    rows.forEach(function (r) {
      var w = num(r.pct) / sum * W, ours = r.side === '당사';
      b += rect(x + 0.5, 0.5, Math.max(1, w - 1), bh, { fill: ours ? INK : '#fff', stroke: INK });
      var label = Math.round(num(r.pct)) + '%';
      if (w > tw(label, 10) + 8) b += text(x + w / 2, 13.5, label, { size: 10, bold: true, anchor: 'middle', fill: ours ? '#fff' : INK });
      x += w;
    });
    // 이름은 막대 아래에 차례대로(좁은 칸 밑에 억지로 맞추지 않는다)
    var lx = 0, ly = bh + 18;
    rows.forEach(function (r) {
      var label = r.name + ' ' + Math.round(num(r.pct)) + '%', w = 16 + tw(label, fs) + 14;
      if (lx + w > W) { lx = 0; ly += 17; }
      b += rect(lx + 0.5, ly - 8.5, 10, 9, { fill: r.side === '당사' ? INK : '#fff', stroke: INK }) + text(lx + 15, ly, label, { size: fs });
      lx += w;
    });
    var side = function (s) { return Math.round(rows.filter(function (r) { return r.side === s; }).reduce(function (t, r) { return t + num(r.pct); }, 0) / sum * 100); };
    if (rows.some(function (r) { return r.side === '고객'; }) && rows.some(function (r) { return r.side === '당사'; })) {
      var total = '고객 ' + side('고객') + '% · 당사 ' + side('당사') + '%';
      if (lx + tw(total, fs) + 8 > W) { lx = 0; ly += 17; }
      b += text(W, ly, total, { size: fs, bold: true, anchor: 'end' });
    }
    return svg(ly + 8, b, '발언 비중');
  }

  // ---- 문서 절 안에서 긴 글을 돕는 도식 ------------------------------------------

  function wrapN(s, maxW, fs, n) {
    s = String(s == null ? '' : s).replace(/\s+/g, ' ').trim();
    var out = [];
    while (s && out.length < n) {
      if (tw(s, fs) <= maxW) { out.push(s); s = ''; break; }
      var cut = 1;
      while (cut < s.length && tw(s.slice(0, cut + 1), fs) <= maxW) cut++;
      for (var i = cut; i > cut - 8 && i > 1; i--) if (/[\s·,/]/.test(s.charAt(i - 1))) { cut = i; break; }
      out.push(s.slice(0, cut).trim()); s = s.slice(cut).trim();
    }
    if (s && out.length) out[out.length - 1] = clip(out[out.length - 1] + '…', maxW, fs);
    return out;
  }
  function arrow(x1, y, x2) {
    return line(x1, y, x2 - 5, y, { stroke: INK }) + '<path d="M' + r1(x2) + ' ' + r1(y) + ' l-7 -4 v8 z" fill="' + INK + '"/>';
  }

  // 단계가 이어지는 흐름: [{ title, text }] — 글이 없는 칸은 점선으로 비워 둔다
  function flow(steps) {
    steps = (steps || []).filter(Boolean);
    if (!steps.some(function (st) { return st.text; })) return '';
    var gap = 26, bw = (W - gap * (steps.length - 1)) / steps.length, fs = 11, pad = 10;
    var rows = steps.map(function (st) { return wrapN(st.text, bw - pad * 2, fs, 14); });
    var bh = 30 + Math.max.apply(null, rows.map(function (r) { return Math.max(1, r.length); })) * 16 + 6, b = '';
    steps.forEach(function (st, i) {
      var x = i * (bw + gap);
      b += rect(x + 0.5, 0.5, bw - 1, bh - 1, { stroke: st.text ? INK : LINE, dash: st.text ? '' : '4 3', fill: '#fff' })
        + rect(x + 0.5, 0.5, bw - 1, 22, { fill: st.text ? '#16365C' : BAND, stroke: st.text ? '#16365C' : LINE })
        + text(x + pad, 15.5, st.title, { size: 11, bold: true, fill: st.text ? '#fff' : GRAY });
      if (rows[i].length) rows[i].forEach(function (l, k) { b += text(x + pad, 40 + k * 16, l, { size: fs }); });
      else b += text(x + pad, 40, '미확보', { size: fs, fill: GRAY });
      if (i < steps.length - 1) b += arrow(x + bw + 3, bh / 2, x + bw + gap - 3);
    });
    return svg(bh, b, steps.map(function (st) { return st.title; }).join(' → '));
  }

  // 목표마다 현재 → 희망: [{ goal, current, target }] — 글은 줄이지 않고 줄을 바꿔 다 적는다
  function goals(rows) {
    rows = (rows || []).filter(function (r) { return r && r.goal; });
    if (!rows.length) return '';
    var lw = 230, cw = (W - lw - 40) / 2, y = 18, b = text(lw + cw / 2, 11, '현재', { size: 10.5, fill: GRAY, anchor: 'middle' }) + text(lw + cw + 40 + cw / 2, 11, '희망', { size: 10.5, fill: GRAY, anchor: 'middle' });
    rows.forEach(function (r) {
      var name = wrapN(r.goal, lw - 12, 11.5, 12), cur = wrapN(r.current || '미확인', cw - 16, 11, 12), tar = wrapN(r.target || '미확인', cw - 16, 11, 12);
      var h = Math.max(name.length, cur.length, tar.length) * 15 + 16, mid = function (n) { return y + (h - n * 15) / 2 + 11; };
      name.forEach(function (l, k) { b += text(0, mid(name.length) + k * 15, l, { size: 11.5, bold: true }); });
      b += rect(lw + 0.5, y + 0.5, cw - 1, h - 1, { stroke: LINE, dash: r.current ? '' : '4 3', fill: '#fff' });
      cur.forEach(function (l, k) { b += text(lw + cw / 2, mid(cur.length) + k * 15, l, { size: 11, anchor: 'middle', fill: r.current ? INK : GRAY }); });
      b += arrow(lw + cw + 5, y + h / 2, lw + cw + 35)
        + rect(lw + cw + 40.5, y + 0.5, cw - 1, h - 1, { stroke: r.target ? ORANGE : LINE, sw: r.target ? 1.5 : 1, dash: r.target ? '' : '4 3', fill: r.target ? PALE : '#fff' });
      tar.forEach(function (l, k) { b += text(lw + cw + 40 + cw / 2, mid(tar.length) + k * 15, l, { size: 11, bold: !!r.target, anchor: 'middle', fill: r.target ? INK : GRAY }); });
      y += h + 8;
    });
    return svg(y, b, '비즈니스 목표: 현재와 희망 수준');
  }

  // 요구사항 → 당사 대응: [{ title, response, response_status }]
  function reqs(rows) {
    rows = (rows || []).filter(function (r) { return r && r.title; });
    if (!rows.length) return '';
    var lw = 250, sw = 78, rw = W - lw - 34 - sw - 8, y = 16, b = text(0, 11, '고객 요구', { size: 10.5, fill: GRAY }) + text(lw + 34, 11, '당사 대응', { size: 10.5, fill: GRAY });
    rows.forEach(function (r) {
      var a = wrapN(r.title, lw - 16, 11.5, 8), c = wrapN(r.response || '', rw - 16, 11, 12), h = Math.max(a.length, c.length, 1) * 15 + 14;
      b += rect(0.5, y + 0.5, lw - 1, h - 1, { stroke: INK, fill: '#fff' });
      a.forEach(function (l, k) { b += text(8, y + 18 + k * 15, l, { size: 11.5, bold: true }); });
      b += arrow(lw + 4, y + h / 2, lw + 30) + rect(lw + 34.5, y + 0.5, rw - 1, h - 1, { stroke: LINE, dash: r.response ? '' : '4 3', fill: r.response ? BAND : '#fff' });
      if (c.length) c.forEach(function (l, k) { b += text(lw + 42, y + 18 + k * 15, l, { size: 11 }); });
      else b += text(lw + 42, y + 18, '대응안 없음', { size: 11, fill: GRAY });
      b += text(W, y + h / 2 + 4, clip(r.response_status || '확인 필요', sw, 11), { size: 11, bold: true, anchor: 'end', fill: /가능|확정|완료/.test(r.response_status || '') ? INK : ORANGE });
      y += h + 6;
    });
    return svg(y, b, '핵심 요구사항과 당사 대응');
  }

  // 논의 주제 → 결론: [{ topic, conclusion }]
  function topics(rows) {
    rows = (rows || []).filter(function (r) { return r && r.topic; });
    if (!rows.length) return '';
    var lw = 190, y = 0, b = '';
    rows.forEach(function (r, i) {
      var c = wrapN(r.conclusion || '', W - lw - 44, 11.5, 8), tp = wrapN((i + 1) + '. ' + r.topic, lw - 18, 11.5, 4), h = Math.max(c.length, tp.length, 1) * 16 + 12;
      b += rect(0.5, y + 0.5, lw - 1, h - 1, { fill: '#16365C', stroke: '#16365C' }) + tp.map(function (l, k) { return text(10, y + (h - tp.length * 16) / 2 + 12 + k * 16, l, { size: 11.5, bold: true, fill: '#fff' }); }).join('')
        + arrow(lw + 4, y + h / 2, lw + 30);
      if (c.length) c.forEach(function (l, k) { b += text(lw + 38, y + 19 + k * 16, l, { size: 11.5 }); });
      else b += text(lw + 38, y + 19, '결론 없음', { size: 11.5, fill: GRAY });
      b += line(lw + 34, y + h + 2.5, W, y + h + 2.5, { stroke: LIGHT });
      y += h + 6;
    });
    return svg(y, b, '논의 주제와 결론');
  }

  // 중요도 × 시급성: [{ kind, label, importance, urgency }] — 값을 읽지 못한 줄은 그리지 않는다
  function matrix(rows) {
    var lv = function (v) { v = String(v || ''); return /상|높|긴급|high/i.test(v) ? 2 : /중|보통|mid/i.test(v) ? 1 : /하|낮|low/i.test(v) ? 0 : -1; };
    var cells = {}, n = 0;
    (rows || []).forEach(function (r) {
      var a = lv(r.importance), u = lv(r.urgency);
      if (a < 0 || u < 0 || !r.label) return;
      (cells[a + '-' + u] = cells[a + '-' + u] || []).push(r); n++;
    });
    if (n < 2) return '';
    var ax = 56, cw = (W - ax) / 3, names = ['낮음', '보통', '높음'], most = 1;
    Object.keys(cells).forEach(function (k) { most = Math.max(most, cells[k].length); });
    var ch = Math.min(4, most) * 15 + 14, b = '';
    for (var a = 2; a >= 0; a--) {
      var y = (2 - a) * ch;
      b += text(ax - 8, y + ch / 2 + 4, names[a], { size: 10.5, fill: GRAY, anchor: 'end' });
      for (var u = 0; u < 3; u++) {
        var list = cells[a + '-' + u] || [], hot = a === 2 && u === 2;
        b += rect(ax + u * cw + 0.5, y + 0.5, cw - 1, ch - 1, { stroke: hot ? ORANGE : LINE, sw: hot ? 1.5 : 1, fill: hot ? PALE : '#fff' });
        list.slice(0, 4).forEach(function (r, k) {
          var last = k === 3 && list.length > 4;
          b += text(ax + u * cw + 8, y + 17 + k * 15, last ? '외 ' + (list.length - 3) + '건' : clip('· ' + r.label, cw - 16, 10.5), { size: 10.5, fill: last ? GRAY : INK });
        });
      }
    }
    b += text(0, 3 * ch / 2 - 4, '중요도', { size: 10.5, bold: true });
    for (var k = 0; k < 3; k++) b += text(ax + k * cw + cw / 2, 3 * ch + 14, names[k], { size: 10.5, fill: GRAY, anchor: 'middle' });
    b += text(W, 3 * ch + 14, '시급성 →', { size: 10.5, bold: true, anchor: 'end' });
    return svg(3 * ch + 20, b, '중요도와 시급성');
  }

  // ---- 보고서에서 흔히 쓰는 도식: 파이 · 하비볼 · 포지셔닝 맵 ---------------------

  // 계열 순서와 색은 INTERX 표준(orange → navy → orange_soft → gray → line_blue → orange_light)
  var NAVY = '#16365C', SHADES = ['#FF8000', NAVY, '#FBAE40', '#8E8E8E', '#C1C7CD', '#FF8E1C'];
  function wedge(cx, cy, R, r, a0, a1, fill) {
    if (a1 - a0 >= Math.PI * 2 - 1e-6) a1 = a0 + Math.PI * 2 - 1e-4;
    var p = function (rad, a) { return r1(cx + rad * Math.sin(a)) + ' ' + r1(cy - rad * Math.cos(a)); }, big = a1 - a0 > Math.PI ? 1 : 0;
    return '<path d="M' + p(R, a0) + ' A' + R + ' ' + R + ' 0 ' + big + ' 1 ' + p(R, a1) + ' L' + p(r, a1) + ' A' + r + ' ' + r + ' 0 ' + big + ' 0 ' + p(r, a0) + ' Z" fill="' + fill + '" stroke="#fff" stroke-width="1"/>';
  }
  // 파이(도넛): [{ name, value }], { title, unit }
  function donut(rows, o) {
    o = o || {};
    rows = (rows || []).filter(function (r) { return r && num(r.value) > 0; });
    var sum = rows.reduce(function (t, r) { return t + num(r.value); }, 0);
    if (!sum) return '';
    var cx = 62, cy = 62, a = 0, b = '', h = Math.max(124, 30 + rows.length * 19);
    rows.forEach(function (r, i) { var d = num(r.value) / sum * Math.PI * 2; b += wedge(cx, cy, 54, 30, a, a + d, r.fill || SHADES[i % SHADES.length]); a += d; });
    b += text(cx, cy + 4, o.center || '', { size: 12, bold: true, anchor: 'middle' });
    if (o.title) b += text(150, 16, o.title, { size: 11.5, bold: true });
    rows.forEach(function (r, i) {
      var y = (o.title ? 38 : 20) + i * 19, pct = Math.round(num(r.value) / sum * 100);
      b += rect(150, y - 9, 10, 10, { fill: r.fill || SHADES[i % SHADES.length] }) + text(167, y, clip(r.name, 330, 11.5), { size: 11.5 })
        + text(W, y, (o.unit ? r.value + o.unit + ' · ' : '') + pct + '%', { size: 11.5, bold: true, anchor: 'end' });
    });
    return svg(h, b, o.title || '구성 비율');
  }
  // 하비볼: [{ label, done, total, note }] — 채운 만큼 원을 칠한다
  function harvey(rows) {
    rows = (rows || []).filter(function (r) { return r && num(r.total) > 0; });
    if (!rows.length) return '';
    var cw = W / rows.length, b = '';
    rows.forEach(function (r, i) {
      var cx = i * cw + 26, cy = 28, f = Math.min(1, num(r.done) / num(r.total));
      b += '<circle cx="' + cx + '" cy="' + cy + '" r="18" fill="#fff" stroke="' + INK + '" stroke-width="1.5"/>';
      if (f >= 1) b += '<circle cx="' + cx + '" cy="' + cy + '" r="18" fill="' + INK + '"/>';
      else if (f > 0) b += wedge(cx, cy, 18, 0, 0, f * Math.PI * 2, INK);
      b += text(cx + 28, cy - 3, clip(r.label, cw - 60, 12), { size: 12, bold: true }) + text(cx + 28, cy + 14, r.done + ' / ' + r.total + (r.note ? ' · ' + r.note : ''), { size: 11, fill: GRAY });
    });
    return svg(56, b, '항목군별 확보 정도');
  }
  // 포지셔닝 맵: [{ label, importance, urgency }] — 상 · 중 · 하를 좌표로. 값을 읽지 못한 줄은 그리지 않는다
  function positioning(rows) {
    var lv = function (v) { v = String(v || ''); return /상|높|긴급|high/i.test(v) ? 2 : /중|보통|mid/i.test(v) ? 1 : /하|낮|low/i.test(v) ? 0 : -1; };
    var pts = [], at = {};
    (rows || []).forEach(function (r) { var a = lv(r.importance), u = lv(r.urgency); if (a >= 0 && u >= 0 && r.label) pts.push({ r: r, a: a, u: u }); });
    if (pts.length < 2) return '';
    var x0 = 40, y0 = 8, pw = 300, ph = 198, b = rect(x0 + pw / 2, y0, pw / 2, ph / 2, { fill: PALE }) + rect(x0 + 0.5, y0 + 0.5, pw - 1, ph - 1, { stroke: INK })
      + line(x0 + pw / 2, y0, x0 + pw / 2, y0 + ph, { dash: '4 3' }) + line(x0, y0 + ph / 2, x0 + pw, y0 + ph / 2, { dash: '4 3' })
      + text(x0 + pw - 6, y0 + 14, '먼저 해결', { size: 10.5, bold: true, anchor: 'end', fill: ORANGE }) + text(x0 + 6, y0 + ph - 6, '지켜봄', { size: 10.5, fill: GRAY })
      + text(x0 + pw / 2, y0 + ph + 16, '시급성 →', { size: 10.5, bold: true, anchor: 'middle' })
      + '<text x="14" y="' + (y0 + ph / 2) + '" font-size="10.5" font-weight="700" fill="' + INK + '" text-anchor="middle" transform="rotate(-90 14 ' + (y0 + ph / 2) + ')">중요도 →</text>';
    pts.forEach(function (p, i) {
      var k = p.a + '-' + p.u, n = at[k] = (at[k] || 0) + 1;
      var cx = x0 + (p.u + 0.5) * pw / 3 + ((n - 1) % 3 - (n > 1 ? 1 : 0)) * 22, cy = y0 + (2 - p.a + 0.5) * ph / 3 + Math.floor((n - 1) / 3) * 22;
      var hot = p.a === 2 && p.u === 2;
      b += '<circle cx="' + r1(cx) + '" cy="' + r1(cy) + '" r="10" fill="' + (hot ? ORANGE : INK) + '"/>' + text(cx, cy + 4, i + 1, { size: 10.5, bold: true, anchor: 'middle', fill: '#fff' });
      if (i < 11) b += text(x0 + pw + 22, y0 + 12 + i * 17, clip((i + 1) + '. ' + p.r.label, W - x0 - pw - 24, 11), { size: 11, bold: hot });
    });
    return svg(y0 + ph + 24, b, '중요도와 시급성 포지셔닝 맵');
  }

  // 구성비는 100% 누적 막대로(조각이 넷 이상인 원 그래프는 쓰지 않는다). 강조할 한 곳만 주황, 나머지는 회색 계열
  // rows: [{ name, value, hot }], o: { title, unit }
  function stack100(rows, o) {
    o = o || {};
    rows = (rows || []).filter(function (r) { return r && num(r.value) > 0; });
    var sum = rows.reduce(function (t, r) { return t + num(r.value); }, 0);
    if (!sum) return '';
    var grays = [NAVY, '#8E8E8E', '#C1C7CD', '#5f6b7a', '#aab1ba', '#dfe3e8'], gi = 0, x = 0, top = o.title ? 22 : 2, bh = 26;
    var b = o.title ? text(0, 12, o.title, { size: 11.5, bold: true }) + (o.unit ? text(W, 12, o.unit, { size: 10.5, fill: GRAY, anchor: 'end' }) : '') : '';
    var lx = 0, ly = top + bh + 16;
    rows.forEach(function (r) {
      var w = num(r.value) / sum * W, fill = r.hot ? '#FF8000' : grays[gi++ % grays.length], pct = Math.round(num(r.value) / sum * 100), light = fill === '#C1C7CD' || fill === '#dfe3e8' || fill === '#aab1ba';
      b += rect(x, top, w, bh, { fill: fill, stroke: '#fff' });
      if (w > 34) b += text(x + w / 2, top + 17, pct + '%', { size: 11, bold: true, anchor: 'middle', fill: light ? INK : '#fff' });
      var label = r.name + ' ' + (o.count ? r.value + o.count + ' · ' : '') + pct + '%', lw = 16 + tw(label, 11) + 14;
      if (lx + lw > W) { lx = 0; ly += 17; }
      b += rect(lx, ly - 9, 10, 10, { fill: fill }) + text(lx + 15, ly, label, { size: 11, bold: !!r.hot });
      lx += lw; x += w;
    });
    return svg(ly + 8, b, o.title || '구성비');
  }

  // 항목끼리 비교는 가로 막대, 값이 큰 순서로. 강조할 한 곳만 주황
  // rows: [{ name, value, hot }], o: { title, unit }
  function hbar(rows, o) {
    o = o || {};
    rows = (rows || []).filter(function (r) { return r && num(r.value) > 0; }).sort(function (a, b) { return num(b.value) - num(a.value); });
    if (!rows.length) return '';
    var max = num(rows[0].value), lw = 150, top = o.title ? 24 : 4, rh = 22, bw = W - lw - 60;
    var b = o.title ? text(0, 13, o.title, { size: 11.5, bold: true }) : '';
    rows.forEach(function (r, i) {
      var y = top + i * rh, w = Math.max(2, num(r.value) / max * bw);
      b += text(lw - 8, y + 14, clip(r.name, lw - 10, 11.5), { size: 11.5, anchor: 'end', bold: !!r.hot })
        + rect(lw, y + 3, w, rh - 8, { fill: r.hot ? '#FF8000' : '#C1C7CD' })
        + text(lw + w + 6, y + 14, r.value + (o.unit || ''), { size: 11, bold: !!r.hot });
    });
    b += line(lw, top, lw, top + rows.length * rh, { stroke: INK });
    return svg(top + rows.length * rh + 4, b, o.title || '항목 비교');
  }

  var api = { hbar: hbar, stack100: stack100, donut: donut, harvey: harvey, positioning: positioning, flow: flow, goals: goals, reqs: reqs, topics: topics, matrix: positioning, completeness: completeness, trend: trend, orgMap: orgMap, schedule: schedule, dueline: dueline, share: share, ROLES: ROLES };
  if (typeof window !== 'undefined') window.SalesCharts = api;
  else if (typeof module !== 'undefined' && module.exports) module.exports = api;
})();
