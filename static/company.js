// Company page: live preview of the client's signing page and of the email/text wording, as you type.
(function () {
  const pv = document.querySelector('.co-preview'); if (!pv) return;
  const $ = (s) => document.querySelector(s);
  const forms = (pv.dataset.forms || '').split('|').filter(Boolean), sender = pv.dataset.sender;
  let tab = 'first';

  function company() { return ($('#name') || {}).value || ''; }
  function ink(hex) {   // dark or light text on the chosen colour
    const m = /^#?([\da-f]{2})([\da-f]{2})([\da-f]{2})$/i.exec(hex || ''); if (!m) return '#fff';
    const [r, g, b] = m.slice(1).map((x) => parseInt(x, 16) / 255).map((c) => (c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4)));
    return 0.2126 * r + 0.7152 * g + 0.0722 * b > 0.4 ? '#10131a' : '#ffffff';
  }
  function page() {
    const name = company() || 'Your company';
    $('#pv-name').textContent = name;
    $('#pv-title').textContent = name + ' has sent you 2 items to sign';
    const color = $('#reset_color') && $('#reset_color').checked ? '#24408e' : (($('#brand_color') || {}).value || '#24408e');
    const btn = $('#pv-btn'); btn.style.background = color; btn.style.color = ink(color);
    const cn = (document.querySelector('[name=contact_name]') || {}).value || '', ph = (document.querySelector('[name=contact_phone]') || {}).value || '', em = (document.querySelector('[name=contact_email]') || {}).value || '';
    const help = $('#pv-help');
    help.textContent = (ph || em) ? 'Questions? Contact ' + (cn || name) + [ph, em].filter(Boolean).map((x) => ' · ' + x).join('') : 'Questions? Contact ' + sender + ' (the person who sent it)';
    const rm = $('#remove_logo'); const logo = $('#pv-logo');
    if (rm && rm.checked) { logo.hidden = true; $('#pv-name').hidden = false; }
    else if (logo.getAttribute('src')) { logo.hidden = false; $('#pv-name').hidden = true; }
  }
  const logoInput = $('#logo');
  if (logoInput) logoInput.addEventListener('change', () => {
    const f = logoInput.files && logoInput.files[0]; if (!f) return;
    const r = new FileReader(); r.onload = () => { const img = $('#pv-logo'); img.src = r.result; img.hidden = false; $('#pv-name').hidden = true; }; r.readAsDataURL(f);
  });

  function fill(t, v) { return (t || '').replace(/\{(\w+)\}/g, (m, k) => (k in v ? v[k] : m)); }
  function mail() {
    const box = $('#pv-mail'); box.replaceChildren();
    const name = company() || 'Your company';
    const due = new Date(Date.now() + 2 * 864e5).toLocaleString([], { month: 'short', day: 'numeric', year: 'numeric', hour: 'numeric', minute: '2-digit' });
    const label = name + ' – ' + forms.slice(0, 2).join(' & ');
    const v = { client: 'Sam', client_full: 'Sam Lee', company: name, sender: sender + (sender.toLowerCase() !== name.toLowerCase() ? '\n' + name : ''),
      forms: forms.map((f) => '  - ' + f).join('\n'), due: tab === 'reminder' ? '' : 'Please complete by ' + due + '.', link: '\u0000LINK\u0000' };
    const get = (n) => (document.querySelector('[name=' + n + ']') || {}).value || '';
    let subject = '', body = '';
    if (tab === 'text') body = fill(get('msg_text'), Object.assign({}, v, { forms: forms.join(', '), sender }));
    else { subject = fill(get(tab === 'first' ? 'msg_subject' : 'msg_rem_subject'), v); body = fill(get(tab === 'first' ? 'msg_body' : 'msg_rem_body'), v); }
    if (!body.includes('\u0000LINK\u0000')) body += (tab === 'text' ? ' ' : '\n\n') + '\u0000LINK\u0000';
    body = body.replace(/\n{3,}/g, '\n\n').trim();
    if (subject) { const s = document.createElement('span'); s.className = 'pv-subj'; s.textContent = 'Subject: ' + subject; box.append(s); }
    body.split('\u0000LINK\u0000').forEach((part, i, arr) => {
      box.append(document.createTextNode(part));
      if (i < arr.length - 1) {
        if (tab === 'text') box.append(document.createTextNode('https://…/s/abc123'));
        else { const a = document.createElement('span'); a.className = 'pv-link'; a.textContent = label; a.title = 'Clickable button in the email'; box.append(a); }
      }
    });
  }
  document.querySelectorAll('[data-pvtab]').forEach((a) => a.addEventListener('click', (e) => {
    e.preventDefault(); tab = a.dataset.pvtab;
    document.querySelectorAll('[data-pvtab]').forEach((x) => x.classList.toggle('active', x === a)); mail();
  }));
  document.querySelectorAll('[data-pvmail]').forEach((el) => el.addEventListener('focus', () => {
    const t = el.dataset.pvmail; if (t !== tab) { const a = document.querySelector('[data-pvtab="' + t + '"]'); if (a) a.click(); }
  }));
  document.addEventListener('input', () => { page(); mail(); });
  document.addEventListener('change', () => { page(); mail(); });
  page(); mail();
})();
