// Desktop alerts while the app is open: checks for new activity every minute.
(function () {
  const btn = document.getElementById('alerts-btn');
  const supported = 'Notification' in window;
  function paint() {
    if (!btn) return;
    if (!supported) { btn.hidden = true; return; }
    btn.textContent = Notification.permission === 'granted' ? 'Desktop alerts on' : 'Turn on desktop alerts';
    btn.disabled = Notification.permission === 'granted' || Notification.permission === 'denied';
    if (Notification.permission === 'denied') btn.title = 'Alerts are blocked in this browser’s site settings';
  }
  if (btn) btn.addEventListener('click', () => Notification.requestPermission().then(paint));
  paint();
  if (!supported) return;
  let after = null;
  async function poll() {
    try {
      const r = await fetch('/updates.json' + (after ? '?after=' + encodeURIComponent(after) : ''), { cache: 'no-store' });
      if (!r.ok) return;
      const d = await r.json();
      if (after && Notification.permission === 'granted') {
        d.updates.slice(0, 3).forEach(u => { const n = new Notification(document.title.split(' · ').pop(), { body: u.text }); n.onclick = () => { window.focus(); location.href = u.url; }; });
      }
      after = d.now;
    } catch (e) { /* offline or restarting: try again next minute */ }
  }
  poll(); setInterval(poll, 60000);
})();
