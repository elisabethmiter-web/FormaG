// Signs out after a period without activity (shared computers). Warns a minute before; activity in any tab counts.
(function () {
  const el = document.getElementById('idle-cfg'); if (!el) return;
  const LIMIT = parseInt(el.dataset.seconds, 10) * 1000, PING = el.dataset.ping, OUT = el.dataset.out;
  const KEY = 'fs-last-activity';
  let lastPing = Date.now(), dlg = null, tick = null;
  const get = () => { try { return parseInt(localStorage.getItem(KEY), 10) || Date.now(); } catch (e) { return mem; } };
  let mem = Date.now();
  const set = (t) => { mem = t; try { localStorage.setItem(KEY, String(t)); } catch (e) {} };
  set(Date.now());
  function activity() {
    set(Date.now());
    if (dlg && dlg.open) return;
    if (Date.now() - lastPing > 4 * 60 * 1000) { lastPing = Date.now(); fetch(PING, { credentials: 'same-origin' }).catch(() => {}); }
  }
  ['mousedown', 'keydown', 'scroll', 'touchstart', 'mousemove'].forEach((ev) => document.addEventListener(ev, throttle(activity, 15000), { passive: true }));
  function throttle(fn, ms) { let t = 0; return () => { const n = Date.now(); if (n - t > ms) { t = n; fn(); } }; }
  function stay() { set(Date.now()); lastPing = Date.now(); fetch(PING, { credentials: 'same-origin' }).catch(() => {}); if (dlg) dlg.close(); }
  function warn(left) {
    if (!dlg) {
      dlg = document.createElement('dialog'); dlg.className = 'qs-dialog';
      dlg.innerHTML = '<h2>Still there?</h2><p>For safety you\'ll be signed out in <strong class="idle-left"></strong> seconds.</p>' +
        '<div class="row"><button type="button" class="btn btn-primary">Stay signed in</button></div>';
      dlg.querySelector('button').addEventListener('click', stay);
      document.body.appendChild(dlg);
    }
    dlg.querySelector('.idle-left').textContent = Math.max(0, Math.ceil(left / 1000));
    if (!dlg.open) { if (dlg.showModal) dlg.showModal(); else dlg.setAttribute('open', ''); }
  }
  tick = setInterval(() => {
    const idle = Date.now() - get();
    if (idle >= LIMIT) { clearInterval(tick); location.href = OUT; return; }
    if (idle >= LIMIT - 60000) warn(LIMIT - idle);
    else if (dlg && dlg.open) dlg.close();   // activity in another tab
  }, 1000);
})();
