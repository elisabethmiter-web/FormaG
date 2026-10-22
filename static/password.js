// Every password box: Show/Hide button, Caps Lock warning, and a strength meter where a new password is chosen.
(function () {
  const COMMON = ['password', 'passw0rd', '12345678', '123456789', 'qwerty', 'letmein', 'welcome', 'iloveyou', 'admin', 'abc123', 'formsign', 'changeme'];
  function strength(v) {
    if (!v) return null;
    let s = 0;
    if (v.length >= 8) s++;
    if (v.length >= 12) s++;
    if (/[a-z]/.test(v) && /[A-Z]/.test(v)) s++;
    if (/\d/.test(v)) s++;
    if (/[^A-Za-z0-9]/.test(v)) s++;
    const low = v.toLowerCase();
    if (v.length < 8 || COMMON.some(c => low.includes(c)) || /^(.)\1+$/.test(v)) s = Math.min(s, 1);
    if (v.length < 8) return { n: 0, t: 'Too short: use at least 8 characters' };
    return [{ n: 1, t: 'Weak: easy to guess' }, { n: 1, t: 'Weak: easy to guess' }, { n: 2, t: 'Fair: add numbers or symbols' },
            { n: 3, t: 'Good' }, { n: 4, t: 'Strong' }, { n: 4, t: 'Strong' }][s];
  }
  document.querySelectorAll('input[type=password]').forEach((inp) => {
    if (inp.dataset.pw) return; inp.dataset.pw = '1';
    const wrap = document.createElement('span'); wrap.className = 'pw-wrap';
    inp.parentNode.insertBefore(wrap, inp); wrap.appendChild(inp);
    const btn = document.createElement('button');
    btn.type = 'button'; btn.className = 'pw-toggle'; btn.textContent = 'Show';
    btn.setAttribute('aria-label', 'Show password'); btn.setAttribute('aria-pressed', 'false');
    btn.addEventListener('click', () => {
      const show = inp.type === 'password';
      inp.type = show ? 'text' : 'password';
      btn.textContent = show ? 'Hide' : 'Show';
      btn.setAttribute('aria-label', show ? 'Hide password' : 'Show password'); btn.setAttribute('aria-pressed', String(show));
      inp.focus();
    });
    wrap.appendChild(btn);
    // put the password back to hidden before the form is sent, so browsers save it as a password
    if (inp.form) inp.form.addEventListener('submit', () => { inp.type = 'password'; });

    const caps = document.createElement('span'); caps.className = 'pw-caps'; caps.hidden = true; caps.setAttribute('role', 'status');
    caps.textContent = '⇪ Caps Lock is on';
    wrap.after(caps);
    const capsCheck = (e) => { if (e.getModifierState) caps.hidden = !e.getModifierState('CapsLock'); };
    inp.addEventListener('keydown', capsCheck); inp.addEventListener('keyup', capsCheck);
    inp.addEventListener('blur', () => { caps.hidden = true; });

    if (inp.autocomplete === 'new-password' && !inp.dataset.nometer && !/confirm/i.test(inp.name)) {
      const meter = document.createElement('span'); meter.className = 'pw-meter'; meter.hidden = true;
      meter.innerHTML = '<span class="pw-bars"><i></i><i></i><i></i><i></i></span><span class="pw-text"></span>';
      caps.after(meter);
      inp.addEventListener('input', () => {
        const r = strength(inp.value);
        meter.hidden = !r; if (!r) return;
        meter.dataset.level = r.n; meter.querySelector('.pw-text').textContent = r.t;
      });
    }
  });
})();
