// "Open in my email", "Text from my phone" and "Copy message": open the staff member's own app and log it on the packet.
(function () {
  const csrf = (document.querySelector('meta[name=csrf]') || {}).content || '';
  document.addEventListener('click', function (e) {
    const a = e.target.closest('.quick-send [data-via]');
    if (!a) return;
    const box = a.closest('.quick-send'), via = a.dataset.via;
    if (via === 'copy') {
      e.preventDefault();
      const done = () => { const t = a.textContent; a.textContent = 'Copied ✓'; setTimeout(() => (a.textContent = t), 1800); };
      if (navigator.clipboard) navigator.clipboard.writeText(a.dataset.text).then(done, () => window.prompt('Copy this message:', a.dataset.text));
      else window.prompt('Copy this message:', a.dataset.text);
    }
    const fd = new FormData();
    fd.append('csrf', csrf); fd.append('via', via); fd.append('kind', box.dataset.kind || 'send'); fd.append('who', box.dataset.who || '');
    if (navigator.sendBeacon) navigator.sendBeacon(box.dataset.nudge, fd);
    else fetch(box.dataset.nudge, { method: 'POST', body: fd, keepalive: true });
  });
})();
