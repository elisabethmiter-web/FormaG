// Support chat with the platform owner: one ongoing conversation per person, checked every few seconds while open.
(function () {
  const box = document.getElementById('sc'); if (!box) return;
  const csrf = (document.querySelector('meta[name=csrf]') || {}).content || '';
  const toggle = box.querySelector('.sc-toggle'), panel = box.querySelector('.sc-panel'), log = box.querySelector('.sc-log');
  const form = box.querySelector('.sc-form'), ta = form.querySelector('textarea'), badge = box.querySelector('.sc-badge');
  let last = 0, timer = null, loaded = false, typingTimer = null;
  // "Liz is typing…" while the owner writes an answer (checked only while the chat is open)
  const typing = document.createElement('p');
  typing.className = 'sc-note sc-typing'; typing.hidden = true; typing.setAttribute('aria-live', 'polite');
  typing.style.cssText = 'padding:0 14px 6px;font-style:italic';
  form.parentNode.insertBefore(typing, form);
  function checkTyping() {
    if (document.hidden) return;
    fetch('/support/typing.json', { headers: { Accept: 'application/json' } }).then(r => r.ok ? r.json() : {}).then(d => {
      const on = !!(d && d.typing);
      if (on) typing.textContent = (d.name || 'Support') + ' is typing…';
      if (typing.hidden === on) { typing.hidden = !on; if (on) log.scrollTop = log.scrollHeight; }
    }).catch(() => {});
  }

  function when(iso) {
    const d = new Date(iso);
    return d.toLocaleString([], { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
  }
  function add(m) {
    if (m.id <= last) return;
    last = m.id;
    const div = document.createElement('div');
    div.className = 'sc-msg ' + (m.sender === 'user' ? 'sc-me' : 'sc-them');
    const who = document.createElement('span'); who.className = 'sc-meta'; who.textContent = (m.sender === 'user' ? 'You' : m.author) + ' · ' + when(m.created_at);
    const p = document.createElement('p'); p.textContent = m.body;
    div.append(p, who); log.appendChild(div);
  }
  function setBadge(n) { badge.hidden = !n; badge.textContent = n || ''; }
  function load(seen) {
    return fetch(box.dataset.url + '?after=' + last + '&seen=' + (seen ? 1 : 0), { headers: { Accept: 'application/json' } })
      .then(r => r.json()).then(d => {
        const before = last; (d.messages || []).forEach(add);
        if (last !== before) { log.scrollTop = log.scrollHeight; if ((d.messages || []).some(m => m.sender !== 'user')) typing.hidden = true; }
        if (seen) setBadge(0);
        loaded = true;
      }).catch(() => {});
  }
  function open() {
    panel.hidden = false; toggle.setAttribute('aria-expanded', 'true');
    try { sessionStorage.setItem('sc-open', '1'); } catch (e) {}
    load(true).then(() => { log.scrollTop = log.scrollHeight; ta.focus(); });
    clearInterval(timer); timer = setInterval(() => { if (!document.hidden) load(true); }, 5000);
    clearInterval(typingTimer); checkTyping(); typingTimer = setInterval(checkTyping, 2000);
  }
  function close() {
    panel.hidden = true; toggle.setAttribute('aria-expanded', 'false');
    try { sessionStorage.removeItem('sc-open'); } catch (e) {}
    clearInterval(timer); timer = setInterval(poll, 60000);
    clearInterval(typingTimer); typing.hidden = true;
  }
  function poll() {
    if (document.hidden) return;
    fetch(box.dataset.unreadUrl, { headers: { Accept: 'application/json' } }).then(r => r.json()).then(d => {
      const before = parseInt(badge.textContent || '0', 10) || 0;
      setBadge(d.unread);
      // desktop pop-up for a new support answer, if this person wants them
      if (d.unread > before && 'Notification' in window && Notification.permission === 'granted' && (window.FS_NOTIFY || {}).support !== false) {
        const n = new Notification('Support', { body: 'You have a new answer in the support chat.' }); n.onclick = () => { window.focus(); open(); };
      }
    }).catch(() => {});
  }
  toggle.addEventListener('click', () => (panel.hidden ? open() : close()));
  box.querySelector('.sc-close').addEventListener('click', close);
  ta.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); form.requestSubmit(); } });
  form.addEventListener('submit', (e) => {
    e.preventDefault();
    const body = ta.value.trim(); if (!body) return;
    const btn = form.querySelector('button'); btn.disabled = true;
    const fd = new FormData(); fd.append('csrf', csrf); fd.append('body', body); fd.append('page', location.pathname);
    fetch(box.dataset.url + '?after=' + last, { method: 'POST', body: fd, headers: { Accept: 'application/json' } })
      .then(r => r.json().then(d => ({ ok: r.ok, d })))
      .then(({ ok, d }) => {
        if (!ok) { alertMsg(d.error || 'Not sent. Try again.'); return; }
        ta.value = ''; (d.messages || []).forEach(add); log.scrollTop = log.scrollHeight;
        if (!box.dataset.thanked) { box.dataset.thanked = '1'; note('Sent. We\'ll answer here as soon as we can.'); }
      })
      .catch(() => alertMsg('Not sent: check your connection and try again.'))
      .finally(() => { btn.disabled = false; ta.focus(); });
  });
  function note(t) { const n = document.createElement('p'); n.className = 'sc-note'; n.textContent = t; log.appendChild(n); log.scrollTop = log.scrollHeight; }
  function alertMsg(t) { const n = document.createElement('p'); n.className = 'sc-note sc-err'; n.textContent = t; log.appendChild(n); log.scrollTop = log.scrollHeight; }
  let wasOpen = false; try { wasOpen = sessionStorage.getItem('sc-open') === '1'; } catch (e) {}
  if (wasOpen) open(); else timer = setInterval(poll, 60000);
})();
