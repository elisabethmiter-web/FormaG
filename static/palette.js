// Quick search (Ctrl+K / ⌘K / "/"), the installable app (service worker + "Install app" button).
(function () {
  'use strict';
  const isMac = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);
  const keyLabel = isMac ? '⌘K' : 'Ctrl+K';

  // ------------------------------------------------------------------ styles
  const css = `
  .qk-btn { display:inline-flex; align-items:center; gap:8px; color:var(--muted); background:var(--bg); border:1px solid var(--rule);
    border-radius:6px; padding:6px 10px; font:500 .88rem var(--font-body, inherit); cursor:pointer; white-space:nowrap; }
  .qk-btn:hover { color:var(--ink); border-color:var(--muted); }
  .qk-btn kbd, .qk-foot kbd { font:600 .72rem var(--font-body, inherit); border:1px solid var(--rule); border-radius:4px; padding:0 5px; background:var(--surface); color:var(--muted); }
  .qk-install { display:none; }
  .qk-install.show { display:inline-flex; }
  .topbar-inner { gap: 16px; } .nav a { padding: 6px 8px; }
  @media (max-width: 1440px) { .qk-btn .qk-lbl, .qk-btn kbd { display:none; } .qk-btn { padding:7px 9px; } }
  @media (max-width: 760px) { .qk-btn .qk-lbl, .qk-btn kbd { display:none; } .qk-btn { padding:6px 9px; } .qk-install.show { display:none; } }
  .qk-back { position:fixed; inset:0; background:rgba(16,19,26,.45); z-index:1000; display:flex; align-items:flex-start; justify-content:center; padding:10vh 16px 16px; }
  .qk-back[hidden] { display:none; }
  .qk-box { width:100%; max-width:600px; background:var(--surface); color:var(--ink); border:1px solid var(--rule); border-radius:10px;
    box-shadow:0 18px 50px rgba(0,0,0,.25); display:flex; flex-direction:column; max-height:75vh; overflow:hidden; }
  .qk-in { display:flex; align-items:center; gap:10px; padding:12px 14px; border-bottom:1px solid var(--rule); }
  .qk-in svg { flex:none; color:var(--muted); }
  .qk-in input { border:0; box-shadow:none !important; padding:6px 0; font-size:1.05rem; background:transparent; }
  .qk-in input:focus { outline:none; }
  .qk-list { overflow-y:auto; padding:6px 0; margin:0; list-style:none; }
  .qk-group { font-size:.72rem; text-transform:uppercase; letter-spacing:.08em; color:var(--muted); font-weight:600; padding:8px 16px 4px; }
  .qk-item a { display:flex; align-items:baseline; gap:10px; padding:9px 16px; color:var(--ink); text-decoration:none; min-height:40px; }
  .qk-item a .qk-hint { color:var(--muted); font-size:.85rem; margin-left:auto; text-align:right; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; max-width:55%; }
  .qk-item a .qk-lab { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .qk-item[aria-selected="true"] a { background:var(--accent-soft); }
  .qk-empty { padding:18px 16px; color:var(--muted); }
  .qk-foot { display:flex; gap:14px; flex-wrap:wrap; padding:8px 14px; border-top:1px solid var(--rule); color:var(--muted); font-size:.8rem; }
  @media (max-width: 560px) { .qk-back { padding-top:12px; } .qk-box { max-height:85vh; } .qk-foot { display:none; } .qk-item a .qk-hint { max-width:45%; } }
  `;
  const st = document.createElement('style'); st.textContent = css; document.head.appendChild(st);

  const searchIcon = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/></svg>';

  // ------------------------------------------------------------------ topbar buttons
  const bar = document.querySelector('.topbar-inner');
  const newBtn = bar && bar.querySelector('a.btn-primary');
  const openBtn = document.createElement('button');
  openBtn.type = 'button'; openBtn.className = 'qk-btn'; openBtn.id = 'qk-open';
  openBtn.setAttribute('aria-label', 'Search (' + keyLabel + ')'); openBtn.title = 'Search clients, packets, forms and pages (' + keyLabel + ')';
  openBtn.innerHTML = searchIcon + '<span class="qk-lbl">Search</span><kbd>' + keyLabel + '</kbd>';
  const installBtn = document.createElement('button');
  installBtn.type = 'button'; installBtn.className = 'btn btn-quiet qk-install'; installBtn.textContent = 'Install app';
  installBtn.title = 'Add this app to your computer or phone, so it opens in its own window';
  if (bar) { if (newBtn) { bar.insertBefore(openBtn, newBtn); bar.insertBefore(installBtn, newBtn); } else { bar.appendChild(openBtn); bar.appendChild(installBtn); } }

  // ------------------------------------------------------------------ dialog
  const back = document.createElement('div');
  back.className = 'qk-back'; back.hidden = true;
  back.innerHTML = '<div class="qk-box" role="dialog" aria-modal="true" aria-label="Quick search">' +
    '<div class="qk-in">' + searchIcon + '<input type="text" id="qk-q" autocomplete="off" spellcheck="false" placeholder="Search clients, SO#, forms, people or pages…" ' +
    'role="combobox" aria-expanded="true" aria-controls="qk-list" aria-autocomplete="list" aria-label="Search"></div>' +
    '<ul class="qk-list" id="qk-list" role="listbox"></ul>' +
    '<div class="qk-foot"><span><kbd>↑</kbd> <kbd>↓</kbd> to move</span><span><kbd>Enter</kbd> to open</span><span><kbd>Esc</kbd> to close</span><span style="margin-left:auto">' + keyLabel + ' opens this from any page</span></div></div>';
  document.body.appendChild(back);
  const input = back.querySelector('#qk-q'), list = back.querySelector('#qk-list');
  let items = [], sel = 0, timer = null, ctl = null, lastFocus = null, deferred = null;

  function extras(q) {
    const out = [];
    if (deferred && (!q || 'install app'.includes(q.toLowerCase()))) out.push({ kind: 'Action', label: 'Install app', hint: 'Opens in its own window', install: true });
    return out;
  }
  function draw(results, q) {
    items = results.concat(extras(q)); sel = 0; list.replaceChildren();
    if (!items.length) {
      const li = document.createElement('li'); li.className = 'qk-empty';
      li.textContent = q.length < 2 ? 'Type at least 2 letters.' : 'Nothing found for “' + q + '”.'; list.appendChild(li); return;
    }
    const order = ['Action', 'Packet', 'Form', 'Person', 'Page'], names = { Action: 'Actions', Packet: 'Packets', Form: 'Forms', Person: 'Team', Page: 'Pages' };
    items.sort((a, b) => order.indexOf(a.kind) - order.indexOf(b.kind));
    let last = null;
    items.forEach((r, i) => {
      if (r.kind !== last) { const h = document.createElement('li'); h.className = 'qk-group'; h.setAttribute('role', 'presentation'); h.textContent = names[r.kind] || r.kind; list.appendChild(h); last = r.kind; }
      const li = document.createElement('li'); li.className = 'qk-item'; li.id = 'qk-o' + i; li.setAttribute('role', 'option');
      const a = document.createElement('a'); a.href = r.url || '#'; a.tabIndex = -1;
      const l = document.createElement('span'); l.className = 'qk-lab'; l.textContent = r.label;
      const h = document.createElement('span'); h.className = 'qk-hint'; h.textContent = r.hint || '';
      a.append(l, h); li.appendChild(a);
      a.addEventListener('click', (e) => { if (r.install) { e.preventDefault(); close(); install(); } });
      li.addEventListener('mousemove', () => { if (sel !== i) { sel = i; mark(); } });
      list.appendChild(li);
    });
    mark();
  }
  function mark() {
    list.querySelectorAll('.qk-item').forEach((li) => li.setAttribute('aria-selected', li.id === 'qk-o' + sel ? 'true' : 'false'));
    const cur = document.getElementById('qk-o' + sel);
    if (cur) { input.setAttribute('aria-activedescendant', cur.id); cur.scrollIntoView({ block: 'nearest' }); }
  }
  function search() {
    const q = input.value.trim();
    if (ctl) ctl.abort();
    ctl = ('AbortController' in window) ? new AbortController() : null;
    fetch('/search.json?q=' + encodeURIComponent(q), { credentials: 'same-origin', headers: { Accept: 'application/json' }, signal: ctl ? ctl.signal : undefined })
      .then((r) => { if (r.redirected || !r.ok) throw new Error('signed out'); return r.json(); })
      .then((d) => { if (input.value.trim() === q) draw(d.results || [], q); })
      .catch((e) => { if (e && e.name === 'AbortError') return; list.innerHTML = '<li class="qk-empty">Search isn’t available right now. Check your connection, or sign in again.</li>'; items = []; });
  }
  function open() {
    if (!back.hidden) { input.select(); return; }
    lastFocus = document.activeElement; back.hidden = false; document.body.style.overflow = 'hidden';
    input.value = ''; search(); setTimeout(() => input.focus(), 0);
  }
  function close() {
    back.hidden = true; document.body.style.overflow = '';
    if (lastFocus && lastFocus.focus) lastFocus.focus();
  }
  function go(i) {
    const r = items[i]; if (!r) return;
    if (r.install) { close(); install(); return; }
    location.href = r.url;
  }
  input.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(search, 140); });
  input.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowDown') { e.preventDefault(); if (items.length) { sel = (sel + 1) % items.length; mark(); } }
    else if (e.key === 'ArrowUp') { e.preventDefault(); if (items.length) { sel = (sel - 1 + items.length) % items.length; mark(); } }
    else if (e.key === 'Enter') { e.preventDefault(); go(sel); }
    else if (e.key === 'Escape') { e.preventDefault(); close(); }
    else if (e.key === 'Tab') { e.preventDefault(); }   // keep focus inside the dialog
  });
  back.addEventListener('mousedown', (e) => { if (e.target === back) close(); });
  openBtn.addEventListener('click', open);
  document.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && !e.altKey && !e.shiftKey && (e.key === 'k' || e.key === 'K')) { e.preventDefault(); open(); return; }
    if (e.key === '/' && !e.ctrlKey && !e.metaKey && !e.altKey && back.hidden) {
      const t = e.target, tag = (t && t.tagName) || '';
      if (/^(INPUT|TEXTAREA|SELECT)$/.test(tag) || (t && t.isContentEditable)) return;
      e.preventDefault(); open();
    }
  });
  window.FSQuickSearch = { open: open, close: close };

  // ------------------------------------------------------------------ installable app
  const secure = location.protocol === 'https:' || location.hostname === 'localhost' || location.hostname === '127.0.0.1';
  if ('serviceWorker' in navigator && secure) {
    const reg = () => navigator.serviceWorker.register('/sw.js', { scope: '/' }).catch(() => {});
    if (document.readyState === 'complete') reg(); else window.addEventListener('load', reg);
  }
  function install() {
    if (!deferred) return;
    deferred.prompt();
    deferred.userChoice.then(() => { deferred = null; installBtn.classList.remove('show'); }).catch(() => {});
  }
  window.addEventListener('beforeinstallprompt', (e) => {
    e.preventDefault(); deferred = e;
    installBtn.classList.add('show');
    document.querySelectorAll('[data-install-app]').forEach((b) => { b.hidden = false; });
  });
  window.addEventListener('appinstalled', () => { deferred = null; installBtn.classList.remove('show'); });
  installBtn.addEventListener('click', install);
  document.addEventListener('click', (e) => { const b = e.target.closest && e.target.closest('[data-install-app]'); if (b) { e.preventDefault(); install(); } });
})();
