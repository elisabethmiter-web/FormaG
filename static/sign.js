// Signature pad: draw with pointer, or type a name rendered in a script face. Produces a PNG data URL.
(function () {
  const form = document.getElementById('sign-form');
  if (!form) return;
  const canvas = document.getElementById('sig-pad');
  const ctx = canvas.getContext('2d');
  const typed = document.getElementById('sig-typed');
  const nameInput = document.getElementById('signer_name');
  const hidden = document.getElementById('signature');
  const help = document.getElementById('sig-help');
  if (document.fonts && document.fonts.load) document.fonts.load('600 64px Caveat').catch(() => {});
  let mode = 'draw', drawing = false, hasInk = false, last = null;

  function resize() {
    const r = canvas.getBoundingClientRect(), dpr = window.devicePixelRatio || 1;
    const prev = hasInk ? canvas.toDataURL() : null;
    canvas.width = r.width * dpr; canvas.height = r.height * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.lineWidth = 2.4; ctx.lineCap = 'round'; ctx.lineJoin = 'round'; ctx.strokeStyle = '#1b2a5c';
    if (prev) { const img = new Image(); img.onload = () => ctx.drawImage(img, 0, 0, r.width, r.height); img.src = prev; }
  }
  resize();
  window.addEventListener('resize', resize);

  function pos(e) { const r = canvas.getBoundingClientRect(); return {x: e.clientX - r.left, y: e.clientY - r.top}; }
  canvas.addEventListener('pointerdown', e => { drawing = true; document.getElementById('submit-msg').textContent = ''; last = pos(e); canvas.setPointerCapture(e.pointerId);
    ctx.beginPath(); ctx.arc(last.x, last.y, 1.1, 0, Math.PI * 2); ctx.fillStyle = '#1b2a5c'; ctx.fill(); hasInk = true; });
  canvas.addEventListener('pointermove', e => { if (!drawing) return; const p = pos(e);
    ctx.beginPath(); ctx.moveTo(last.x, last.y); ctx.lineTo(p.x, p.y); ctx.stroke(); last = p; hasInk = true; });
  ['pointerup', 'pointercancel', 'pointerleave'].forEach(t => canvas.addEventListener(t, () => drawing = false));

  function clearPad() { ctx.clearRect(0, 0, canvas.width, canvas.height); hasInk = false; }
  document.getElementById('sig-clear').onclick = () => { clearPad(); };

  function paintTyped() { typed.textContent = nameInput.value.trim() || 'Your name'; typed.style.opacity = nameInput.value.trim() ? 1 : .35; }
  nameInput.addEventListener('input', paintTyped);
  paintTyped();

  document.querySelectorAll('.sig-tabs button').forEach(b => b.onclick = () => {
    mode = b.dataset.mode;
    document.querySelectorAll('.sig-tabs button').forEach(x => x.setAttribute('aria-selected', x === b));
    document.getElementById('sig-draw').hidden = mode !== 'draw';
    document.getElementById('sig-type').hidden = mode !== 'type';
    document.getElementById('sig-clear').hidden = mode !== 'draw';
    help.textContent = mode === 'draw' ? 'Use your mouse, trackpad or finger to sign in the box.' : 'We use the full name you typed above.';
    if (mode === 'draw') resize();
  });

  function trimmedPng(src) {
    // Crop transparent margins so the signature sits cleanly on the PDF.
    const w = src.width, h = src.height, d = src.getContext('2d').getImageData(0, 0, w, h).data;
    let x0 = w, y0 = h, x1 = 0, y1 = 0;
    for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) if (d[(y * w + x) * 4 + 3] > 10) {
      if (x < x0) x0 = x; if (x > x1) x1 = x; if (y < y0) y0 = y; if (y > y1) y1 = y; }
    if (x1 <= x0 || y1 <= y0) return null;
    const pad = 12, out = document.createElement('canvas');
    out.width = x1 - x0 + pad * 2; out.height = y1 - y0 + pad * 2;
    out.getContext('2d').drawImage(src, x0, y0, x1 - x0, y1 - y0, pad, pad, x1 - x0, y1 - y0);
    return out.toDataURL('image/png');
  }

  function typedPng() {
    const name = nameInput.value.trim(); if (!name) return null;
    const c = document.createElement('canvas'), x = c.getContext('2d');
    const font = '600 64px Caveat, "Segoe Script", "Brush Script MT", cursive';
    x.font = font; const w = Math.ceil(x.measureText(name).width) + 40;
    c.width = w; c.height = 110; x.font = font; x.fillStyle = '#1b2a5c'; x.textBaseline = 'middle';
    x.fillText(name, 20, 58);
    return trimmedPng(c);
  }

  form.addEventListener('submit', e => {
    const data = mode === 'draw' ? (hasInk ? trimmedPng(canvas) : null) : typedPng();
    const msg = document.getElementById('submit-msg');
    if (!data) { e.preventDefault(); msg.textContent = mode === 'draw' ? 'Please sign in the box first.' : 'Type your full name first.'; msg.style.color = 'var(--bad)'; return; }
    if (!document.getElementById('consent').checked) { e.preventDefault(); msg.textContent = 'Please tick the agreement box.'; msg.style.color = 'var(--bad)'; return; }
    hidden.value = data;
    const btn = document.getElementById('submit-btn'); btn.disabled = true; btn.textContent = 'Signing…';
  });
})();
