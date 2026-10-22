// "What's new": short release notes written by the platform owner in the Admin Console.
// Adds a "✨ What's new" button to the top menu (with the number of unread notes), a side panel listing the notes
// newest first, and a one-time pop-up for important ones. Shows nothing at all when there are no notes.
(function () {
  if (!window.fetch || !document.querySelector) return;
  const nav = document.querySelector('header.topbar nav.nav') || document.querySelector('header nav');
  if (!nav) return;
  const csrf = (document.querySelector('meta[name=csrf]') || {}).content || '';
  let data = null, btn = null, badge = null, panel = null, shade = null, lastFocus = null;

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  }
  function when(day) {
    const p = String(day || '').split('-').map(Number);
    if (p.length !== 3 || !p[0]) return day || '';
    return new Date(p[0], p[1] - 1, p[2]).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
  }
  function post(ids, kind) {
    if (!ids.length) return Promise.resolve();
    return fetch('/whatsnew/seen', { method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf, Accept: 'application/json' },
      body: JSON.stringify({ ids: ids, kind: kind || 'read' }) }).catch(function () {});
  }
  function setBadge(n) {
    badge.hidden = !n;
    badge.textContent = n ? String(n) : '';
    btn.setAttribute('aria-label', "What's new" + (n ? ' (' + n + ' unread)' : ''));
  }
  function paragraphs(text) {
    return String(text || '').split(/\n\s*\n/).filter(function (t) { return t.trim(); }).map(function (t) {
      const p = el('p');
      t.trim().split('\n').forEach(function (line, i) { if (i) p.append(document.createElement('br')); p.append(document.createTextNode(line)); });
      return p;
    });
  }
  function render() {
    const list = panel.querySelector('.wn-list');
    list.replaceChildren();
    data.notes.forEach(function (n) {
      const li = el('li', 'wn-note' + (n.unread ? ' wn-unread' : '') + (n.important ? ' wn-important' : ''));
      const meta = el('div', 'wn-meta');
      meta.append(el('time', '', when(n.day)));
      if (n.unread) meta.append(el('span', 'wn-pill wn-pill-new', 'New'));
      if (n.important) meta.append(el('span', 'wn-pill wn-pill-imp', '! Important'));
      li.append(meta, el('h3', '', n.title));
      paragraphs(n.body).forEach(function (p) { li.append(p); });
      list.append(li);
    });
  }
  function open() {
    if (!panel.hidden) return;
    lastFocus = document.activeElement;
    render();
    panel.hidden = false; shade.hidden = false;
    btn.setAttribute('aria-expanded', 'true');
    document.body.classList.add('wn-open');
    panel.querySelector('.wn-close').focus();
    const unread = data.notes.filter(function (n) { return n.unread; }).map(function (n) { return n.id; });
    if (unread.length) {
      post(unread, 'read');
      data.notes.forEach(function (n) { n.unread = false; });   // highlighted until the panel is closed
      setBadge(0);
    }
    hideToast();
  }
  function close() {
    if (panel.hidden) return;
    panel.hidden = true; shade.hidden = true;
    btn.setAttribute('aria-expanded', 'false');
    document.body.classList.remove('wn-open');
    if (lastFocus && lastFocus.focus) lastFocus.focus();
  }
  function hideToast() { const t = document.getElementById('wn-toast'); if (t) t.remove(); }
  function toast(t) {
    const box = el('div', 'wn-toast'); box.id = 'wn-toast'; box.setAttribute('role', 'status');
    const head = el('div', 'wn-toast-head');
    head.append(el('span', 'wn-pill wn-pill-imp', '! Important update'));
    const x = el('button', 'wn-x', '×'); x.type = 'button'; x.setAttribute('aria-label', 'Dismiss');
    x.onclick = hideToast;
    head.append(x);
    const more = el('button', 'wn-more', 'Read more'); more.type = 'button'; more.onclick = open;
    box.append(head, el('strong', 'wn-toast-title', t.title));
    if (t.body) box.append(el('p', '', t.body.length > 200 ? t.body.slice(0, 200).trim() + '…' : t.body));
    box.append(more);
    document.body.append(box);
    post([t.id], 'toast');   // shown once per person
  }
  function build() {
    btn = el('button', 'wn-btn'); btn.type = 'button';
    btn.setAttribute('aria-haspopup', 'dialog'); btn.setAttribute('aria-expanded', 'false'); btn.setAttribute('aria-controls', 'wn-panel');
    btn.append(el('span', 'wn-spark', '✨'), document.createTextNode(" What's new"));
    badge = el('span', 'wn-badge'); btn.append(badge);
    btn.addEventListener('click', function () { if (panel.hidden) open(); else close(); });
    nav.append(btn);
    shade = el('div', 'wn-shade'); shade.hidden = true; shade.addEventListener('click', close);
    panel = el('aside', 'wn-panel'); panel.id = 'wn-panel'; panel.hidden = true;
    panel.setAttribute('role', 'dialog'); panel.setAttribute('aria-label', "What's new"); panel.tabIndex = -1;
    const head = el('header', 'wn-head');
    const h = el('div'); h.append(el('h2', '', "What's new"), el('span', 'wn-sub', 'Changes and new features in the app'));
    const x = el('button', 'wn-close', '×'); x.type = 'button'; x.setAttribute('aria-label', 'Close');
    x.addEventListener('click', close);
    head.append(h, x);
    panel.append(head, el('ol', 'wn-list'));
    document.body.append(shade, panel);
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape') { close(); hideToast(); } });
  }
  function load() {
    return fetch('/whatsnew.json', { credentials: 'same-origin', headers: { Accept: 'application/json' } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (!d || !Array.isArray(d.notes)) return;
        if (!d.notes.length) { if (btn) { btn.remove(); btn = null; panel.remove(); shade.remove(); } data = null; return; }
        const wasOpen = panel && !panel.hidden;
        if (wasOpen) return;   // don't change the list while it's being read
        data = d;
        if (!btn) build();
        setBadge(d.unread || 0);
        if (d.toast && !document.getElementById('wn-toast')) toast(d.toast);
      }).catch(function () {});
  }
  load();
  setInterval(function () { if (!document.hidden) load(); }, 10 * 60 * 1000);
})();
