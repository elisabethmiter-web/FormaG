// Team page: edit in place, instant access changes, sign-in switch with Undo, invite helpers, weekly sparklines.
(function () {
  const csrf = (document.querySelector('meta[name=csrf]') || {}).content || '';
  function toast(html, ms, undo) {
    let t = document.getElementById('db-toast');
    if (!t) { t = document.createElement('div'); t.id = 'db-toast'; t.setAttribute('role', 'status'); document.body.append(t); }
    t.innerHTML = html; t.hidden = false; clearTimeout(t._h); t._h = setTimeout(() => { t.hidden = true; }, ms || 5000);
    if (undo) { const b = document.createElement('button'); b.className = 'db-undo'; b.type = 'button'; b.textContent = 'Undo'; b.onclick = (e) => { e.stopPropagation(); t.hidden = true; undo(); }; t.append(b); }
    t.onclick = () => { t.hidden = true; };
  }
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  function save(row, field, value) {
    return fetch(row.dataset.edit, { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf }, body: JSON.stringify({ field, value }) })
      .then((r) => r.json().then((d) => ({ ok: r.ok, d })))
      .then(({ ok, d }) => { toast(esc(ok ? d.message : d.error), ok ? 4000 : 7000); return ok; })
      .catch(() => { toast("Couldn't save. Check your connection."); return false; });
  }

  // ---- edit name and email in place
  document.querySelectorAll('.inline-edit').forEach((el) => {
    const row = el.closest('tr');
    const start = () => {
      if (el.isContentEditable) return;
      el.dataset.old = el.textContent.trim();
      el.textContent = el.dataset.old; el.contentEditable = 'true'; el.classList.add('editing'); el.focus();
      const r = document.createRange(); r.selectNodeContents(el); const s = getSelection(); s.removeAllRanges(); s.addRange(r);
    };
    const finish = (keep) => {
      if (!el.isContentEditable) return;
      el.contentEditable = 'false'; el.classList.remove('editing');
      const v = el.textContent.trim(), old = el.dataset.old;
      const paint = (txt) => { if (el.dataset.field === 'name') { el.textContent = ''; const b = document.createElement('strong'); b.textContent = txt; el.append(b); } else el.textContent = txt; };
      if (!keep || !v || v === old) { paint(old); return; }
      paint(v);
      save(row, el.dataset.field, v).then((ok) => { if (!ok) paint(old); });
    };
    el.addEventListener('click', start);
    el.addEventListener('keydown', (e) => {
      if (!el.isContentEditable && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); start(); return; }
      if (el.isContentEditable && e.key === 'Enter') { e.preventDefault(); finish(true); }
      if (el.isContentEditable && e.key === 'Escape') { e.preventDefault(); finish(false); }
    });
    el.addEventListener('blur', () => finish(true));
  });

  // ---- access level saves as soon as it changes
  document.querySelectorAll('.role-select').forEach((sel) => {
    let old = sel.value;
    sel.addEventListener('change', () => {
      const row = sel.closest('tr');
      save(row, 'role', sel.value).then((ok) => { if (ok) old = sel.value; else sel.value = old; });
    });
  });

  // ---- can sign in: switch with Undo
  document.querySelectorAll('.active-toggle').forEach((cb) => {
    cb.addEventListener('change', () => {
      const row = cb.closest('tr'), on = cb.checked;
      save(row, 'active', on ? '1' : '0').then((ok) => {
        if (!ok) { cb.checked = !on; return; }
        row.classList.toggle('muted', !on);
        toast(esc(row.querySelector('[data-field=name]').textContent.trim()) + (on ? ' can sign in again.' : ' can no longer sign in.'), 8000,
          () => { cb.checked = !on; save(row, 'active', on ? '0' : '1').then(() => row.classList.toggle('muted', on)); });
      });
    });
  });

  // ---- add someone: invite link or temporary password
  const form = document.getElementById('add-form');
  if (form) {
    const pw = form.querySelector('.pw-field'), btn = document.getElementById('add-btn'), input = form.querySelector('#password');
    const sync = () => {
      const how = (form.querySelector('input[name=how]:checked') || {}).value;
      pw.hidden = how !== 'password'; input.required = how === 'password';
      btn.textContent = how === 'password' ? 'Create login' : 'Create invite link';
    };
    form.querySelectorAll('input[name=how]').forEach((r) => r.addEventListener('change', sync)); sync();
  }
  document.querySelectorAll('[data-copy]').forEach((b) => b.addEventListener('click', () => {
    const inp = document.querySelector(b.dataset.copy);
    const done = () => { const t = b.textContent; b.textContent = 'Copied ✓'; setTimeout(() => (b.textContent = t), 1600); };
    if (navigator.clipboard) navigator.clipboard.writeText(inp.value).then(done, () => { inp.select(); document.execCommand('copy'); done(); });
    else { inp.select(); document.execCommand('copy'); done(); }
  }));

  // ---- weekly sparklines (packets sent per week, last 8 weeks)
  document.querySelectorAll('.team-spark').forEach((el) => {
    const v = el.dataset.spark.split(',').map(Number), max = Math.max(1, ...v), W = 120, H = 28, bw = W / v.length;
    const NS = 'http://www.w3.org/2000/svg', svg = document.createElementNS(NS, 'svg');
    svg.setAttribute('viewBox', `0 0 ${W} ${H}`); svg.setAttribute('width', W); svg.setAttribute('height', H); svg.setAttribute('aria-hidden', 'true');
    v.forEach((n, i) => {
      const h = n ? Math.max(3, (n / max) * (H - 2)) : 1.5, r = document.createElementNS(NS, 'rect');
      r.setAttribute('x', i * bw + 2); r.setAttribute('width', bw - 4); r.setAttribute('y', H - h); r.setAttribute('height', h); r.setAttribute('rx', 2);
      r.setAttribute('class', n ? 'spark-bar' + (i === v.length - 1 ? ' now' : '') : 'spark-zero');
      const t = document.createElementNS(NS, 'title'); t.textContent = n + ' sent ' + (i === v.length - 1 ? 'this week' : (v.length - 1 - i) + ' week' + (v.length - 1 - i === 1 ? '' : 's') + ' ago'); r.append(t);
      svg.append(r);
    });
    el.append(svg);
  });
})();
