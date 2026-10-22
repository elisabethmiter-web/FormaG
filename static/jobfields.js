// Job details and "I've read this page" / initials (jobfields.py). Three small parts, each runs only on its page:
//  1. signing page: read-this-page checklist, initials, markers on the page pictures, blocks signing until done
//  2. New packet: shows the job detail inputs used by the ticked forms
//  3. fillable form builder: insert {Job detail} placeholders into the text
(function () {
  const $ = (s, r) => (r || document).querySelector(s), $$ = (s, r) => Array.from((r || document).querySelectorAll(s));
  const tidy = (v) => v.toUpperCase().replace(/[^A-ZÀ-ɏ.'\- ]/g, '').slice(0, 8);
  const initialsOf = (name) => {
    const w = (name || '').split(/[\s-]+/).filter((x) => x && /\p{L}/u.test(x[0]));
    if (!w.length) return '';
    return (w.length === 1 ? w[0][0] : w[0][0] + w[w.length - 1][0]).toUpperCase();
  };

  // ------------------------------------------------ 1. signing page
  function signing() {
    const box = $('#jf-confirm'), form = $('#sign-form');
    if (!box || !form || box.dataset.ready) return;
    box.dataset.ready = '1';
    let marks = [];
    try { marks = JSON.parse(box.dataset.boxes || '[]'); } catch (e) { marks = []; }
    const anchors = $$('.doc-pages > a');
    const ini = $('#initials'), use = $('#jf-use-initials');
    const time = (d) => d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });

    // page markers: where the initials go, and which pages need a tick
    const markEls = [];
    marks.forEach((b) => {
      const a = anchors[b.page]; if (!a) return;
      a.classList.add('jf-has-mark');
      const m = document.createElement('span'); m.className = 'jf-mark';
      Object.assign(m.style, { left: b.x * 100 + '%', top: b.y * 100 + '%', width: b.w * 100 + '%', height: b.h * 100 + '%' });
      a.appendChild(m); markEls.push(m);
    });
    const tags = {};
    $$('.jf-check input[type=checkbox]', box).forEach((cb) => {
      const a = anchors[+cb.dataset.page]; if (!a) return;
      a.classList.add('jf-has-mark');
      const t = document.createElement('span'); t.className = 'jf-pagetag'; a.appendChild(t); tags[cb.dataset.page] = t;
      const go = cb.closest('label').querySelector('.jf-goto');
      if (go) { go.hidden = false; go.addEventListener('click', (e) => { e.preventDefault(); a.scrollIntoView({ behavior: 'smooth', block: 'start' }); }); }
    });
    function clearMsg() { const m = $('#submit-msg'); if (m) m.textContent = ''; }
    function paintMarks() {
      const v = ini ? ini.value.trim() : '';
      markEls.forEach((m) => { m.textContent = v || 'Initials'; m.classList.toggle('filled', !!v); });
    }
    function paintChecks() {
      $$('.jf-check input[type=checkbox]', box).forEach((cb) => {
        const lab = cb.closest('label'), at = lab.querySelector('input[type=hidden]'), shown = lab.querySelector('.jf-at');
        lab.classList.toggle('done', cb.checked);
        if (shown) shown.textContent = cb.checked && at && at.value ? '· ticked at ' + time(new Date(at.value)) : '';
        const t = tags[cb.dataset.page];
        if (t) { t.textContent = cb.checked ? '✓ Read' : 'Confirm you read this page ↓'; t.classList.toggle('ok', cb.checked); }
      });
    }
    $$('.jf-check input[type=checkbox]', box).forEach((cb) => cb.addEventListener('change', () => {
      const at = cb.closest('label').querySelector('input[type=hidden]');
      if (at) at.value = cb.checked ? new Date().toISOString() : '';
      const m = $('#submit-msg'); if (m) m.textContent = '';
      paintChecks();
    }));
    if (ini) {
      ini.addEventListener('input', () => { const p = ini.selectionStart, v = tidy(ini.value); if (v !== ini.value) { ini.value = v; try { ini.setSelectionRange(p, p); } catch (e) { /* */ } } paintMarks(); clearMsg(); });
      if (use) use.addEventListener('click', () => { ini.value = use.dataset.initials; paintMarks(); clearMsg(); ini.focus(); });
      if (use) document.addEventListener('input', (e) => {   // the name box comes later on the page
        if (!e.target || e.target.id !== 'signer_name') return;
        const s = initialsOf(e.target.value); use.dataset.initials = s; use.textContent = 'Use ' + s; use.hidden = !s; ini.placeholder = s || 'e.g. SL';
      });
    }
    paintMarks(); paintChecks();

    // runs before the signature script (registered first): stop signing until everything is done
    form.addEventListener('submit', (e) => {
      const left = $$('.jf-check input[type=checkbox]', box).filter((cb) => !cb.checked);
      let msg = '', focus = null;
      if (left.length) {
        msg = left.length === 1 ? 'Tick the box to confirm you’ve read page ' + (+left[0].dataset.page + 1) + '.'
          : 'Tick a box for each page you’ve read (' + left.length + ' still to tick).';
        focus = left[0];
      } else if (ini && !ini.value.trim()) { msg = 'Type your initials. They’re printed in the initials boxes.'; focus = ini; }
      if (msg) {
        e.preventDefault(); e.stopImmediatePropagation();
        const m = $('#submit-msg'); if (m) { m.textContent = msg; m.style.color = 'var(--bad)'; }
        box.scrollIntoView({ behavior: 'smooth', block: 'center' }); setTimeout(() => focus.focus({ preventScroll: true }), 300);
      }
    });
  }

  // ------------------------------------------------ 2. New packet
  function newPacket() {
    const sec = $('#step-jobdetails'); if (!sec || sec.dataset.ready) return;
    sec.dataset.ready = '1';
    document.documentElement.classList.add('jd-js');
    const ticked = () => new Set($$('input[name=template_ids][type=checkbox]').filter((c) => c.checked).map((c) => c.value));
    function update() {
      const on = ticked(); let any = false;
      $$('.jd-row, .jd-builtins', sec).forEach((row) => {
        const show = (row.dataset.tids || '').split(' ').some((t) => on.has(t));
        row.hidden = !show; any = any || show;
        const inp = $('input', row);
        if (inp) inp.required = show && inp.dataset.required === '1';
      });
      sec.hidden = !any;
    }
    document.addEventListener('change', (e) => { if (e.target && e.target.name === 'template_ids') update(); });
    update();
  }

  // ------------------------------------------------ 3. fillable form builder
  function builder() {
    const body = $('#body'), chips = $('#jf-chips'); if (!body || !chips || chips.dataset.ready) return;
    chips.dataset.ready = '1';
    chips.hidden = false;
    const found = $('#jf-found');
    function insert(text, back) {
      const s = body.selectionStart ?? body.value.length, e = body.selectionEnd ?? s;
      body.value = body.value.slice(0, s) + text + body.value.slice(e);
      body.focus(); const p = s + text.length - (back || 0); body.setSelectionRange(p, p); list();
    }
    function list() {
      if (!found) return;
      const names = []; const re = /\{([A-Za-z0-9#][^{}\n]{0,240})\}/g; let m;
      while ((m = re.exec(body.value))) { const l = m[1].split('=')[0].trim(); if (l && !names.includes(l)) names.push(l); }
      found.textContent = names.length ? 'Job details in this text: ' + names.join(', ') + '. You fill them in on New packet.' : '';
    }
    $$('button[data-insert]', chips).forEach((b) => b.addEventListener('click', () => insert('{' + b.dataset.insert + '}')));
    const other = $('button[data-other]', chips); if (other) other.addEventListener('click', () => insert('{}', 1));
    body.addEventListener('input', list); list();
  }

  function start() { signing(); newPacket(); builder(); }
  start();   // now: the signing checks must be registered before the signature script's
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
})();
