/* 노드 그래프: 한 프로젝트의 일의 흐름과 관계를 그린다. 의존성 없는 파일 하나.
 *
 *   SalesGraph.mount(container, data, { mode: 'flow' | 'relation', onOpen(node) })
 *     → { setMode(mode), setData(data), destroy() }
 *
 *   flow(워크플로) : 고객 → 프로젝트 → 회의 → 회의록 → 결과지 → 항목을 왼쪽에서 오른쪽으로. 회의마다 세 문 가운데 어디에 서 있는지가 보인다.
 *   relation(관계) : 사람 · 회의 · 액션 · 요구사항 · 항목이 어떻게 이어지는지를 힘 기반 배치로.
 *
 * data는 /api/graph(agent/flow.py)의 {nodes, edges}. 스타일은 한 번 주입하고 모든 클래스에 sg- 를 붙인다.
 */
(function () {
  'use strict';

  const ACCENT = '#ff7f00';
  // 종류: 이름, 색, 워크플로에서의 세로줄(없으면 흐름 밖 — 같은 줄 아래쪽에 둔다)
  const TYPES = {
    customer:    { label: '고객',     color: '#8ab4f8', col: 0 },
    project:     { label: '프로젝트', color: '#7dcfb6', col: 1 },
    meeting:     { label: '회의',     color: '#f6c177', col: 2 },
    minutes:     { label: '회의록',   color: '#c4a7e7', col: 3 },
    sheet:       { label: '결과지',   color: '#e6e9ef', col: 4 },
    item:        { label: '항목',     color: '#5bd08a', col: 5 },
    person:      { label: '사람',     color: '#f28fad', under: 2 },
    action:      { label: '액션',     color: '#9aa8c7', under: 3 },
    requirement: { label: '요구사항', color: '#89dceb', under: 4 },
  };
  const ITEM_COLOR = { confirmed: '#5bd08a', draft: '#f6c177', missing: '#5b6573' };
  const STATE_LABEL = { done: '지나옴', now: '지금 여기', todo: '아직', confirmed: '확정', draft: 'AI 초안', missing: '미확보', open: '진행 중' };
  const HIDDEN_IN_FLOW = ['person', 'action', 'requirement']; // 워크플로에서는 흐름만 먼저 보여 준다(범례에서 켠다)
  const CARD_W = 188, CARD_H = 44, COL_W = 252, ROW_H = 58;

  const colorOf = n => (n.type === 'item' ? ITEM_COLOR[n.state] : TYPES[n.type].color) || '#9aa8c7';
  const cut = (s, n) => { s = String(s || ''); return s.length > n ? s.slice(0, n) + '…' : s; };
  const stateLabel = n => (n.type === 'action' && n.state === 'done' ? '완료' : STATE_LABEL[n.state] || '');

  // ── 배치 계산(화면 없이도 돌아간다) ─────────────────────────────────────────
  /** 워크플로 배치: 세로줄마다 위아래로 쌓고 가운데를 맞춘다. 흐름 밖의 것은 그 아래에. {id: {x, y}} */
  function layoutFlow(nodes) {
    const pos = {}, cols = {}, extra = {};
    for (const n of nodes) {
      const t = TYPES[n.type] || {};
      if (t.col !== undefined) (cols[t.col] = cols[t.col] || []).push(n);
      else (extra[t.under === undefined ? 5 : t.under] = extra[t.under === undefined ? 5 : t.under] || []).push(n);
    }
    let bottom = 0;
    for (const c of Object.keys(cols)) {
      const list = cols[c], h = (list.length - 1) * ROW_H;
      list.forEach((n, i) => { pos[n.id] = { x: c * COL_W, y: i * ROW_H - h / 2 }; });
      bottom = Math.max(bottom, h / 2);
    }
    for (const c of Object.keys(extra)) extra[c].forEach((n, i) => { pos[n.id] = { x: c * COL_W, y: bottom + 96 + i * ROW_H }; });
    return pos;
  }

  /** 힘 기반 배치의 첫 자리: 종류별로 원 위에 고르게(실행마다 같은 그림이 나오도록 난수를 쓰지 않는다). */
  function forceInit(nodes) {
    const pos = {};
    nodes.forEach((n, i) => {
      const a = i * 2.399963, r = 60 + 22 * Math.sqrt(i); // 황금각 나선
      pos[n.id] = { x: Math.cos(a) * r, y: Math.sin(a) * r, vx: 0, vy: 0 };
    });
    return pos;
  }

  /** 힘 기반 배치 한 걸음: 서로 밀어내고(반발), 이어진 것끼리 당기고(스프링), 가운데로 모은다. 움직인 양을 돌려준다. */
  function forceStep(nodes, edges, pos, alpha) {
    const REPULSE = 21000, REST = 120, SPRING = 0.05, GRAVITY = 0.01, DAMP = 0.82; // 이름표가 겹치지 않을 만큼 벌린다
    for (let i = 0; i < nodes.length; i++) {
      const a = pos[nodes[i].id];
      for (let j = i + 1; j < nodes.length; j++) {
        const b = pos[nodes[j].id];
        let dx = a.x - b.x, dy = a.y - b.y;
        if (!dx && !dy) { dx = 0.5 + i * 0.01; dy = 0.5 - j * 0.01; } // 겹친 것은 살짝 떼어 놓는다
        const d2 = Math.max(dx * dx + dy * dy, 64), d = Math.sqrt(d2), f = REPULSE / d2;
        a.vx += dx / d * f; a.vy += dy / d * f; b.vx -= dx / d * f; b.vy -= dy / d * f;
      }
    }
    for (const e of edges) {
      const a = pos[e.src], b = pos[e.dst];
      if (!a || !b) continue;
      const dx = b.x - a.x, dy = b.y - a.y, d = Math.sqrt(dx * dx + dy * dy) || 1;
      const f = (d - (e.kind === 'flow' ? REST * 0.9 : REST)) * SPRING;
      a.vx += dx / d * f; a.vy += dy / d * f; b.vx -= dx / d * f; b.vy -= dy / d * f;
    }
    let moved = 0;
    for (const n of nodes) {
      const p = pos[n.id];
      p.vx = (p.vx - p.x * GRAVITY) * DAMP; p.vy = (p.vy - p.y * GRAVITY) * DAMP;
      if (p.pinned) { p.vx = p.vy = 0; continue; }
      p.x += p.vx * alpha; p.y += p.vy * alpha;
      moved += Math.abs(p.vx) + Math.abs(p.vy);
    }
    return moved;
  }

  // ── 스타일 ────────────────────────────────────────────────────────────────
  const CSS = `
  .sg-root { position:relative; overflow:hidden; background:#0f1318; border-radius:10px; color:#c9d1dc; font-size:12px; user-select:none; min-height:320px; }
  .sg-svg { display:block; width:100%; height:100%; cursor:grab; touch-action:none; }
  .sg-svg.sg-panning { cursor:grabbing; }
  .sg-node { cursor:pointer; transition:opacity .15s; }
  .sg-node text { fill:#d6dce6; pointer-events:none; font-family:inherit; }
  .sg-node .sg-sub { fill:#8793a6; font-size:10.5px; }
  .sg-node .sg-name { font-size:12px; font-weight:600; }
  .sg-node.sg-rel .sg-name { font-size:10.5px; font-weight:500; text-anchor:middle; }
  .sg-shape { stroke-width:1.4; transition:stroke .15s, stroke-width .15s; }
  .sg-node.sg-todo, .sg-node.sg-missing { opacity:.62; } .sg-node.sg-todo .sg-shape, .sg-node.sg-missing .sg-shape { stroke-dasharray:4 3; }
  .sg-node.sg-now .sg-shape { stroke:${ACCENT}; stroke-width:2; animation:sg-pulse 1.6s ease-in-out infinite; }
  .sg-node.sg-sel .sg-shape, .sg-node:hover .sg-shape { stroke:${ACCENT}; stroke-width:2.4; }
  .sg-node.sg-dim { opacity:.14; }
  .sg-edge { fill:none; stroke:rgba(160,175,195,.2); stroke-width:1; transition:opacity .15s, stroke .15s; }
  .sg-edge.sg-flow { stroke:#5d6b80; stroke-width:1.8; }
  .sg-edge.sg-flow.sg-todo { stroke:#3c4655; stroke-dasharray:5 4; }
  .sg-edge.sg-flow.sg-now { stroke:${ACCENT}; stroke-dasharray:6 5; animation:sg-march 1s linear infinite; }
  .sg-edge.sg-faint { stroke:rgba(160,175,195,.07); }
  .sg-edge.sg-lit { stroke:${ACCENT}; stroke-width:1.8; opacity:1; }
  .sg-edge.sg-dim { opacity:.08; }
  @keyframes sg-pulse { 0%,100% { stroke-opacity:1; } 50% { stroke-opacity:.35; } }
  @keyframes sg-march { to { stroke-dashoffset:-22; } }
  .sg-bar, .sg-legend, .sg-hint { position:absolute; display:flex; gap:6px; align-items:center; flex-wrap:wrap; }
  .sg-bar { top:12px; left:12px; } .sg-legend { bottom:12px; left:12px; max-width:70%; } .sg-hint { bottom:14px; right:14px; color:#5d6b80; font-size:11px; pointer-events:none; }
  .sg-btn, .sg-chip, .sg-find { font:inherit; color:#c9d1dc; background:rgba(26,32,41,.92); border:1px solid #2a3340; border-radius:7px; padding:5px 10px; }
  .sg-btn { cursor:pointer; } .sg-btn:hover { border-color:#4a586b; } .sg-btn.sg-on { background:${ACCENT}; border-color:${ACCENT}; color:#fff; font-weight:600; }
  .sg-seg { display:flex; } .sg-seg .sg-btn { border-radius:0; margin-left:-1px; } .sg-seg .sg-btn:first-child { border-radius:7px 0 0 7px; margin-left:0; } .sg-seg .sg-btn:last-child { border-radius:0 7px 7px 0; }
  .sg-find { width:150px; outline:none; cursor:text; } .sg-find:focus { border-color:${ACCENT}; } .sg-find::placeholder { color:#5d6b80; }
  .sg-chip { cursor:pointer; display:inline-flex; align-items:center; gap:6px; padding:3px 9px; font-size:11px; border-radius:999px; }
  .sg-chip i { width:8px; height:8px; border-radius:50%; } .sg-chip.sg-off { opacity:.4; text-decoration:line-through; }
  .sg-panel { position:absolute; top:12px; right:12px; bottom:12px; width:340px; background:rgba(20,25,32,.96); border:1px solid #2a3340; border-radius:10px; padding:14px; overflow:auto; display:none; }
  .sg-panel.sg-open { display:block; }
  .sg-panel h4 { margin:6px 0 4px; font-size:14px; color:#eef1f5; word-break:keep-all; } .sg-panel p { margin:0 0 8px; color:#9aa6b6; line-height:1.5; word-break:keep-all; overflow-wrap:anywhere; }
  .sg-tag { display:inline-block; font-size:10.5px; padding:1px 7px; border-radius:999px; border:1px solid #2a3340; margin-right:4px; }
  .sg-panel h5 { margin:12px 0 4px; font-size:11px; color:#5d6b80; font-weight:600; }
  .sg-link { display:flex; gap:6px; padding:4px 6px; border-radius:6px; cursor:pointer; color:#c9d1dc; line-height:1.4; } .sg-link:hover { background:#1c232d; }
  .sg-link small { color:#5d6b80; flex:none; } .sg-x { position:absolute; top:8px; right:10px; background:none; border:none; color:#5d6b80; font-size:16px; cursor:pointer; }
  .sg-open-btn { margin-top:12px; width:100%; }
  `;
  function injectStyle() {
    if (document.getElementById('sg-style')) return;
    const s = document.createElement('style');
    s.id = 'sg-style'; s.textContent = CSS;
    document.head.appendChild(s);
  }
  const SVG = 'http://www.w3.org/2000/svg';
  const svg = (tag, attrs) => { const e = document.createElementNS(SVG, tag); for (const k in attrs) e.setAttribute(k, attrs[k]); return e; };
  const html = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined) e.textContent = text; return e; };

  // ── 화면 ──────────────────────────────────────────────────────────────────
  function mount(container, data, opts) {
    opts = opts || {};
    injectStyle();
    container.classList.add('sg-root');
    container.innerHTML = '';
    let mode = opts.mode === 'relation' ? 'relation' : 'flow';
    let nodes = [], edges = [], byId = {}, pos = {}, off = new Set();
    let view = { x: 0, y: 0, k: 1 }, selected = null, hovered = null, query = '', raf = 0, alpha = 0;
    const nodeEl = {}, edgeEls = [];

    const root = svg('svg', { class: 'sg-svg' });
    const defs = svg('defs', {});
    const grid = svg('pattern', { id: 'sg-grid', width: 26, height: 26, patternUnits: 'userSpaceOnUse' });
    grid.appendChild(svg('circle', { cx: 1.2, cy: 1.2, r: 1.1, fill: 'rgba(140,155,175,.13)' }));
    const arrow = svg('marker', { id: 'sg-arrow', viewBox: '0 0 8 8', refX: 7, refY: 4, markerWidth: 7, markerHeight: 7, orient: 'auto' });
    arrow.appendChild(svg('path', { d: 'M0 0L8 4L0 8z', fill: '#5d6b80' }));
    defs.appendChild(grid); defs.appendChild(arrow); root.appendChild(defs);
    const bg = svg('rect', { width: '100%', height: '100%', fill: 'url(#sg-grid)' });
    const world = svg('g', {}), edgeLayer = svg('g', {}), nodeLayer = svg('g', {});
    world.appendChild(edgeLayer); world.appendChild(nodeLayer); root.appendChild(bg); root.appendChild(world);
    container.appendChild(root);

    // 위 막대: 보기 전환 · 찾기 · 맞춤
    const bar = html('div', 'sg-bar'), seg = html('div', 'sg-seg');
    const modeBtn = { flow: html('button', 'sg-btn', '업무 흐름'), relation: html('button', 'sg-btn', '관계 보기') };
    seg.appendChild(modeBtn.flow); seg.appendChild(modeBtn.relation);
    const find = html('input', 'sg-find'); find.placeholder = '고객·프로젝트·담당자 찾기';
    const fitBtn = html('button', 'sg-btn', '전체 보기');
    bar.appendChild(seg); bar.appendChild(find); bar.appendChild(fitBtn);
    const legend = html('div', 'sg-legend'), panel = html('div', 'sg-panel');
    const hint = html('div', 'sg-hint', '휠로 확대 · 빈 곳을 끌어 이동 · 항목을 두 번 눌러 상세 보기');
    container.appendChild(bar); container.appendChild(legend); container.appendChild(hint); container.appendChild(panel);

    const visible = n => !off.has(n.type);
    const applyView = () => world.setAttribute('transform', `translate(${view.x},${view.y}) scale(${view.k})`);
    const degree = id => edges.reduce((c, e) => c + (e.src === id || e.dst === id ? 1 : 0), 0);
    const radius = n => (n.type === 'customer' || n.type === 'project' || n.type === 'sheet' ? 15 : 6 + Math.min(9, degree(n.id) * 0.8));

    function setData(d) {
      nodes = (d && d.nodes || []).filter(n => TYPES[n.type]);
      byId = {}; nodes.forEach(n => { byId[n.id] = n; });
      edges = (d && d.edges || []).filter(e => byId[e.src] && byId[e.dst]);
      selected = selected && byId[selected] ? selected : null;
      relayout(true);
    }

    function setMode(m) {
      mode = m === 'relation' ? 'relation' : 'flow';
      off = new Set(mode === 'flow' ? HIDDEN_IN_FLOW : []);
      relayout(true);
    }

    /** 보이는 것들의 자리를 다시 잡고 처음부터 그린다. */
    function relayout(fitAfter) {
      cancelAnimationFrame(raf);
      modeBtn.flow.classList.toggle('sg-on', mode === 'flow'); modeBtn.relation.classList.toggle('sg-on', mode === 'relation');
      const shown = nodes.filter(visible), shownEdges = edges.filter(e => visible(byId[e.src]) && visible(byId[e.dst]));
      if (mode === 'flow') pos = layoutFlow(shown);
      else {
        pos = forceInit(shown);
        for (let i = 0; i < 220; i++) forceStep(shown, shownEdges, pos, 1 - i / 260); // 처음 보일 때 이미 자리가 잡혀 있게
        alpha = 0.25; cool(shown, shownEdges);
      }
      build(shown, shownEdges);
      renderLegend();
      if (fitAfter) fit();
      paint();
    }

    function cool(shown, shownEdges) {
      const tick = () => {
        alpha *= 0.97;
        const moved = forceStep(shown, shownEdges, pos, alpha);
        place();
        if (alpha > 0.02 && moved > 0.5) raf = requestAnimationFrame(tick);
      };
      raf = requestAnimationFrame(tick);
    }

    /** 노드와 선의 SVG 요소를 만든다. 자리는 place()가 넣는다. */
    function build(shown, shownEdges) {
      edgeLayer.innerHTML = ''; nodeLayer.innerHTML = ''; edgeEls.length = 0;
      for (const k in nodeEl) delete nodeEl[k];
      for (const e of shownEdges) {
        const el = svg('path', { class: 'sg-edge' + (e.kind === 'flow' ? ` sg-flow sg-${e.state || 'done'}` : mode === 'flow' ? ' sg-faint' : '') });
        if (e.kind === 'flow' && mode === 'flow') el.setAttribute('marker-end', 'url(#sg-arrow)');
        edgeLayer.appendChild(el); edgeEls.push({ e, el });
      }
      for (const n of shown) {
        const g = svg('g', { class: `sg-node sg-${mode === 'flow' ? 'card' : 'rel'}${n.state ? ' sg-' + n.state : ''}` });
        const c = colorOf(n), solid = n.state === 'done' || n.state === 'confirmed' || !n.state;
        const title = svg('title', {}); title.textContent = `${n.label}${n.sub ? '\n' + n.sub : ''}`; g.appendChild(title);
        if (mode === 'flow') {
          g.appendChild(svg('rect', { class: 'sg-shape', x: -CARD_W / 2, y: -CARD_H / 2, width: CARD_W, height: CARD_H, rx: 8, fill: c, 'fill-opacity': solid ? 0.2 : 0.07, stroke: c }));
          g.appendChild(svg('rect', { x: -CARD_W / 2, y: -CARD_H / 2, width: 4, height: CARD_H, rx: 2, fill: c, 'pointer-events': 'none' }));
          const name = svg('text', { class: 'sg-name', x: -CARD_W / 2 + 14, y: n.sub ? -3 : 4 }); name.textContent = cut(n.label, 14); g.appendChild(name);
          if (n.sub) { const sub = svg('text', { class: 'sg-sub', x: -CARD_W / 2 + 14, y: 13 }); sub.textContent = cut(n.sub, 17); g.appendChild(sub); }
        } else {
          const r = radius(n);
          if (n.type === 'minutes' || n.type === 'sheet' || n.type === 'requirement')
            g.appendChild(svg('rect', { class: 'sg-shape', x: -r, y: -r, width: r * 2, height: r * 2, rx: 4, fill: c, 'fill-opacity': solid ? 0.85 : 0.25, stroke: c }));
          else g.appendChild(svg('circle', { class: 'sg-shape', r, fill: c, 'fill-opacity': solid ? 0.85 : 0.25, stroke: c }));
          const name = svg('text', { class: 'sg-name', y: r + 13 }); name.textContent = cut(n.label, 12); g.appendChild(name);
        }
        g.addEventListener('pointerdown', ev => startDrag(ev, n));
        g.addEventListener('pointerenter', () => { hovered = n.id; paint(); });
        g.addEventListener('pointerleave', () => { hovered = null; paint(); });
        g.addEventListener('dblclick', ev => { ev.stopPropagation(); if (opts.onOpen) opts.onOpen(n); });
        nodeLayer.appendChild(g); nodeEl[n.id] = g;
      }
      place();
    }

    /** 노드를 제자리에 놓고 선을 그 사이에 긋는다. */
    function place() {
      for (const id in nodeEl) nodeEl[id].setAttribute('transform', `translate(${pos[id].x},${pos[id].y})`);
      for (const { e, el } of edgeEls) {
        const a = pos[e.src], b = pos[e.dst];
        if (mode === 'flow' && e.kind === 'flow') { // 카드의 오른쪽 변에서 다음 카드의 왼쪽 변으로 부드럽게
          const x1 = a.x + CARD_W / 2, x2 = b.x - CARD_W / 2 - 3, mx = (x1 + x2) / 2;
          el.setAttribute('d', `M${x1} ${a.y}C${mx} ${a.y} ${mx} ${b.y} ${x2} ${b.y}`);
        } else if (mode === 'flow') {
          const my = (a.y + b.y) / 2, bend = Math.abs(a.x - b.x) < 1 ? 70 : 0; // 같은 세로줄이면 옆으로 휘게
          el.setAttribute('d', `M${a.x} ${a.y}Q${(a.x + b.x) / 2 - bend} ${my} ${b.x} ${b.y}`);
        } else el.setAttribute('d', `M${a.x} ${a.y}L${b.x} ${b.y}`);
      }
    }

    /** 무엇을 밝히고 무엇을 흐리게 할지: 올린 노드(없으면 고른 노드)와 이어진 것, 그리고 찾기에 맞는 것. */
    function paint() {
      const focus = hovered || selected, near = new Set(), q = query.trim().toLowerCase();
      if (focus) { near.add(focus); edges.forEach(e => { if (e.src === focus) near.add(e.dst); if (e.dst === focus) near.add(e.src); }); }
      const hit = n => !q || (n.label + ' ' + n.sub).toLowerCase().includes(q);
      for (const id in nodeEl) {
        const n = byId[id];
        nodeEl[id].classList.toggle('sg-dim', (focus ? !near.has(id) : false) || !hit(n));
        nodeEl[id].classList.toggle('sg-sel', id === selected);
      }
      for (const { e, el } of edgeEls) {
        const lit = focus && (e.src === focus || e.dst === focus);
        el.classList.toggle('sg-lit', !!lit);
        el.classList.toggle('sg-dim', (focus ? !lit : false) || (!!q && !(hit(byId[e.src]) && hit(byId[e.dst]))));
      }
    }

    function renderLegend() {
      legend.innerHTML = '';
      const present = new Set(nodes.map(n => n.type));
      Object.keys(TYPES).filter(t => present.has(t)).forEach(t => {
        const chip = html('button', 'sg-chip' + (off.has(t) ? ' sg-off' : ''));
        const dot = html('i'); dot.style.background = TYPES[t].color;
        chip.appendChild(dot); chip.appendChild(document.createTextNode(`${TYPES[t].label} ${nodes.filter(n => n.type === t).length}`));
        chip.title = '눌러서 켜고 끈다';
        chip.onclick = () => { off.has(t) ? off.delete(t) : off.add(t); if (selected && off.has(byId[selected].type)) select(null); relayout(mode === 'flow'); };
        legend.appendChild(chip);
      });
    }

    function select(id) {
      selected = id && byId[id] ? id : null;
      panel.classList.toggle('sg-open', !!selected);
      panel.innerHTML = '';
      if (selected) {
        const n = byId[selected], x = html('button', 'sg-x', '×');
        x.onclick = () => select(null); panel.appendChild(x);
        const tag = html('span', 'sg-tag', TYPES[n.type].label); tag.style.color = colorOf(n); tag.style.borderColor = colorOf(n); panel.appendChild(tag);
        if (stateLabel(n)) panel.appendChild(html('span', 'sg-tag', stateLabel(n)));
        panel.appendChild(html('h4', '', n.label));
        if (n.sub) panel.appendChild(html('p', '', n.sub));
        if (opts.preview) { const pv = html('div', 'sg-preview'); panel.appendChild(pv); opts.preview(n, pv); }
        const links = edges.filter(e => e.src === n.id || e.dst === n.id);
        if (links.length) panel.appendChild(html('h5', '', `이어진 것 ${links.length}`));
        links.forEach(e => {
          const other = byId[e.src === n.id ? e.dst : e.src], row = html('div', 'sg-link');
          row.appendChild(html('small', '', e.rel || (e.kind === 'flow' ? '흐름' : '관계')));
          row.appendChild(html('span', '', other.label));
          row.onclick = () => { if (!visible(other)) { off.delete(other.type); relayout(false); } select(other.id); };
          panel.appendChild(row);
        });
        const open = html('button', 'sg-btn sg-on sg-open-btn', '상세 보기');
        open.onclick = () => { if (opts.onOpen) opts.onOpen(n); };
        panel.appendChild(open);
      }
      paint();
    }

    /** 보이는 것이 모두 들어오게 맞춘다. */
    function fit() {
      const ids = Object.keys(nodeEl), h = container.clientHeight || 500;
      const w = (container.clientWidth || 800) - (selected ? 290 : 0); // 상세 패널이 열려 있으면 그 왼쪽에 맞춘다
      if (!ids.length) { view = { x: w / 2, y: h / 2, k: 1 }; applyView(); return; }
      const px = mode === 'flow' ? CARD_W / 2 : 40, py = mode === 'flow' ? CARD_H / 2 : 30;
      const xs = ids.map(i => pos[i].x), ys = ids.map(i => pos[i].y);
      const x0 = Math.min(...xs) - px, x1 = Math.max(...xs) + px, y0 = Math.min(...ys) - py, y1 = Math.max(...ys) + py;
      const k = Math.max(0.2, Math.min(1.25, (w - 60) / (x1 - x0 || 1), (h - 120) / (y1 - y0 || 1))); // 위아래 막대 자리를 남긴다
      view = { k, x: w / 2 - (x0 + x1) / 2 * k, y: h / 2 - (y0 + y1) / 2 * k };
      applyView();
    }

    // ── 끌기: 빈 곳은 화면 이동, 노드는 그 노드 이동(거의 안 움직였으면 고르기) ──
    let drag = null;
    function startDrag(ev, n) {
      ev.stopPropagation();
      drag = { n, x: ev.clientX, y: ev.clientY, moved: 0 };
      root.setPointerCapture && root.setPointerCapture(ev.pointerId);
    }
    function onDown(ev) { if (!drag) { drag = { x: ev.clientX, y: ev.clientY, moved: 0 }; root.classList.add('sg-panning'); } }
    function onMove(ev) {
      if (!drag) return;
      const dx = ev.clientX - drag.x, dy = ev.clientY - drag.y;
      drag.x = ev.clientX; drag.y = ev.clientY; drag.moved += Math.abs(dx) + Math.abs(dy);
      if (drag.n) {
        const p = pos[drag.n.id];
        p.x += dx / view.k; p.y += dy / view.k;
        if (mode === 'relation') p.pinned = true; // 놓은 자리에 둔다
        place();
      } else { view.x += dx; view.y += dy; applyView(); }
    }
    function onUp() {
      if (!drag) return;
      if (drag.n && drag.moved < 5) select(drag.n.id);
      else if (!drag.n && drag.moved < 5) select(null);
      drag = null; root.classList.remove('sg-panning');
    }
    function onWheel(ev) {
      ev.preventDefault();
      const r = root.getBoundingClientRect(), cx = ev.clientX - r.left, cy = ev.clientY - r.top;
      const k = Math.max(0.2, Math.min(3, view.k * Math.exp(-ev.deltaY * 0.0015)));
      view.x = cx - (cx - view.x) * k / view.k; view.y = cy - (cy - view.y) * k / view.k; view.k = k; // 커서 아래 지점이 제자리에 있게
      applyView();
    }
    const onKey = ev => { if (ev.key === 'Escape') { find.value = ''; query = ''; select(null); } };

    root.addEventListener('pointerdown', onDown);
    window.addEventListener('pointermove', onMove);
    window.addEventListener('pointerup', onUp);
    root.addEventListener('wheel', onWheel, { passive: false });
    document.addEventListener('keydown', onKey);
    modeBtn.flow.onclick = () => setMode('flow');
    modeBtn.relation.onclick = () => setMode('relation');
    fitBtn.onclick = fit;
    find.oninput = () => { query = find.value; paint(); };
    const ro = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(() => fit()) : null;
    if (ro) ro.observe(container);

    off = new Set(mode === 'flow' ? HIDDEN_IN_FLOW : []);
    setData(data);

    return {
      setMode, setData,
      destroy() {
        cancelAnimationFrame(raf);
        if (ro) ro.disconnect();
        window.removeEventListener('pointermove', onMove);
        window.removeEventListener('pointerup', onUp);
        document.removeEventListener('keydown', onKey);
        container.innerHTML = ''; container.classList.remove('sg-root');
      },
    };
  }

  const api = { mount, layoutFlow, forceInit, forceStep };
  if (typeof window !== 'undefined') window.SalesGraph = api;
  else if (typeof module !== 'undefined' && module.exports) module.exports = api; // 배치 계산을 Node에서 시험할 때
})();
