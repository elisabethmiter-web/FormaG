// Box editor: signature, name, date, initials and job detail boxes, stored as fractions of the page
// (x, y from the top-left corner). Job detail boxes also carry a label and an optional default.
(function () {
  const boxes = (window.PLACE_BOXES || []).map(b => Object.assign({}, b));
  const labels = window.BOX_LABELS || {};
  const SIZE = { signature: [0.30, 0.05], name: [0.26, 0.026], date: [0.16, 0.026], initials: [0.08, 0.035], jobfield: [0.32, 0.026] };
  let armed = null, selected = null;
  const pages = [...document.querySelectorAll('.place-page')];
  const who = () => +(document.querySelector('input[name=who]:checked') || { value: 1 }).value;
  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
  const jdLabel = document.getElementById('jd-label'), jdDefault = document.getElementById('jd-default');
  const jdEditing = document.getElementById('jd-editing'), help = document.getElementById('place-help');
  const helpText = help ? help.textContent : '';

  function say(msg) { if (help) { help.textContent = msg || helpText; help.classList.toggle('jf-warn', !!msg); } }

  function arm(kind) {
    if (kind === 'jobfield' && armed !== 'jobfield' && jdLabel && !jdLabel.value.trim()) {
      say('First type what goes in the box, for example “Job address”.'); jdLabel.focus(); return;
    }
    say('');
    select(null);
    armed = armed === kind ? null : kind;
    document.querySelectorAll('.tool').forEach(t => t.classList.toggle('active', t.dataset.kind === armed));
    pages.forEach(p => p.classList.toggle('armed', !!armed && !p.classList.contains('rotated')));
  }
  document.querySelectorAll('.tool').forEach(t => t.addEventListener('click', () => arm(t.dataset.kind)));

  function boxText(b) {
    if (b.kind === 'jobfield') return (b.label || 'Job detail') + (b.default ? ' (' + b.default + ')' : '');
    return (labels[b.kind] || b.kind) + (b.signer === 2 ? ' · owner 2' : '');
  }

  function select(b) {
    selected = b && b.kind === 'jobfield' ? b : null;
    if (jdEditing) jdEditing.hidden = !selected;
    if (selected) { jdLabel.value = selected.label || ''; jdDefault.value = selected.default || ''; }
    document.querySelectorAll('.pbox').forEach(e => e.classList.toggle('sel', !!selected && e._box === selected));
  }
  function editSelected() {
    if (!selected) return;
    const v = jdLabel.value.trim();
    if (v) selected.label = v.slice(0, 40);
    selected.default = jdDefault.value.trim().slice(0, 200);
    document.querySelectorAll('.pbox').forEach(e => { if (e._box === selected) e.firstChild.textContent = boxText(selected); });
  }
  if (jdLabel) { jdLabel.addEventListener('input', () => { say(''); editSelected(); }); jdDefault.addEventListener('input', editSelected); }
  const done = document.getElementById('jd-done');
  if (done) done.addEventListener('click', () => { select(null); jdLabel.value = ''; jdDefault.value = ''; });

  function render() {
    document.querySelectorAll('.pbox').forEach(e => e.remove());
    boxes.forEach((b, i) => {
      const page = pages[b.page]; if (!page) return;
      const el = document.createElement('div');
      el.className = 'pbox s' + (b.kind === 'jobfield' ? 'j' : b.signer) + ' k-' + b.kind;
      el._box = b;
      Object.assign(el.style, { left: b.x * 100 + '%', top: b.y * 100 + '%', width: b.w * 100 + '%', height: b.h * 100 + '%' });
      el.innerHTML = '<span></span><button type="button" class="del" aria-label="Remove box">×</button><span class="grip"></span>';
      el.firstChild.textContent = boxText(b);
      el.title = boxText(b);
      el.querySelector('.del').addEventListener('click', e => { e.stopPropagation(); boxes.splice(i, 1); if (selected === b) select(null); render(); });
      drag(el, b, page);
      page.appendChild(el);
    });
    select(selected);
    const n = boxes.length, reads = document.querySelectorAll('.jf-read:checked').length;
    document.getElementById('box-count').textContent = (n ? n + ' box' + (n === 1 ? '' : 'es') : 'No boxes yet')
      + (reads ? ' · ' + reads + ' page' + (reads === 1 ? '' : 's') + ' to confirm' : '');
  }

  function drag(el, b, page) {
    el.addEventListener('pointerdown', e => {
      if (e.target.classList.contains('del')) return;
      e.preventDefault(); e.stopPropagation();
      const r = page.getBoundingClientRect(), resize = e.target.classList.contains('grip');
      const sx = e.clientX, sy = e.clientY, o = Object.assign({}, b);
      let moved = false;
      el.setPointerCapture(e.pointerId);
      function move(ev) {
        const dx = (ev.clientX - sx) / r.width, dy = (ev.clientY - sy) / r.height;
        if (Math.abs(ev.clientX - sx) + Math.abs(ev.clientY - sy) > 3) moved = true;
        if (resize) { b.w = clamp(o.w + dx, 0.03, 1 - b.x); b.h = clamp(o.h + dy, 0.012, 1 - b.y); }
        else { b.x = clamp(o.x + dx, 0, 1 - b.w); b.y = clamp(o.y + dy, 0, 1 - b.h); }
        Object.assign(el.style, { left: b.x * 100 + '%', top: b.y * 100 + '%', width: b.w * 100 + '%', height: b.h * 100 + '%' });
      }
      function up() {
        el.removeEventListener('pointermove', move); el.removeEventListener('pointerup', up);
        if (!moved && b.kind === 'jobfield') { if (armed) arm(armed); select(b); jdLabel.focus(); }
      }
      el.addEventListener('pointermove', move); el.addEventListener('pointerup', up);
    });
  }

  pages.forEach((page, n) => page.addEventListener('click', e => {
    if (!armed || page.classList.contains('rotated') || e.target.closest('.pbox')) return;
    const r = page.getBoundingClientRect(), [w, h] = SIZE[armed];
    const x = clamp((e.clientX - r.left) / r.width - w / 2, 0, 1 - w), y = clamp((e.clientY - r.top) / r.height - h / 2, 0, 1 - h);
    const b = { page: n, kind: armed, signer: armed === 'jobfield' ? 1 : who(), x: +x.toFixed(5), y: +y.toFixed(5), w, h };
    if (armed === 'jobfield') {
      b.label = jdLabel.value.trim().slice(0, 40); b.default = jdDefault.value.trim().slice(0, 200);
      if (!b.label) { say('First type what goes in the box, for example “Job address”.'); jdLabel.focus(); return; }
    }
    boxes.push(b);
    render();
  }));

  // "Client must confirm they read this page": show the optional title when ticked
  document.querySelectorAll('.jf-read').forEach(cb => cb.addEventListener('change', () => {
    const t = cb.closest('.jf-pagebar').querySelector('.jf-read-title');
    if (t) { t.hidden = !cb.checked; if (cb.checked) t.focus(); }
    render();
  }));

  document.getElementById('place-form').addEventListener('submit', () => {
    document.getElementById('placements_json').value = JSON.stringify(boxes.map(b => {
      const o = { page: b.page, kind: b.kind, signer: b.signer, x: +b.x.toFixed(5), y: +b.y.toFixed(5), w: +b.w.toFixed(5), h: +b.h.toFixed(5) };
      if (b.kind === 'jobfield') { o.label = b.label; o.default = b.default || ''; }
      return o;
    }));
  });
  render();
})();
