// Follow-up page: rows tick off as you contact people, the progress bar fills, call notes and snooze without reloading.
(function () {
  const csrf = (document.querySelector('meta[name=csrf]') || {}).content || '';
  const prog = document.getElementById('fu-progress');
  function toast(text, ms) {
    let t = document.getElementById('db-toast');
    if (!t) { t = document.createElement('div'); t.id = 'db-toast'; t.setAttribute('role', 'status'); document.body.append(t); t.onclick = () => { t.hidden = true; }; }
    t.textContent = text; t.hidden = false; clearTimeout(t._h); t._h = setTimeout(() => { t.hidden = true; }, ms || 5000);
  }
  function paint() {
    if (!prog) return;
    const done = parseInt(prog.dataset.done, 10), total = parseInt(prog.dataset.total, 10);
    prog.querySelector('#fu-progress-text').textContent = done + ' of ' + total + ' followed up today';
    prog.querySelector('#fu-progress-note').textContent = done >= total ? 'All done for today 🎉' : (total - done) + ' to go';
    prog.querySelector('.bar span').style.width = (total ? Math.round((100 * done) / total) : 0) + '%';
    prog.querySelector('.bar').setAttribute('aria-valuenow', done);
    document.querySelectorAll('#fu-due, #fu-donelist').forEach((sec) => {
      const n = sec.querySelectorAll('.fu-row').length; const c = sec.querySelector('.fu-count'); if (c) c.textContent = n; sec.hidden = !n;
    });
  }
  function markDone(row) {
    if (!row || row.classList.contains('fu-done')) return;
    row.classList.add('fu-done');
    const last = row.querySelector('.fu-last'); if (last) last.textContent = 'just now';
    const list = document.querySelector('#fu-donelist .doc-list');
    if (prog && list && row.closest('#fu-due')) {
      prog.dataset.done = String(parseInt(prog.dataset.done, 10) + 1);
      setTimeout(() => { list.prepend(row); paint(); }, 900);
    }
  }
  function post(url, body) {
    return fetch(url, { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf, Accept: 'application/json' }, body: JSON.stringify(body) })
      .then((r) => r.json().then((d) => ({ ok: r.ok, d })));
  }
  // email / text / copy from the row counts as contact (quicksend.js logs it on the packet)
  document.addEventListener('click', (e) => {
    const a = e.target.closest('.fu-row .quick-send [data-via]');
    if (a && a.dataset.via !== 'copy') markDone(a.closest('.fu-row'));
  });
  function note(row, text) {
    return post(row.dataset.note, { note: text }).then(({ ok, d }) => { toast(ok ? d.message : d.error, 6000); if (ok) markDone(row); return ok; })
      .catch(() => toast("Couldn't save the note. Check your connection."));
  }
  document.querySelectorAll('[data-callnote]').forEach((b) => b.addEventListener('click', () => note(b.closest('.fu-row'), b.dataset.callnote)));
  document.querySelectorAll('.fu-note-input').forEach((inp) => inp.addEventListener('keydown', (e) => {
    if (e.key !== 'Enter') return; e.preventDefault(); const v = inp.value.trim(); if (!v) return;
    note(inp.closest('.fu-row'), v).then((ok) => { if (ok) inp.value = ''; });
  }));
  // snooze without leaving the page
  document.querySelectorAll('.fu-row form[action$="/snooze"] button[name=until]').forEach((b) => b.addEventListener('click', (e) => {
    const row = b.closest('.fu-row'); if (!row || !row.dataset.snooze) return;
    e.preventDefault();
    post(row.dataset.snooze, { until: b.value }).then(({ ok, d }) => {
      toast(ok ? d.message : (d.error || 'Not saved.'), 6000);
      if (!ok) return;
      const wasDue = !!row.closest('#fu-due');
      row.style.transition = 'opacity .3s'; row.style.opacity = '0';
      setTimeout(() => { row.remove(); if (prog) { prog.dataset.total = String(parseInt(prog.dataset.total, 10) - 1); if (!wasDue) prog.dataset.done = String(Math.max(0, parseInt(prog.dataset.done, 10) - 1)); } paint(); }, 320);
    });
  }));
})();
