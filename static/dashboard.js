// Interactive Packets dashboard: live updates, instant search and sort, charts, row actions,
// a preview panel and bulk actions. Without this script the server-rendered list still works.
(function () {
  const board = document.getElementById('board');
  if (!board || !window.fetch) return;
  const cfg = board.dataset;
  const csrf = (document.querySelector('meta[name=csrf]') || {}).content || '';
  const FAB = cfg.edition === 'fab', MANAGER = cfg.manager === '1';
  const mac = /Mac|iPhone|iPad/.test(navigator.platform);
  const SVGNS = 'http://www.w3.org/2000/svg';

  const state = {
    data: null, version: '', tab: new URLSearchParams(location.search).get('show') || 'all',
    q: (document.getElementById('q') || {}).value || '', rep: '', by: '', status: '', week: null,
    sort: { key: 'when', dir: -1 }, selected: new Set(), flash: new Set(),
  };
  const repSel = document.getElementById('rep'), bySel = document.getElementById('by');
  if (repSel) { state.rep = repSel.value; repSel.onchange = () => { state.rep = repSel.value; render(); }; }
  if (bySel) { state.by = bySel.value; bySel.onchange = () => { state.by = bySel.value; render(); }; }
  if (!state.by) state.by = new URLSearchParams(location.search).get('by') || '';   // from a Team card

  // ---------------------------------------------------------------- helpers
  function el(tag, attrs, ...kids) {
    const n = tag.startsWith('svg:') ? document.createElementNS(SVGNS, tag.slice(4)) : document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === 'class') n.setAttribute('class', v);
      else if (k.startsWith('on')) n.addEventListener(k.slice(2), v);
      else if (k === 'text') n.textContent = v;
      else n.setAttribute(k, v === true ? '' : v);
    }
    kids.flat().forEach((c) => { if (c !== null && c !== undefined && c !== false) n.append(c.nodeType ? c : document.createTextNode(String(c))); });
    return n;
  }
  function toast(html, ms) {
    let t = document.getElementById('db-toast');
    if (!t) { t = el('div', { id: 'db-toast', role: 'status' }); document.body.append(t); t.onclick = () => { t.hidden = true; }; }
    t.innerHTML = html; t.hidden = false; clearTimeout(t._h); t._h = setTimeout(() => { t.hidden = true; }, ms || 6000);
  }
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const post = (url, body) => fetch(url, { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf }, body: JSON.stringify(body) });
  const isOpen = (p) => ['sent', 'viewed', 'in_progress', 'draft'].includes(p.state);
  const t = (iso) => (iso ? Date.parse(iso) : 0);

  // ---------------------------------------------------------------- filtering & sorting
  function inTab(p) {
    if (state.tab === 'archived') return p.archived;
    if (p.archived && !state.q) return false;
    switch (state.tab) {
      case 'open': return isOpen(p);
      case 'awaiting': return p.state === 'sent';
      case 'overdue': return p.deadline.overdue;
      case 'completed': return p.state === 'completed';
      default: return true;
    }
  }
  function matches(p) {
    if (!inTab(p)) return false;
    if (state.rep && p.rep !== state.rep) return false;
    if (state.by && String(p.by_id) !== state.by) return false;
    if (state.status) {
      const s = state.status;
      if (s === 'awaiting' && p.state !== 'sent') return false;
      if (s === 'opened' && !['viewed', 'in_progress'].includes(p.state)) return false;
      if (s === 'overdue' && !p.deadline.overdue) return false;
      if (s === 'signed' && p.state !== 'completed') return false;
      if (s === 'cancelled' && p.state !== 'voided') return false;
    }
    if (state.week) {
      const a = t(state.week.start), b = t(state.week.end);
      const sent = t(p.sent_at), done = t(p.completed_at);
      if (!((sent >= a && sent < b) || (done >= a && done < b))) return false;
    }
    const q = state.q.trim().toLowerCase();
    if (q) {
      const bare = q.replace(/^so#?\s*/, '');
      const hay = [p.client, p.email, p.so, p.rep, p.by, ...(p.forms || [])].join(' ').toLowerCase();
      if (!q.split(/\s+/).every((w) => hay.includes(w)) && !(bare && (p.so || '').toLowerCase().replace(/^so#?-?/, '').includes(bare))) return false;
    }
    return true;
  }
  const SORTS = {
    client: (p) => p.client.toLowerCase(), so: (p) => (p.so || '').toLowerCase(), rep: (p) => (p.rep || '').toLowerCase(),
    by: (p) => (p.by || '').toLowerCase(),
    status: (p) => ({ overdue: 0, sent: 1, draft: 1, viewed: 2, in_progress: 3, completed: 4, expired: 5, voided: 6 }[p.deadline.overdue ? 'overdue' : p.state] ?? 9),
    signed: (p) => (p.total ? p.signed / p.total : 1),
    when: (p) => t(p.sent_at || p.created_at), fu: (p) => (p.fu ? p.fu.n : -1),
  };
  function sorted(rows) {
    const f = SORTS[state.sort.key] || SORTS.when, d = state.sort.dir;
    return rows.slice().sort((a, b) => { const x = f(a), y = f(b); return x < y ? -d : x > y ? d : b.id - a.id; });
  }

  // ---------------------------------------------------------------- table
  const COLS = [
    { key: 'client', label: 'Client' },
    ...(FAB ? [{ key: 'so', label: 'SO#' }, { key: 'rep', label: 'Sales rep' }] : (MANAGER ? [{ key: 'by', label: 'Created by' }] : [])),
    { key: 'status', label: 'Status' }, { key: 'signed', label: 'Signed' }, { key: 'when', label: 'Sent / due' },
    { key: 'fu', label: 'Last follow-up' },
  ];
  const tableHost = el('div', { class: 'table-wrap db-table' });
  const bulkBar = el('div', { class: 'db-bulk', hidden: true, role: 'region', 'aria-label': 'Selected packets' });
  const chips = el('div', { class: 'db-chips' });

  function header(rows) {
    const all = rows.length && rows.every((p) => state.selected.has(p.id));
    const tr = el('tr', {}, el('th', { class: 'db-check' }, el('input', { type: 'checkbox', 'aria-label': 'Select all shown', checked: all || null,
      onchange: (e) => { rows.forEach((p) => (e.target.checked ? state.selected.add(p.id) : state.selected.delete(p.id))); render(); } })));
    COLS.forEach((c) => {
      const on = state.sort.key === c.key;
      tr.append(el('th', { 'aria-sort': on ? (state.sort.dir > 0 ? 'ascending' : 'descending') : 'none' },
        el('button', { type: 'button', class: 'db-sort' + (on ? ' on' : ''), onclick: () => {
          state.sort = { key: c.key, dir: on ? -state.sort.dir : (['client', 'so', 'rep', 'by', 'status'].includes(c.key) ? 1 : -1) }; render(); } },
        c.label, el('span', { class: 'db-arrow', 'aria-hidden': 'true', text: on ? (state.sort.dir > 0 ? '▲' : '▼') : '↕' }))));
    });
    tr.append(el('th', { class: 'db-actions-h' }, el('span', { class: 'sr-only', text: 'Actions' })));
    return el('thead', {}, tr);
  }

  function statusCell(p) {
    const td = el('td', {}, el('span', { class: 'stamp s-' + p.state, text: p.label }));
    if (p.refusals) td.append(el('div', { class: 'decision neg small', style: 'margin-top:4px', text: 'Changes requested' }));
    if (p.missing && p.missing.length) {
      td.append(el('div', { class: 'still-needed', title: p.missing.join(', ') }, el('span', { text: 'Still needed:' }),
        el('ul', {}, p.missing.slice(0, 4).map((m) => el('li', { text: m })), p.missing.length > 4 ? el('li', { text: '+ ' + (p.missing.length - 4) + ' more' }) : null)));
    } else if (p.total === 0 && !['completed', 'voided'].includes(p.state)) {
      td.append(el('div', { class: 'still-needed' }, el('span', { text: 'Receipt not confirmed yet' })));
    }
    (p.waiting || []).forEach((w) => td.append(el('div', {}, el('span', { class: 'wait-chip', text: 'Waiting on ' + w }))));
    return td;
  }

  function actionsCell(p) {
    const wrap = el('div', { class: 'db-acts' });
    if (isOpen(p)) wrap.append(el('button', { type: 'button', class: 'btn small', title: 'Open an email to ' + p.client + ' with the message and link', onclick: (e) => { e.stopPropagation(); emailPacket(p); } }, '✉ ', el('span', { text: 'Email' })));
    wrap.append(el('button', { type: 'button', class: 'btn btn-quiet small', title: 'Copy the signing link', onclick: (e) => { e.stopPropagation(); copyLink(p, e.currentTarget); } }, 'Copy link'));
    wrap.append(el('button', { type: 'button', class: 'btn btn-quiet small', title: p.archived ? 'Back on the dashboard' : 'Hide from the dashboard (nothing is deleted)',
      onclick: (e) => { e.stopPropagation(); bulk(p.archived ? 'unarchive' : 'archive', [p.id]); } }, p.archived ? 'Restore' : 'Archive'));
    return el('td', { class: 'db-actions' }, wrap);
  }

  function rowEl(p) {
    const tr = el('tr', { class: 'clickable' + (p.deadline.overdue ? ' row-overdue' : '') + (state.flash.has(p.id) ? ' db-flash' : '') + (state.selected.has(p.id) ? ' db-selected' : ''),
      tabindex: '0', 'data-id': p.id,
      onclick: (e) => { if (!e.target.closest('a,button,input')) openPreview(p.id); },
      onkeydown: (e) => { if (e.key === 'Enter' && e.target === e.currentTarget) openPreview(p.id); } });
    tr.append(el('td', { class: 'db-check' }, el('input', { type: 'checkbox', 'aria-label': 'Select ' + p.client, checked: state.selected.has(p.id) || null,
      onclick: (e) => e.stopPropagation(), onchange: (e) => { e.target.checked ? state.selected.add(p.id) : state.selected.delete(p.id); render(); } })));
    tr.append(el('td', {}, el('div', { class: 'client-cell' }, el('a', { href: p.url, text: p.client }), el('span', { class: 'muted small', text: p.email }))));
    if (FAB) {
      tr.append(el('td', { class: 'mono', text: p.so || '—' }));
      const rep = el('td', { class: 'small', text: p.rep || '—' });
      if (MANAGER && p.by && p.by !== p.rep) rep.append(el('div', { class: 'muted', text: 'by ' + p.by }));
      tr.append(rep);
    } else if (MANAGER) tr.append(el('td', { class: 'small', text: p.by || '—' }));
    tr.append(statusCell(p));
    const pct = p.total ? Math.round((100 * p.signed) / p.total) : 0;
    tr.append(el('td', {}, el('div', { class: 'progress' }, el('div', { class: 'bar' + (p.signed === p.total ? ' done' : '') }, el('span', { style: 'width:' + pct + '%' })), p.signed + ' of ' + p.total)));
    const when = el('td', { class: 'small nowrap', text: p.sent_day || '—' });
    if (p.deadline.label) when.append(el('div', { class: 'due' + (p.deadline.overdue ? ' late' : ''), text: p.deadline.label }));
    else if (p.done_day) when.append(el('div', { class: 'muted', text: 'Done ' + p.done_day }));
    tr.append(when);
    const fu = el('td', { class: 'small fu-cell' });
    if (p.fu) { fu.append(el('strong', { text: '#' + p.fu.n }), ' · ' + p.fu.day); if (p.fu.note) fu.append(el('div', { class: 'muted fu-note', title: p.fu.note, text: p.fu.note.length > 60 ? p.fu.note.slice(0, 57) + '…' : p.fu.note })); }
    else if (p.deadline.overdue) fu.append(el('span', { class: 'due late', text: 'None yet' }));
    else fu.append(el('span', { class: 'muted', text: '—' }));
    if (p.notes) fu.append(el('div', { class: 'muted fu-notes-cell', title: p.notes, text: '📝 ' + p.notes }));
    tr.append(fu);
    tr.append(actionsCell(p));
    return tr;
  }

  function renderChips(count) {
    chips.replaceChildren();
    const add = (label, clear) => chips.append(el('span', { class: 'db-chip' }, label, el('button', { type: 'button', 'aria-label': 'Remove filter ' + label, onclick: () => { clear(); render(); } }, '×')));
    if (state.status) add({ awaiting: 'Awaiting client', opened: 'Opened, in progress', overdue: 'Overdue', signed: 'Signed', cancelled: 'Cancelled' }[state.status], () => { state.status = ''; });
    if (state.week) add('Sent or signed the week of ' + state.week.label, () => { state.week = null; });
    if (state.by && !bySel) { const who = (state.data.packets.find((p) => String(p.by_id) === state.by) || {}).by || 'one person'; add('Packets by ' + who, () => { state.by = ''; }); }
    chips.append(el('span', { class: 'muted small', text: count + ' packet' + (count === 1 ? '' : 's') + (state.q ? ' matching “' + state.q.trim() + '”' : '') }));
  }

  function renderBulk() {
    const ids = [...state.selected].filter((id) => state.data.packets.some((p) => p.id === id));
    state.selected = new Set(ids);
    bulkBar.hidden = !ids.length;
    if (!ids.length) return;
    const sel = state.data.packets.filter((p) => state.selected.has(p.id));
    const anyArchived = sel.some((p) => p.archived), anyOpen = sel.some(isOpen);
    bulkBar.replaceChildren(...[
      el('strong', { text: ids.length + ' selected' }),
      anyOpen ? el('button', { type: 'button', class: 'btn small btn-primary', onclick: () => remindDialog(sel.filter(isOpen)) }, '✉ Remind…') : null,
      el('a', { class: 'btn small', href: cfg.export + '?ids=' + ids.join(',') }, 'Export to Excel'),
      el('button', { type: 'button', class: 'btn small', onclick: () => bulk('archive', ids) }, 'Archive'),
      anyArchived ? el('button', { type: 'button', class: 'btn small', onclick: () => bulk('unarchive', ids) }, 'Restore') : null,
      el('button', { type: 'button', class: 'btn btn-quiet small', onclick: () => { state.selected.clear(); render(); } }, 'Clear selection')].filter(Boolean));
  }

  function renderStats() {
    const ps = state.data.packets.filter((p) => !p.archived);
    const n = { awaiting: 0, opened: 0, completed: 0, overdue: 0 };
    ps.forEach((p) => { if (p.state === 'sent') n.awaiting++; else if (['viewed', 'in_progress'].includes(p.state)) n.opened++; else if (p.state === 'completed') n.completed++; if (p.deadline.overdue) n.overdue++; });
    const cards = document.querySelectorAll('.stats .stat');
    const map = [['awaiting', 'awaiting'], ['opened', 'opened'], ['completed', 'signed'], ['overdue', 'overdue']];
    cards.forEach((c, i) => {
      if (!map[i]) return;
      const strong = c.querySelector('strong'); if (strong) strong.textContent = n[map[i][0]];
      if (map[i][0] === 'overdue') c.classList.toggle('has', n.overdue > 0);
      if (!c.dataset.wired) {
        c.dataset.wired = '1'; c.classList.add('db-stat'); c.setAttribute('role', 'button'); c.setAttribute('tabindex', '0');
        const go = (e) => { e.preventDefault(); state.status = state.status === map[i][1] ? '' : map[i][1]; state.tab = 'all'; render(); board.scrollIntoView({ behavior: 'smooth', block: 'start' }); };
        c.addEventListener('click', go); c.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') go(e); });
      }
      c.classList.toggle('db-stat-on', state.status === map[i][1]);
    });
  }

  function render() {
    if (!state.data) return;
    document.querySelectorAll('#board .tabs a').forEach((a) => {
      const show = new URL(a.href, location.href).searchParams.get('show') || 'all';
      a.classList.toggle('active', show === state.tab);
    });
    const rows = sorted(state.data.packets.filter(matches));
    const table = el('table', {}, header(rows), el('tbody', {}, rows.map(rowEl)));
    tableHost.replaceChildren(rows.length ? table : el('div', { class: 'empty' }, el('h2', { text: 'No matches' }),
      el('p', { class: 'muted', text: state.q ? 'Nothing matches “' + state.q.trim() + '”. Check the spelling or clear the search.' : 'No packets here.' }),
      el('button', { type: 'button', class: 'btn', onclick: () => { state.q = ''; state.status = ''; state.week = null; state.tab = 'all'; const q = document.getElementById('q'); if (q) q.value = ''; render(); } }, 'Show all packets')));
    renderChips(rows.length); renderBulk(); renderStats(); renderCharts();
    const u = new URL(location.href); u.searchParams.set('show', state.tab);
    if (state.q) u.searchParams.set('q', state.q); else u.searchParams.delete('q');
    if (state.by) u.searchParams.set('by', state.by); else u.searchParams.delete('by');
    history.replaceState(null, '', u);
  }

  // ---------------------------------------------------------------- charts
  const ins = document.getElementById('insights');
  let tip = null;
  function showTip(evt, lines) {
    if (!tip) { tip = el('div', { class: 'db-tip', role: 'tooltip' }); document.body.append(tip); }
    tip.replaceChildren(...lines.map(([v, label, cls]) => el('div', { class: 'db-tip-row' }, cls ? el('span', { class: 'db-key ' + cls }) : null, el('strong', { text: v }), el('span', { text: ' ' + label }))));
    tip.hidden = false;
    const r = (evt.target.getBoundingClientRect ? evt.target.getBoundingClientRect() : { left: 0, top: 0, width: 0 });
    const x = evt.clientX || r.left + r.width / 2, y = evt.clientY || r.top;
    tip.style.left = Math.min(window.innerWidth - tip.offsetWidth - 8, Math.max(8, x + 12)) + 'px';
    tip.style.top = Math.max(8, y - tip.offsetHeight - 12) + window.scrollY + 'px';
  }
  const hideTip = () => { if (tip) tip.hidden = true; };

  function niceMax(v) { if (v <= 4) return 4; const p = Math.pow(10, Math.floor(Math.log10(v))); const m = v / p; return (m <= 2 ? 2 : m <= 5 ? 5 : 10) * p; }
  function barPath(x, y, w, h, r) {   // rounded data-end (top), square at the baseline
    if (h <= 0) return '';
    r = Math.min(r, w / 2, h);
    return `M${x},${y + h}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + h}Z`;
  }

  function weeksChart(host, weeks) {
    host.replaceChildren();
    const W = Math.max(280, host.clientWidth || 520), H = 190, ml = 28, mr = 6, mt = 10, mb = 24;
    const iw = W - ml - mr, ih = H - mt - mb, band = iw / weeks.length;
    const max = niceMax(Math.max(1, ...weeks.map((w) => Math.max(w.sent, w.signed))));
    const bw = Math.min(24, Math.max(4, (band - 10) / 2 - 1));
    const svg = el('svg:svg', { viewBox: `0 0 ${W} ${H}`, width: '100%', height: H, role: 'img', 'aria-label': 'Packets sent and signed per week, last 12 weeks' });
    const ticks = [0, max / 2, max];
    ticks.forEach((v) => {
      const y = mt + ih - (v / max) * ih;
      svg.append(el('svg:line', { x1: ml, x2: W - mr, y1: y, y2: y, class: v === 0 ? 'db-axis' : 'db-grid' }));
      svg.append(el('svg:text', { x: ml - 6, y: y + 4, 'text-anchor': 'end', class: 'db-tick', text: v >= 1000 ? (v / 1000) + 'K' : v }));
    });
    const every = band < 34 ? 2 : 1;
    weeks.forEach((w, i) => {
      const x0 = ml + i * band, cx = x0 + band / 2;
      const g = el('svg:g', { class: 'db-band' + (state.week && state.week.start === w.start ? ' on' : ''), tabindex: '0', role: 'button',
        'aria-label': `Week of ${w.label}: ${w.sent} sent, ${w.signed} signed. Show these packets.` });
      g.append(el('svg:rect', { x: x0, y: mt, width: band, height: ih, class: 'db-hit' }));
      const hs = (w.sent / max) * ih, hg = (w.signed / max) * ih;
      g.append(el('svg:path', { d: barPath(cx - bw - 1, mt + ih - hs, bw, hs, 4), class: 'db-s1' }));
      g.append(el('svg:path', { d: barPath(cx + 1, mt + ih - hg, bw, hg, 4), class: 'db-s2' }));
      if ((weeks.length - 1 - i) % every === 0) svg.append(el('svg:text', { x: cx, y: H - 6, 'text-anchor': 'middle', class: 'db-tick', text: w.label }));
      const tipLines = () => [[w.sent, 'sent', 'db-s1'], [w.signed, 'signed', 'db-s2'], ['Week of ' + w.label, '', '']];
      g.addEventListener('pointermove', (e) => showTip(e, tipLines()));
      g.addEventListener('pointerleave', hideTip);
      g.addEventListener('focus', (e) => showTip(e, tipLines()));
      g.addEventListener('blur', hideTip);
      const pick = () => { state.week = state.week && state.week.start === w.start ? null : w; state.tab = 'all'; hideTip(); render(); };
      g.addEventListener('click', pick);
      g.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); pick(); } });
      svg.append(g);
    });
    const last = weeks[weeks.length - 1];
    host.append(svg);
    // table view of the same numbers
    const tbl = el('table', { class: 'db-mini' }, el('thead', {}, el('tr', {}, el('th', { text: 'Week of' }), el('th', { text: 'Sent' }), el('th', { text: 'Signed' }))),
      el('tbody', {}, weeks.map((w) => el('tr', {}, el('td', { text: w.label }), el('td', { text: w.sent }), el('td', { text: w.signed })))));
    host.append(el('details', { class: 'db-tablev' }, el('summary', { class: 'small', text: 'Show as table' }), tbl));
    return last;
  }

  function statusChart(host, packets) {
    const ps = packets.filter((p) => !p.archived);
    const rows = [
      ['awaiting', 'Awaiting client', '○', ps.filter((p) => p.state === 'sent').length, 'db-st-wait'],
      ['opened', 'Opened, in progress', '◐', ps.filter((p) => ['viewed', 'in_progress'].includes(p.state)).length, 'db-st-open'],
      ['overdue', 'Overdue', '!', ps.filter((p) => p.deadline.overdue).length, 'db-st-late'],
      ['signed', 'Signed', '✓', ps.filter((p) => p.state === 'completed').length, 'db-st-done'],
      ['cancelled', 'Cancelled', '×', ps.filter((p) => p.state === 'voided').length, 'db-st-void'],
    ];
    const max = Math.max(1, ...rows.map((r) => r[3]));
    host.replaceChildren(...rows.map(([key, label, icon, n, cls]) => el('button', { type: 'button', class: 'db-st-row' + (state.status === key ? ' on' : ''),
      'aria-pressed': state.status === key ? 'true' : 'false', title: 'Show ' + label.toLowerCase() + ' packets',
      onclick: () => { state.status = state.status === key ? '' : key; state.tab = 'all'; render(); } },
      el('span', { class: 'db-st-label' }, el('span', { class: 'db-st-icon ' + cls, 'aria-hidden': 'true', text: icon }), label),
      el('span', { class: 'db-st-track' }, el('span', { class: 'db-st-bar ' + cls, style: `width:${n ? Math.max(3, (100 * n) / max) : 0}%` })),
      el('strong', { class: 'db-st-n', text: n }))));
  }

  function renderCharts() {
    if (!ins) return;
    const c = state.data.charts;
    ins.hidden = false;
    const wk = document.getElementById('chart-weeks'); if (wk) weeksChart(wk, c.weeks);
    statusChart(document.getElementById('chart-status'), state.data.packets);
    const med = document.getElementById('kpi-median'), within = document.getElementById('kpi-within');
    if (med) med.textContent = c.median_hours === null ? '—' : (c.median_hours < 48 ? Math.round(c.median_hours * 10) / 10 + ' h' : Math.round(c.median_hours / 24 * 10) / 10 + ' days');
    if (within) within.textContent = c.within_pct === null ? '—' : c.within_pct + '%';
    const sub = document.getElementById('kpi-sub'); if (sub) sub.textContent = c.completed_90d ? `from ${c.completed_90d} packet${c.completed_90d === 1 ? '' : 's'} signed in the last 90 days` : 'No packets signed in the last 90 days yet';
  }
  let rT = null; window.addEventListener('resize', () => { clearTimeout(rT); rT = setTimeout(() => state.data && renderCharts(), 150); });

  // ---------------------------------------------------------------- actions
  const PASTE = () => '<strong>Your email is open.</strong> To make the link clickable: click in the message, press <kbd>' + (mac ? '⌘A' : 'Ctrl+A') + '</kbd> then <kbd>' + (mac ? '⌘V' : 'Ctrl+V') + '</kbd>, then press Send.';
  function emailPacket(p, done) {
    const job = fetch(cfg.quick.replace('/0/', '/' + p.id + '/'), { credentials: 'same-origin' }).then((r) => r.json());
    let copied = Promise.reject(); copied.catch(() => {});
    if (navigator.clipboard && window.ClipboardItem) {
      try {
        copied = navigator.clipboard.write([new ClipboardItem({ 'text/html': job.then((q) => new Blob([q.html], { type: 'text/html' })), 'text/plain': job.then((q) => new Blob([q.text], { type: 'text/plain' })) })]);
        copied.catch(() => {});
      } catch (e) { /* older browser: plain text only */ }
    }
    job.then((q) => {
      location.href = q.mailto;
      const fd = new FormData(); fd.append('csrf', csrf); fd.append('via', 'email'); fd.append('kind', q.reminder ? 'reminder' : 'send');
      fetch(q.nudge, { method: 'POST', body: fd, credentials: 'same-origin', keepalive: true }).then(() => setTimeout(refresh, 800));
      copied.then(() => toast(PASTE(), 20000), () => toast('<strong>Your email is open</strong> with the message and link to ' + esc(q.client) + '.', 8000));
      if (done) done(q);
    }).catch(() => toast("Couldn't prepare the email. Open the packet and use Open in Email.", 8000));
  }
  function copyLink(p, btn) {
    const job = fetch(cfg.quick.replace('/0/', '/' + p.id + '/'), { credentials: 'same-origin' }).then((r) => r.json());
    const ok = () => { const old = btn.textContent; btn.textContent = 'Copied ✓'; setTimeout(() => { btn.textContent = old; }, 1600); };
    if (navigator.clipboard && window.ClipboardItem) {
      navigator.clipboard.write([new ClipboardItem({ 'text/plain': job.then((q) => new Blob([q.link], { type: 'text/plain' })) })]).then(ok, () => job.then((q) => window.prompt('Copy the link:', q.link)));
    } else job.then((q) => (navigator.clipboard ? navigator.clipboard.writeText(q.link).then(ok, () => window.prompt('Copy the link:', q.link)) : window.prompt('Copy the link:', q.link)));
  }
  function bulk(action, ids) {
    post(cfg.bulk, { action, ids }).then((r) => r.json()).then((d) => {
      if (d.error) { toast(esc(d.error)); return; }
      if (action === 'archive') ids.forEach((id) => state.selected.delete(id));
      toast(esc(d.message) + (action === 'archive' ? ' <button type="button" class="db-undo">Undo</button>' : ''), 8000);
      const u = document.querySelector('#db-toast .db-undo'); if (u) u.onclick = (e) => { e.stopPropagation(); bulk('unarchive', ids); };
      refresh(true);
    });
  }

  function remindDialog(list) {
    const d = el('dialog', { class: 'qs-dialog db-remind' });
    const items = list.map((p) => {
      const btn = el('button', { type: 'button', class: 'btn small btn-primary' }, '✉ Open email');
      const row = el('li', {}, el('span', {}, el('strong', { text: p.client }), el('span', { class: 'muted small', text: p.email ? ' · ' + p.email : ' · no email on file' })), btn);
      btn.onclick = () => emailPacket(p, () => { row.classList.add('done'); btn.textContent = 'Opened ✓'; btn.classList.remove('btn-primary'); });
      return row;
    });
    d.append(el('h2', { text: 'Remind ' + list.length + ' client' + (list.length === 1 ? '' : 's') }),
      el('p', { class: 'small muted', text: 'Click each one: a new email opens with that client\'s message and link. Send it, then come back for the next. Each one is logged on its packet.' }),
      el('ul', { class: 'db-remind-list' }, items),
      el('div', { class: 'row' }, el('button', { type: 'button', class: 'btn', onclick: () => d.close() }, 'Done')));
    d.addEventListener('close', () => d.remove());
    document.body.append(d); d.showModal ? d.showModal() : d.setAttribute('open', '');
  }

  // ---------------------------------------------------------------- preview panel
  const drawer = el('aside', { class: 'db-drawer', hidden: true, 'aria-label': 'Packet preview', tabindex: '-1' });
  document.body.append(drawer);
  function closeDrawer() { drawer.hidden = true; document.querySelectorAll('tr.db-open').forEach((r) => r.classList.remove('db-open')); }
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !drawer.hidden) closeDrawer(); });
  function openPreview(id) {
    document.querySelectorAll('tr.db-open').forEach((r) => r.classList.remove('db-open'));
    const tr = document.querySelector(`tr[data-id="${id}"]`); if (tr) tr.classList.add('db-open');
    drawer.hidden = false; drawer.replaceChildren(el('p', { class: 'muted', text: 'Loading…' }));
    fetch(cfg.preview.replace('/0/', '/' + id + '/'), { credentials: 'same-origin' }).then((r) => r.json()).then((p) => {
      const open = isOpen(p);
      drawer.replaceChildren(...[
        el('div', { class: 'db-drawer-head' }, el('div', {}, el('h2', { text: p.client }), el('span', { class: 'muted small', text: [p.email, p.so ? 'SO# ' + p.so : ''].filter(Boolean).join(' · ') })),
          el('button', { type: 'button', class: 'db-x', 'aria-label': 'Close preview', onclick: closeDrawer }, '×')),
        el('div', { class: 'row', style: 'gap:8px;flex-wrap:wrap' }, el('span', { class: 'stamp s-' + p.state, text: p.label }),
          p.deadline.label ? el('span', { class: 'due' + (p.deadline.overdue ? ' late' : ''), text: p.deadline.label }) : null,
          p.archived ? el('span', { class: 'muted small', text: 'Archived' }) : null),
        (p.waiting || []).length ? el('p', { class: 'small', style: 'margin:0' }, el('span', { class: 'wait-chip', text: 'Waiting on ' + p.waiting.join(', ') })) : null,
        el('div', { class: 'db-drawer-acts' },
          open ? el('button', { type: 'button', class: 'btn btn-primary small', onclick: () => emailPacket(p) }, p.emailed ? '✉ Send a reminder' : '✉ Email the client') : null,
          el('button', { type: 'button', class: 'btn small', onclick: (e) => copyLink(p, e.currentTarget) }, 'Copy link'),
          el('a', { class: 'btn small', href: p.url }, 'Open packet →')),
        el('h3', { text: 'Forms · ' + p.signed + ' of ' + p.total + ' signed' }),
        el('ul', { class: 'db-items' }, p.items.map((it) => el('li', { class: it.signed ? 'ok' : '' }, el('span', { 'aria-hidden': 'true', text: it.signed ? '✓' : (it.sign ? '○' : '·') }),
          el('span', {}, el('span', { text: it.name }), it.signed ? el('span', { class: 'muted small', text: ' · ' + (it.by ? it.by + ', ' : '') + it.day }) : (it.sign ? null : el('span', { class: 'muted small', text: ' · no signature needed' })))))),
        p.message ? el('div', {}, el('h3', { text: 'Note to the client' }), el('p', { class: 'small', style: 'white-space:pre-wrap;margin:0', text: p.message })) : null,
        (p.followups || []).length || p.notes_full ? el('div', {}, el('h3', { text: 'Follow-ups & notes' }),
          (p.followups || []).length ? el('ul', { class: 'db-events' }, p.followups.map((f) => el('li', {},
            el('strong', { text: 'Follow-up ' + f.n }), el('span', { class: 'small', text: f.note ? ' · ' + f.note : '' }),
            el('div', { class: 'muted small', text: f.done ? ('Done ' + f.day + (f.by ? ' · ' + f.by : '')) : 'Not done yet' })))) : null,
          p.notes_full ? el('p', { class: 'small', style: 'white-space:pre-wrap;margin:6px 0 0', text: '📝 ' + p.notes_full }) : null) : null,
        el('h3', { text: 'Recent activity' }),
        el('ul', { class: 'db-events' }, p.events.map((e) => el('li', {}, el('strong', { text: e.label }), e.detail ? el('span', { class: 'small', text: ' · ' + e.detail }) : null, el('div', { class: 'muted small', text: e.when })))),
        el('p', { class: 'muted small', text: 'Link valid until ' + p.expires_day + (p.access_code ? ' · access code on' : '') })].filter(Boolean));
      drawer.focus();
    }).catch(() => drawer.replaceChildren(el('p', { text: "Couldn't load this packet." }), el('a', { href: '#', onclick: (e) => { e.preventDefault(); closeDrawer(); } }, 'Close')));
  }

  // ---------------------------------------------------------------- live updates
  function changes(oldRows, newRows) {
    const before = new Map(oldRows.map((p) => [p.id, p]));
    const msgs = [];
    newRows.forEach((p) => {
      const o = before.get(p.id);
      if (!o) return;
      if (o.state !== p.state || o.signed !== p.signed) {
        state.flash.add(p.id);
        if (p.state === 'completed' && o.state !== 'completed') msgs.push(`<strong>${esc(p.client)}</strong> signed everything ✓`);
        else if (p.signed > o.signed) msgs.push(`<strong>${esc(p.client)}</strong> signed ${p.signed} of ${p.total}`);
        else if (['viewed', 'in_progress'].includes(p.state) && o.state === 'sent') msgs.push(`<strong>${esc(p.client)}</strong> opened their forms`);
      }
    });
    return msgs;
  }
  function refresh(force) {
    if (document.hidden && !force) return Promise.resolve();
    board.classList.add('db-loading');
    return fetch(cfg.json + (state.version && !force ? '?v=' + encodeURIComponent(state.version) : ''), { credentials: 'same-origin', headers: { Accept: 'application/json' } })
      .then((r) => { if (r.status === 401 || r.redirected) throw new Error('signed out'); return r.json(); })
      .then((d) => {
        board.classList.remove('db-loading');
        if (d.unchanged) return;
        const msgs = state.data ? changes(state.data.packets, d.packets) : [];
        state.data = d; state.version = d.version;
        render();
        if (msgs.length) toast(msgs.slice(0, 3).join('<br>') + (msgs.length > 3 ? `<br>and ${msgs.length - 3} more` : ''), 9000);
        if (state.flash.size) setTimeout(() => { state.flash.clear(); document.querySelectorAll('tr.db-flash').forEach((r) => r.classList.remove('db-flash')); }, 4000);
        const live = document.getElementById('db-live'); if (live) live.textContent = 'Live · updated ' + new Date().toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
      })
      .catch(() => board.classList.remove('db-loading'));
  }

  // ---------------------------------------------------------------- take over the page
  const searchForm = board.querySelector('form.search-bar');
  if (searchForm) {
    searchForm.addEventListener('submit', (e) => { e.preventDefault(); });
    const q = document.getElementById('q');
    if (q) { q.addEventListener('input', () => { state.q = q.value; render(); }); q.setAttribute('placeholder', q.getAttribute('placeholder') + ' (results as you type)'); }
    searchForm.querySelectorAll('button.btn:not([name]), a.btn-quiet').forEach((b) => { b.hidden = true; });
  }
  board.querySelectorAll('.tabs a').forEach((a) => a.addEventListener('click', (e) => {
    e.preventDefault(); state.tab = new URL(a.href, location.href).searchParams.get('show') || 'all'; state.status = ''; render();
  }));
  refresh(true).then(() => {
    if (!state.data) return;
    const old = board.querySelector(':scope > .table-wrap, :scope > .empty');
    if (old) old.replaceWith(tableHost); else board.append(tableHost);
    const head = board.querySelector('.panel-head');
    if (head) { head.after(chips); chips.after(bulkBar); }
    head && head.append(el('span', { id: 'db-live', class: 'db-live small muted', 'aria-live': 'polite', text: 'Live' }));
    render();
  });
  setInterval(() => refresh(false), 20000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(false); });
})();
