// "Open in Email", "Text from my phone" and "Copy message": open the staff member's own app and log it on the packet.
(function () {
  const csrf = (document.querySelector('meta[name=csrf]') || {}).content || '';
  const isPhone = /Android|iPhone|iPad|iPod|Mobile/i.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  const isMac = /Macintosh|Mac OS X/.test(navigator.userAgent) && !isPhone;
  const canRich = !!(navigator.clipboard && navigator.clipboard.write && window.ClipboardItem);
  const pasteKey = /Mac|iPhone|iPad/.test(navigator.platform) ? '⌘V' : 'Ctrl+V';

  function toast(html, ms) {
    let t = document.getElementById('qs-toast');
    if (!t) { t = document.createElement('div'); t.id = 'qs-toast'; t.setAttribute('role', 'status'); document.body.appendChild(t); }
    t.innerHTML = html; t.hidden = false;
    clearTimeout(t._h); t._h = setTimeout(() => { t.hidden = true; }, ms || 15000);
    t.onclick = () => { t.hidden = true; };
  }

  // Copies the message as rich text (clickable named link) with a plain-text fallback. Must run inside the click.
  function copyRich(box) {
    if (!canRich) return Promise.reject(new Error('no rich clipboard'));
    return navigator.clipboard.write([new ClipboardItem({
      'text/html': new Blob([box.dataset.html], { type: 'text/html' }),
      'text/plain': new Blob([box.dataset.text], { type: 'text/plain' }),
    })]);
  }
  function copyPlain(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) return navigator.clipboard.writeText(text);
    return Promise.reject(new Error('no clipboard'));
  }

  function phoneDialog(box) {
    let d = document.getElementById('qs-phone');
    if (!d) {
      d = document.createElement('dialog'); d.id = 'qs-phone'; d.className = 'qs-dialog';
      d.innerHTML = '<h2>Text from your phone</h2>' +
        '<p class="small">Scan this with your phone\'s camera. Your Messages app opens with the text ready to send to your client.</p>' +
        '<img alt="Code to scan with your phone" width="220" height="220">' +
        '<p class="small muted qs-short"></p>' +
        '<div class="row" style="gap:8px;flex-wrap:wrap"><button type="button" class="btn" data-act="copy">Copy text message</button>' +
        (isMac ? '<a class="btn btn-quiet" data-act="mac">Open Messages on this Mac</a>' : '') +
        '<button type="button" class="btn btn-quiet" data-act="close">Close</button></div>' +
        '<p class="small muted">The code works for 30 minutes.</p>';
      document.body.appendChild(d);
      d.addEventListener('click', (e) => {
        const act = (e.target.closest('[data-act]') || {}).dataset; if (!act) { if (e.target === d) d.close(); return; }
        if (act.act === 'close') d.close();
        if (act.act === 'copy') copyPlain(d.dataset.short).then(() => { e.target.textContent = 'Copied ✓'; }, () => window.prompt('Copy this text message:', d.dataset.short));
      });
    }
    d.dataset.short = box.dataset.short;
    d.querySelector('img').src = box.dataset.qr;
    d.querySelector('.qs-short').textContent = box.dataset.short;
    const mac = d.querySelector('[data-act=mac]'); if (mac) mac.href = 'sms:?&body=' + encodeURIComponent(box.dataset.short);
    d.querySelector('[data-act=copy]').textContent = 'Copy text message';
    if (d.showModal) d.showModal(); else d.setAttribute('open', '');
  }

  document.addEventListener('click', function (e) {
    const a = e.target.closest('.quick-send [data-via]');
    if (!a) return;
    const box = a.closest('.quick-send'), via = a.dataset.via;
    if (via === 'copy') {
      e.preventDefault();
      const done = () => { const t = a.textContent; a.textContent = 'Copied ✓'; setTimeout(() => (a.textContent = t), 1800); };
      copyRich(box).then(done, () => copyPlain(box.dataset.text).then(done, () => window.prompt('Copy this message:', box.dataset.text)));
    }
    if (via === 'email' && a.dataset.app && box.dataset.setapp) {   // picked another email: remember it as the default
      const f2 = new FormData(); f2.append('csrf', csrf); f2.append('email_app', a.dataset.app); f2.append('quiet', '1');
      fetch(box.dataset.setapp, { method: 'POST', body: f2, keepalive: true });
      const det = a.closest('details'); if (det) det.open = false;
    }
    if (via === 'phone' && !isPhone) {   // computers have no texting app: show a code to scan with the phone
      e.preventDefault();
      phoneDialog(box);
    }
    const fd = new FormData();
    fd.append('csrf', csrf); fd.append('via', via); fd.append('kind', box.dataset.kind || 'send'); fd.append('who', box.dataset.who || '');
    if (navigator.sendBeacon) navigator.sendBeacon(box.dataset.nudge, fd);
    else fetch(box.dataset.nudge, { method: 'POST', body: fd, keepalive: true });
  });

  // Arrived from "Create and open in Email": open the email right away (webmail was already opened in a new tab).
  const params = new URLSearchParams(location.search);
  const mode = params.get('compose');
  if (mode) {
    params.delete('compose');
    history.replaceState(null, '', location.pathname + (params.toString() ? '?' + params : '') + location.hash);
    const main = document.querySelector('#send-panel .quick-send a[data-main]');
    if (main && mode === '1') {
      if (main.getAttribute('href').startsWith('mailto:')) setTimeout(() => main.click(), 300);   // the email app opens
      else { const w = window.open(main.href, '_blank'); if (w) { const b = document.getElementById('compose-banner-wait'); if (b) b.hidden = true; } }
    }
    if (main) { main.classList.add('qs-pulse'); }
  }
})();
