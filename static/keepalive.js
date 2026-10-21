// If the server is restarting (an update), keep the client's signature on the page and send it as soon as it's back.
(function () {
  const form = document.getElementById('sign-form');
  if (!form) return;
  const up = () => fetch('/healthz', { cache: 'no-store' }).then(r => r.ok, () => false);
  form.addEventListener('submit', async function (e) {
    if (e.defaultPrevented) return;            // the page's own checks stopped the submit
    e.preventDefault();
    const btn = document.getElementById('submit-btn');
    if (await up()) { HTMLFormElement.prototype.submit.call(form); return; }
    let bar = document.getElementById('restart-bar');
    if (!bar) {
      bar = document.createElement('div'); bar.id = 'restart-bar'; bar.className = 'flash flash-warn'; bar.setAttribute('role', 'status');
      form.parentNode.insertBefore(bar, form);
    }
    bar.textContent = 'Just a moment: the service is restarting. Your signature is saved on this page and will be sent automatically when it’s back. Please keep this page open.';
    bar.scrollIntoView({ behavior: 'smooth', block: 'center' });
    if (btn) btn.disabled = true;
    const started = Date.now();
    const timer = setInterval(async () => {
      if (await up()) {
        clearInterval(timer); bar.textContent = 'Back online. Sending your signature…';
        HTMLFormElement.prototype.submit.call(form);
      } else if (Date.now() - started > 10 * 60 * 1000) {
        clearInterval(timer); if (btn) btn.disabled = false;
        bar.textContent = 'The service is still unavailable. Please try again in a few minutes; your page stays as it is.';
      }
    }, 5000);
  });
})();
