// Signature box editor: boxes are stored as fractions of the page (x, y from the top-left corner).
(function () {
  const boxes = (window.PLACE_BOXES || []).map(b => Object.assign({}, b));
  const labels = window.BOX_LABELS || {};
  const SIZE = { signature: [0.30, 0.05], name: [0.26, 0.026], date: [0.16, 0.026] };
  let armed = null;
  const pages = [...document.querySelectorAll('.place-page')];
  const who = () => +(document.querySelector('input[name=who]:checked') || { value: 1 }).value;
  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

  function arm(kind) {
    armed = armed === kind ? null : kind;
    document.querySelectorAll('.tool').forEach(t => t.classList.toggle('active', t.dataset.kind === armed));
    pages.forEach(p => p.classList.toggle('armed', !!armed && !p.classList.contains('rotated')));
  }
  document.querySelectorAll('.tool').forEach(t => t.addEventListener('click', () => arm(t.dataset.kind)));

  function render() {
    document.querySelectorAll('.pbox').forEach(e => e.remove());
    boxes.forEach((b, i) => {
      const page = pages[b.page]; if (!page) return;
      const el = document.createElement('div');
      el.className = 'pbox s' + b.signer;
      Object.assign(el.style, { left: b.x * 100 + '%', top: b.y * 100 + '%', width: b.w * 100 + '%', height: b.h * 100 + '%' });
      el.innerHTML = '<span></span><button type="button" class="del" aria-label="Remove box">×</button><span class="grip"></span>';
      el.firstChild.textContent = (labels[b.kind] || b.kind) + (b.signer === 2 ? ' · owner 2' : '');
      el.querySelector('.del').addEventListener('click', e => { e.stopPropagation(); boxes.splice(i, 1); render(); });
      drag(el, b, page);
      page.appendChild(el);
    });
    document.getElementById('box-count').textContent = boxes.length ? boxes.length + ' box' + (boxes.length === 1 ? '' : 'es') : 'No boxes yet';
  }

  function drag(el, b, page) {
    el.addEventListener('pointerdown', e => {
      if (e.target.classList.contains('del')) return;
      e.preventDefault(); e.stopPropagation();
      const r = page.getBoundingClientRect(), resize = e.target.classList.contains('grip');
      const sx = e.clientX, sy = e.clientY, o = Object.assign({}, b);
      el.setPointerCapture(e.pointerId);
      function move(ev) {
        const dx = (ev.clientX - sx) / r.width, dy = (ev.clientY - sy) / r.height;
        if (resize) { b.w = clamp(o.w + dx, 0.03, 1 - b.x); b.h = clamp(o.h + dy, 0.012, 1 - b.y); }
        else { b.x = clamp(o.x + dx, 0, 1 - b.w); b.y = clamp(o.y + dy, 0, 1 - b.h); }
        Object.assign(el.style, { left: b.x * 100 + '%', top: b.y * 100 + '%', width: b.w * 100 + '%', height: b.h * 100 + '%' });
      }
      function up() { el.removeEventListener('pointermove', move); el.removeEventListener('pointerup', up); }
      el.addEventListener('pointermove', move); el.addEventListener('pointerup', up);
    });
  }

  pages.forEach((page, n) => page.addEventListener('click', e => {
    if (!armed || page.classList.contains('rotated') || e.target.closest('.pbox')) return;
    const r = page.getBoundingClientRect(), [w, h] = SIZE[armed];
    const x = clamp((e.clientX - r.left) / r.width - w / 2, 0, 1 - w), y = clamp((e.clientY - r.top) / r.height - h / 2, 0, 1 - h);
    boxes.push({ page: n, kind: armed, signer: who(), x: +x.toFixed(5), y: +y.toFixed(5), w, h });
    render();
  }));

  document.getElementById('place-form').addEventListener('submit', () => {
    document.getElementById('placements_json').value = JSON.stringify(boxes.map(b => ({
      page: b.page, kind: b.kind, signer: b.signer, x: +b.x.toFixed(5), y: +b.y.toFixed(5), w: +b.w.toFixed(5), h: +b.h.toFixed(5) })));
  });
  render();
})();
