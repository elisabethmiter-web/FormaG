// New packet: quick-start chips, a live summary of what the client will get, and step progress.
(function () {
  const form = document.getElementById('packet-form'); if (!form) return;
  const $ = (s, r) => (r || document).querySelector(s), $$ = (s, r) => Array.from((r || document).querySelectorAll(s));
  const sum = document.getElementById('np-summary'); if (sum) sum.hidden = false;
  const name = $('#client_name'), email = $('#client_email');

  // ---- prefill from quick search ("New packet for …" links ?client=&email=)
  try {
    const qs = new URLSearchParams(location.search);
    if (qs.get('client') && !name.value) name.value = qs.get('client').slice(0, 200);
    if (email && qs.get('email') && !email.value) email.value = qs.get('email').slice(0, 200);
  } catch (e) { /* old browser */ }

  // ---- chips: recent clients fill the client; bundles tick their forms
  $$('.np-chip[data-client]').forEach((c) => c.addEventListener('click', () => {
    name.value = c.dataset.client; if (email && !email.value) email.value = c.dataset.email || '';
    if (email && c.dataset.email) email.value = c.dataset.email;
    name.dispatchEvent(new Event('input', { bubbles: true })); render();
    const forms = $('#step-forms'); if (forms) forms.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }));
  $$('.np-chip[data-bundle]').forEach((c) => c.addEventListener('click', () => {
    const b = $$('.bundle-btn')[parseInt(c.dataset.bundle, 10)]; if (b) b.click();
    $$('.np-chip[data-bundle]').forEach((x) => x.classList.toggle('btn-primary', x === c));
    setTimeout(render, 30);
  }));

  // ---- live summary
  const li = (t, cls) => { const e = document.createElement('li'); e.textContent = t; if (cls) e.className = cls; return e; };
  function render() {
    if (!sum) return;
    const missing = [];
    // client
    const c = $('#sum-client'); c.replaceChildren();
    const nm = name.value.trim();
    if (nm) {
      c.append(document.createTextNode(nm));
      if (email && email.value.trim()) c.append(document.createElement('br'), Object.assign(document.createElement('span'), { className: 'muted small', textContent: email.value.trim() }));
    } else { c.append(Object.assign(document.createElement('span'), { className: 'np-miss', textContent: 'Not entered yet' })); missing.push("the client's name"); }
    const two = $('input[name=signers]:checked'); const cos = $('#cosigner_name');
    if (two && two.value === '2') c.append(document.createElement('br'), Object.assign(document.createElement('span'), { className: 'small', textContent: '+ second owner: ' + ((cos && cos.value.trim()) || 'name not entered') }));
    const so = $('#so_number'); if (so && so.value.trim()) c.append(document.createElement('br'), Object.assign(document.createElement('span'), { className: 'small muted', textContent: 'SO# ' + so.value.trim() }));
    // forms
    const f = $('#sum-forms'); f.replaceChildren();
    const ul = document.createElement('ul');
    $$('input[name=template_ids]:checked').forEach((cb) => { const s = cb.closest('label').querySelector('strong'); ul.append(li(s ? s.textContent.trim() : 'Form')); });
    const files = $('#adhoc_files'); if (files && files.files) Array.from(files.files).forEach((x) => ul.append(li(x.name + ' (one-off file)')));
    $$('#approvals .approval-card, .approval-list .approval-card, fieldset.approval-card').forEach((card) => {
      if (card.closest('template')) return;
      const t = card.querySelector('input[name$="_title___N__"], input[id^="layout_title_"], input[id^="drawing_title_"]');
      ul.append(li((t && t.value) || (card.dataset.kind === 'layout' ? 'Layout approval' : 'Drawing approval')));
    });
    if (ul.children.length) f.append(ul); else { f.append(Object.assign(document.createElement('span'), { className: 'np-miss', textContent: 'Nothing picked yet' })); missing.push('at least one form or file'); }
    // options
    const o = $('#sum-options'); o.replaceChildren();
    const opts = [];
    if ($('input[name=access_code]') && $('input[name=access_code]').checked) opts.push('Access code required');
    if ($('#ask_files') && $('#ask_files').checked) opts.push('Asks for photos or files');
    const msg = $('#message'); if (msg && msg.value.trim()) opts.push('Note to the client');
    const exp = $('#expiry_days'); if (exp) opts.push('Link works ' + exp.value + ' days');
    const ou = document.createElement('ul'); opts.forEach((t) => ou.append(li(t))); o.append(ou);
    $('#sum-missing').textContent = missing.length ? 'Still needed: ' + missing.join(' and ') + '.' : 'Ready to send ✓';
    $('#sum-missing').classList.toggle('np-miss', !!missing.length);
    // steps
    const ok = { client: !!nm, forms: !!ul.children.length, options: true, send: !missing.length };
    $$('.np-steps a').forEach((a) => a.classList.toggle('ok', !!ok[a.dataset.step] && a.dataset.step !== 'options' || (a.dataset.step === 'options' && ok.client && ok.forms)));
  }
  form.addEventListener('input', render); form.addEventListener('change', render); form.addEventListener('click', () => setTimeout(render, 30));
  render();

  // ---- highlight the step you're on
  if ('IntersectionObserver' in window) {
    const links = $$('.np-steps a'), targets = links.map((a) => document.querySelector(a.getAttribute('href'))).filter(Boolean);
    const io = new IntersectionObserver((entries) => {
      entries.forEach((e) => { if (e.isIntersecting) links.forEach((a) => a.classList.toggle('on', a.getAttribute('href') === '#' + e.target.id)); });
    }, { rootMargin: '-30% 0px -60% 0px' });
    targets.forEach((t) => io.observe(t));
  }
})();
