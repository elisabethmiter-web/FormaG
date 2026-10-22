// Packet page: checks every 15 seconds and refreshes when the client opens, signs or sends files.
(function () {
  const cfg = document.getElementById('packet-live'); if (!cfg || !window.fetch) return;
  const CLIENT = ['Opened by client', 'Signed', 'Completed', 'Files from client', 'Confirmed receipt', 'Downloaded by client'];
  const clientEv = (d) => (d.events || []).filter((e) => CLIENT.includes(e.label));   // only what the client does, not your own clicks
  const key = (d) => { const e = clientEv(d)[0]; return [d.state, d.signed, d.total, e ? e.when + e.label : ''].join('|'); };
  let base = null;
  const msgKey = 'fs-live-msg-' + location.pathname;
  try { const m = sessionStorage.getItem(msgKey); if (m) { sessionStorage.removeItem(msgKey); show(m, false); const y = sessionStorage.getItem(msgKey + '-y'); if (y) window.scrollTo(0, +y); sessionStorage.removeItem(msgKey + '-y'); } } catch (e) {}
  function show(text, refreshBtn) {
    let bar = document.getElementById('live-bar');
    if (!bar) { bar = document.createElement('div'); bar.id = 'live-bar'; bar.className = 'flash flash-ok live-bar'; bar.setAttribute('role', 'status'); const main = document.querySelector('main') || document.body; main.prepend(bar); }
    bar.replaceChildren(document.createTextNode('🔔 ' + text + ' '));
    if (refreshBtn) { const b = document.createElement('button'); b.type = 'button'; b.className = 'btn small'; b.textContent = 'Refresh'; b.onclick = () => location.reload(); bar.append(b); }
    else setTimeout(() => bar.remove(), 8000);
  }
  function busy() {
    const a = document.activeElement;
    return a && (a.tagName === 'TEXTAREA' || (a.tagName === 'INPUT' && !['checkbox', 'radio', 'button', 'submit'].includes(a.type)) || a.isContentEditable) ||
      document.querySelector('details[open] form, dialog[open]');
  }
  function check() {
    if (document.hidden) return;
    fetch(cfg.dataset.url, { credentials: 'same-origin', headers: { Accept: 'application/json' } }).then((r) => (r.ok ? r.json() : null)).then((d) => {
      if (!d) return;
      const k = key(d);
      if (base === null) { base = k; return; }
      if (k === base) return;
      base = k;
      const e0 = clientEv(d)[0]; const what = e0 ? e0.label + (e0.detail && e0.detail !== e0.label ? ': ' + e0.detail : '') : 'This packet changed';
      if (busy()) { show(what + '. Refresh to see it (your typing is kept until you do).', true); return; }
      try { sessionStorage.setItem(msgKey, 'Just now: ' + what); sessionStorage.setItem(msgKey + '-y', String(window.scrollY)); } catch (e) {}
      location.reload();
    }).catch(() => {});
  }
  check(); setInterval(check, 15000);
  document.addEventListener('visibilitychange', check);
})();
