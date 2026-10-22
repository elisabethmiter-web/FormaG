// The client's answers are saved as they type, so they can close the page and finish later from the same link.
(function () {
  const form = document.getElementById('sign-form'); if (!form || !form.dataset.draft) return;
  const t = (s) => s;
  const status = document.getElementById('draft-status'), later = document.getElementById('save-later');
  const csrf = (form.querySelector('input[name=csrf]') || {}).value || '';
  const SKIP = new Set(['csrf', 'signature', 'consent', 'specs_json', 'signer_name']);
  let timer = null, lastSent = '';

  function collect() {
    const data = {};
    Array.from(form.elements).forEach((el) => {
      if (!el.name || SKIP.has(el.name) || el.type === 'file' || el.type === 'button' || el.type === 'submit') return;
      if (el.type === 'radio') { if (el.checked) data[el.name] = el.value; }
      else if (el.type === 'checkbox') data[el.name] = el.checked;
      else data[el.name] = el.value;
    });
    return data;
  }
  function restore(data) {
    let n = 0;
    Object.keys(data || {}).forEach((name) => {
      if (SKIP.has(name)) return;
      form.querySelectorAll('[name="' + CSS.escape(name) + '"]').forEach((el) => {
        if (el.type === 'radio') { if (el.value === data[name] && !form.querySelector('[name="' + CSS.escape(name) + '"]:checked')) { el.checked = true; n++; el.dispatchEvent(new Event('change', { bubbles: true })); } }
        else if (el.type === 'checkbox') { if (data[name] === true && !el.checked) { el.checked = true; n++; } }
        else if (!el.value && data[name]) { el.value = data[name]; n++; }
      });
    });
    return n;
  }
  function when(iso) { try { return new Date(iso).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' }); } catch (e) { return ''; } }
  function save(isLater) {
    const data = collect(), body = JSON.stringify({ data, later: !!isLater });
    if (!isLater && body === lastSent) return Promise.resolve();
    status.textContent = t('Saving…');
    return fetch(form.dataset.draft, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf }, body, credentials: 'same-origin', keepalive: true })
      .then((r) => r.json().then((d) => ({ ok: r.ok, d })))
      .then(({ ok, d }) => {
        if (!ok) throw new Error(d.error || 'not saved');
        lastSent = body;
        status.textContent = isLater
          ? t('Saved. You can close this page: open the same link any time to finish, and your answers will be here.')
          : t('Answers saved') + ' ✓ ' + when(d.saved_at);
      })
      .catch(() => { status.textContent = t("Couldn't save just now. Your answers are still on this page."); });
  }
  try {
    const restored = restore(JSON.parse(form.dataset.restore || '{}'));
    if (restored) status.textContent = t('Welcome back: your saved answers are filled in.');
  } catch (e) {}
  lastSent = JSON.stringify({ data: collect(), later: false });
  form.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(() => save(false), 1500); });
  form.addEventListener('change', () => { clearTimeout(timer); timer = setTimeout(() => save(false), 800); });
  if (later) later.addEventListener('click', () => { clearTimeout(timer); save(true); });
  window.addEventListener('pagehide', () => { const b = JSON.stringify({ data: collect(), later: false }); if (b !== lastSent) save(false); });
})();
