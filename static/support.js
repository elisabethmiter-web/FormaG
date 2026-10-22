// Support chat with the platform owner: one ongoing conversation per person, checked every few seconds while open.
// A new conversation starts with the person confirming their company, name and email; "End chat" finishes it.
(function () {
  const box = document.getElementById('sc'); if (!box) return;
  const csrf = (document.querySelector('meta[name=csrf]') || {}).content || '';
  const toggle = box.querySelector('.sc-toggle'), panel = box.querySelector('.sc-panel'), log = box.querySelector('.sc-log');
  const form = box.querySelector('.sc-form'), ta = form.querySelector('textarea'), badge = box.querySelector('.sc-badge');
  const intro = box.querySelector('.sc-intro'), introErr = intro && intro.querySelector('.sc-intro-err');
  const endBtn = box.querySelector('.sc-end'), endBar = box.querySelector('.sc-endbar');
  const base = box.dataset.url.replace(/chat\.json.*$/, '');
  let last = 0, timer = null, typingTimer = null, needIntro = null, introFilled = false;
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
    if (m.sender === 'system') {
      const s = document.createElement('p'); s.className = 'sc-sys'; s.textContent = m.body + ' · ' + when(m.created_at);
      log.appendChild(s); return;
    }
    const div = document.createElement('div');
    div.className = 'sc-msg ' + (m.sender === 'user' ? 'sc-me' : 'sc-them');
    const who = document.createElement('span'); who.className = 'sc-meta'; who.textContent = (m.sender === 'user' ? 'You' : m.author) + ' · ' + when(m.created_at);
    const p = document.createElement('p'); p.textContent = m.body;
    div.append(p, who); log.appendChild(div);
  }
  function setBadge(n) { badge.hidden = !n; badge.textContent = n || ''; }

  // ---- which bottom part shows: the details form (new chat) or the message box (chat going)
  function setMode(need, defaults) {
    needIntro = !!need;
    if (intro) intro.hidden = !needIntro;
    form.hidden = needIntro;
    if (endBtn) endBtn.hidden = needIntro;
    if (endBar && needIntro) endBar.hidden = true;
    if (needIntro && intro && defaults && !introFilled) {
      introFilled = true;
      intro.elements.company.value = defaults.company || ''; intro.elements.name.value = defaults.name || '';
      intro.elements.email.value = defaults.email || ''; intro.elements.email_confirm.value = '';
    }
  }
  function focusInput() {
    if (needIntro && intro) { const f = [intro.elements.company, intro.elements.name, intro.elements.email, intro.elements.email_confirm].find(i => !i.value) || intro.elements.email_confirm; f.focus(); }
    else ta.focus();
  }
  function load(seen) {
    return fetch(box.dataset.url + '?after=' + last + '&seen=' + (seen ? 1 : 0), { headers: { Accept: 'application/json' } })
      .then(r => r.json()).then(d => {
        const before = last; (d.messages || []).forEach(add);
        if (last !== before) { log.scrollTop = log.scrollHeight; if ((d.messages || []).some(m => m.sender === 'owner')) typing.hidden = true; }
        if (seen) setBadge(0);
        if ('need_intro' in d && (d.need_intro !== needIntro)) setMode(d.need_intro, d.intro);
      }).catch(() => {});
  }
  function open() {
    panel.hidden = false; toggle.setAttribute('aria-expanded', 'true');
    try { sessionStorage.setItem('sc-open', '1'); } catch (e) {}
    load(true).then(() => { log.scrollTop = log.scrollHeight; focusInput(); });
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

  // ---- details before a chat starts
  if (intro) intro.addEventListener('submit', (e) => {
    e.preventDefault();
    const btn = intro.querySelector('button'); btn.disabled = true;
    [intro.elements.company, intro.elements.name, intro.elements.email, intro.elements.email_confirm].forEach(i => i.removeAttribute('aria-invalid'));
    introErr.hidden = true;
    const fd = new FormData(intro); fd.append('csrf', csrf); fd.append('after', last);
    fetch(base + 'start.json', { method: 'POST', body: fd, headers: { Accept: 'application/json' } })
      .then(r => r.json().then(d => ({ ok: r.ok, d })))
      .then(({ ok, d }) => {
        if (!ok) {
          const errs = d.errors || {}; let first = null;
          Object.keys(errs).forEach(k => { const i = intro.elements[k]; if (i) { i.setAttribute('aria-invalid', 'true'); first = first || i; } });
          introErr.textContent = Object.values(errs).join(' ') || d.error || 'Please check your details.'; introErr.hidden = false;
          if (first) first.focus();
          return;
        }
        (d.messages || []).forEach(add); log.scrollTop = log.scrollHeight;
        introFilled = false; setMode(false); delete box.dataset.thanked;
        note('Thanks! Now type your question below.'); ta.focus();
      })
      .catch(() => { introErr.textContent = 'Not sent: check your connection and try again.'; introErr.hidden = false; })
      .finally(() => { btn.disabled = false; });
  });

  // ---- end the chat
  if (endBtn) endBtn.addEventListener('click', () => { endBar.hidden = false; endBar.querySelector('.sc-end-no').focus(); });
  if (endBar) {
    endBar.querySelector('.sc-end-no').addEventListener('click', () => { endBar.hidden = true; ta.focus(); });
    endBar.querySelector('.sc-end-yes').addEventListener('click', () => {
      const fd = new FormData(); fd.append('csrf', csrf);
      fetch(base + 'end.json', { method: 'POST', body: fd, headers: { Accept: 'application/json' } })
        .then(r => r.json()).then(() => load(true)).then(() => {
          endBar.hidden = true;
          note('You ended the chat. Thank you! If you need help again, confirm your details below to start a new chat.');
          introFilled = false; setMode(true);
        }).catch(() => alertMsg('Couldn\'t end the chat: check your connection and try again.'));
    });
  }

  form.addEventListener('submit', (e) => {
    e.preventDefault();
    const body = ta.value.trim(); if (!body) return;
    const btn = form.querySelector('button'); btn.disabled = true;
    const fd = new FormData(); fd.append('csrf', csrf); fd.append('body', body); fd.append('page', location.pathname);
    fetch(box.dataset.url + '?after=' + last, { method: 'POST', body: fd, headers: { Accept: 'application/json' } })
      .then(r => r.json().then(d => ({ ok: r.ok, d })))
      .then(({ ok, d }) => {
        if (!ok) {
          if (d.need_intro) { introFilled = false; setMode(true, d.intro); note('Please confirm your details first. Your message is still in the box.'); focusInput(); return; }
          alertMsg(d.error || 'Not sent. Try again.'); return;
        }
        ta.value = ''; (d.messages || []).forEach(add); log.scrollTop = log.scrollHeight;
        if (!box.dataset.thanked) { box.dataset.thanked = '1'; note('Sent. We\'ll answer here as soon as we can.'); }
      })
      .catch(() => alertMsg('Not sent: check your connection and try again.'))
      .finally(() => { btn.disabled = false; if (!needIntro) ta.focus(); });
  });
  function note(t) { const n = document.createElement('p'); n.className = 'sc-note'; n.textContent = t; log.appendChild(n); log.scrollTop = log.scrollHeight; }
  function alertMsg(t) { const n = document.createElement('p'); n.className = 'sc-note sc-err'; n.textContent = t; log.appendChild(n); log.scrollTop = log.scrollHeight; }
  let wasOpen = false; try { wasOpen = sessionStorage.getItem('sc-open') === '1'; } catch (e) {}
  if (wasOpen) open(); else timer = setInterval(poll, 60000);
})();
