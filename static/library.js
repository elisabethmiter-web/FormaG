/* Form library: instant search and tag filters, inline tags, drag (or ↑ ↓ / Alt+arrow) ordering of forms and
   of the forms inside each bundle, and a Quick look side panel. Without JavaScript the page still works:
   ?q= / ?tag= filter on the server, ↑ ↓ buttons and the tag form post normally. */
(function () {
  'use strict';
  var grid = document.getElementById('lib-grid');
  if (!grid) return;
  var csrfMeta = document.querySelector('meta[name=csrf]');
  var CSRF = csrfMeta ? csrfMeta.content : '';
  var isManager = grid.dataset.manager === '1';
  var search = document.getElementById('lib-search');
  var tagbar = document.getElementById('lib-tagbar');
  var statusEl = document.getElementById('lib-status');
  var clearBtn = document.getElementById('lib-clear');
  var none = document.getElementById('lib-none');
  var datalist = document.getElementById('lib-taglist');
  var savedEl = document.getElementById('lib-saved');
  var refreshers = [];  // re-enable/disable ↑ ↓ after filtering

  document.querySelectorAll('.lib-js').forEach(function (el) { el.hidden = false; });
  document.querySelectorAll('.lib-nojs').forEach(function (el) { el.hidden = true; });
  document.querySelectorAll('.lib-tagedit').forEach(function (el) { el.hidden = true; });

  function cards() { return Array.prototype.slice.call(grid.querySelectorAll('.form-card')); }
  function tagsOf(card) { try { return JSON.parse(card.dataset.tags || '[]'); } catch (e) { return []; } }
  function low(s) { return String(s || '').toLowerCase(); }

  function send(url, payload) {
    return fetch(url, {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': CSRF, 'X-Requested-With': 'fetch', 'Accept': 'application/json' },
      body: JSON.stringify(payload)
    }).then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); });
  }
  function say(el, msg) { if (!el) return; el.textContent = msg; clearTimeout(el._t); el._t = setTimeout(function () { el.textContent = ''; }, 2500); }

  // ------------------------------------------------------------------ search and tag filters
  var selected = [];
  try { selected = new URL(location.href).searchParams.getAll('tag'); } catch (e) { selected = []; }

  function matches(card, words, tags) {
    var mine = tagsOf(card).map(low);
    for (var i = 0; i < tags.length; i++) if (mine.indexOf(low(tags[i])) < 0) return false;
    if (!words.length) return true;
    var hay = (card.dataset.search || '') + ' ' + mine.join(' ');
    return words.every(function (w) { return hay.indexOf(w) >= 0; });
  }

  function allTags() {
    var counts = {}, spell = {};
    cards().forEach(function (c) { tagsOf(c).forEach(function (t) { var k = low(t); spell[k] = spell[k] || t; counts[k] = (counts[k] || 0) + 1; }); });
    return Object.keys(counts).sort(function (a, b) { return counts[b] - counts[a] || (a < b ? -1 : 1); }).map(function (k) { return spell[k]; });
  }

  function paintTagbar(words) {
    if (!tagbar) return;
    var tags = allTags();
    selected = selected.filter(function (s) { return tags.some(function (t) { return low(t) === low(s); }); });
    tagbar.querySelectorAll('.lib-tagfilter').forEach(function (a) { a.remove(); });
    tags.forEach(function (t) {
      var on = selected.some(function (s) { return low(s) === low(t); });
      var want = on ? selected : selected.concat([t]);
      var n = cards().filter(function (c) { return matches(c, words, want); }).length;
      var a = document.createElement('button');
      a.type = 'button'; a.className = 'lib-tagfilter' + (on ? ' on' : '') + (!n && !on ? ' zero' : '');
      a.dataset.tag = t; a.setAttribute('aria-pressed', on ? 'true' : 'false');
      a.setAttribute('aria-label', (on ? 'Remove filter ' : 'Filter by ') + t + ', ' + n + ' form' + (n === 1 ? '' : 's'));
      a.appendChild(document.createTextNode(t + ' '));
      var s = document.createElement('span'); s.className = 'lib-n'; s.textContent = n; a.appendChild(s);
      tagbar.appendChild(a);
    });
    tagbar.hidden = !tags.length;
    if (datalist) {
      datalist.innerHTML = '';
      tags.forEach(function (t) { var o = document.createElement('option'); o.value = t; datalist.appendChild(o); });
    }
  }

  function apply(push) {
    var q = search ? search.value.trim() : '';
    var words = low(q).split(/\s+/).filter(Boolean);
    paintTagbar(words);
    var all = cards(), shown = 0;
    all.forEach(function (c) { var m = matches(c, words, selected); c.hidden = !m; if (m) shown++; });
    var filtering = !!(words.length || selected.length);
    if (statusEl) statusEl.textContent = filtering ? 'Showing ' + shown + ' of ' + all.length + ' forms' : all.length + ' form' + (all.length === 1 ? '' : 's');
    if (clearBtn) clearBtn.hidden = !filtering;
    if (none) none.hidden = shown > 0;
    document.querySelectorAll('.lib-chip').forEach(function (ch) {
      ch.classList.toggle('on', selected.some(function (s) { return low(s) === low(ch.dataset.tag); }));
    });
    if (push !== false && window.history && history.replaceState) {
      try {
        var u = new URL(location.href);
        u.searchParams.delete('q'); u.searchParams.delete('tag');
        if (q) u.searchParams.set('q', q);
        selected.forEach(function (t) { u.searchParams.append('tag', t); });
        history.replaceState(null, '', u.pathname + u.search + u.hash);
      } catch (e) { /* ignore */ }
    }
    document.querySelectorAll('.lib-tag-param').forEach(function (i) { i.remove(); });
    refreshers.forEach(function (f) { f(); });
  }

  function toggleTag(t) {
    var i = -1;
    selected.forEach(function (s, k) { if (low(s) === low(t)) i = k; });
    if (i >= 0) selected.splice(i, 1); else selected.push(t);
    apply();
  }

  if (search) {
    search.addEventListener('input', function () { apply(); });
    search.form.addEventListener('submit', function (e) { e.preventDefault(); apply(); });
    search.addEventListener('keydown', function (e) { if (e.key === 'Escape' && search.value) { search.value = ''; apply(); } });
  }
  if (tagbar) tagbar.addEventListener('click', function (e) {
    var b = e.target.closest('.lib-tagfilter'); if (!b) return;
    e.preventDefault(); toggleTag(b.dataset.tag);
  });
  grid.addEventListener('click', function (e) {
    var ch = e.target.closest('.lib-chip');
    if (ch && !e.target.closest('.lib-chip-x')) { e.preventDefault(); toggleTag(ch.dataset.tag); }
  });
  function clearAll(e) { if (e) e.preventDefault(); selected = []; if (search) search.value = ''; apply(); if (search) search.focus(); }
  if (clearBtn) clearBtn.addEventListener('click', clearAll);
  document.querySelectorAll('.lib-clear-link').forEach(function (a) { a.addEventListener('click', clearAll); });

  // ------------------------------------------------------------------ inline tags (managers)
  function paintTags(card) {
    var box = card.querySelector('.lib-tags'); if (!box) return;
    var tags = tagsOf(card), editable = isManager && box.dataset.url;
    box.innerHTML = '';
    tags.forEach(function (t) {
      var ch = document.createElement('span');
      ch.className = 'chip lib-chip'; ch.dataset.tag = t; ch.setAttribute('role', 'button'); ch.tabIndex = 0;
      ch.title = 'Show forms tagged ' + t; ch.textContent = t;
      if (editable) {
        var x = document.createElement('button');
        x.type = 'button'; x.className = 'lib-chip-x'; x.textContent = '×';
        x.setAttribute('aria-label', 'Remove tag ' + t + ' from ' + card.dataset.name);
        x.addEventListener('click', function (e) { e.stopPropagation(); save(card, tagsOf(card).filter(function (y) { return low(y) !== low(t); })); });
        ch.appendChild(x);
      }
      ch.addEventListener('keydown', function (e) { if ((e.key === 'Enter' || e.key === ' ') && e.target === ch) { e.preventDefault(); toggleTag(t); } });
      box.appendChild(ch);
    });
    if (editable) {
      var inp = document.createElement('input');
      inp.type = 'text'; inp.className = 'lib-tagadd'; inp.placeholder = tags.length ? '+ tag' : '+ Add tag';
      inp.setAttribute('list', 'lib-taglist'); inp.maxLength = 30; inp.autocomplete = 'off';
      inp.setAttribute('aria-label', 'Add a tag to ' + card.dataset.name + ' (press Enter)');
      function commit() {
        var v = inp.value.split(',').map(function (s) { return s.trim(); }).filter(Boolean);
        if (!v.length) return;
        var cur = tagsOf(card);
        v.forEach(function (n) { if (!cur.some(function (c) { return low(c) === low(n); })) cur.push(n); });
        save(card, cur, true);
      }
      inp.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' || e.key === ',') { e.preventDefault(); commit(); }
        else if (e.key === 'Backspace' && !inp.value && tagsOf(card).length) { var c = tagsOf(card); c.pop(); save(card, c, true); }
        else if (e.key === 'Escape') { inp.value = ''; inp.blur(); }
      });
      inp.addEventListener('change', function () { if (datalist && Array.prototype.some.call(datalist.options, function (o) { return o.value === inp.value; })) commit(); });
      box.appendChild(inp);
    }
  }
  function save(card, tags, refocus) {
    var box = card.querySelector('.lib-tags');
    box.classList.add('busy');
    send(box.dataset.url, { tags: tags }).then(function (d) {
      card.dataset.tags = JSON.stringify(d.tags);
      paintTags(card); apply(false);
      if (refocus) { var i = card.querySelector('.lib-tagadd'); if (i) i.focus(); }
      say(savedEl, 'Tags saved.');
    }).catch(function () { alert('Tags could not be saved. Check your connection and try again.'); })
      .then(function () { box.classList.remove('busy'); });
  }
  cards().forEach(paintTags);

  // ------------------------------------------------------------------ ordering: drag, ↑ ↓ buttons, Alt+arrows
  function sortable(list, itemSel, url, onSaved) {
    if (!list || !url) return;
    function items() { return Array.prototype.slice.call(list.querySelectorAll(itemSel)); }
    function refresh() {
      var all = items().filter(function (x) { return !x.hidden; });
      all.forEach(function (it, i) {
        var up = it.querySelector('.lib-arrow[value=up]'), dn = it.querySelector('.lib-arrow[value=down]');
        if (up) up.disabled = i === 0; if (dn) dn.disabled = i === all.length - 1;
      });
    }
    function persist() {
      var ids = items().map(function (it) { return it.dataset.id; });
      list.classList.add('busy');
      return send(url, { ids: ids }).then(function () { say(onSaved.el, 'Order saved.'); if (onSaved.fn) onSaved.fn(ids); })
        .catch(function () { say(onSaved.el, 'Could not save the order — reload and try again.'); })
        .then(function () { list.classList.remove('busy'); });
    }
    function move(it, dir, focusSel) {
      // move past the next *visible* item, so ↑ ↓ behave as seen while a search or tag filter is on
      var all = items().filter(function (x) { return !x.hidden || x === it; }), i = all.indexOf(it), j = dir === 'up' ? i - 1 : i + 1;
      if (j < 0 || j >= all.length) return;
      if (dir === 'up') list.insertBefore(it, all[j]); else list.insertBefore(it, all[j].nextSibling);
      refresh(); persist();
      var f = focusSel ? it.querySelector(focusSel) : null;
      if (!f || f.disabled) f = it.querySelector('.lib-arrow:not([disabled])');
      if (f) f.focus();
    }
    list.addEventListener('click', function (e) {
      var b = e.target.closest('.lib-arrow'); if (!b || !list.contains(b)) return;
      var it = b.closest(itemSel); if (!it || it.parentElement !== list) return;
      e.preventDefault(); move(it, b.value, '.lib-arrow[value=' + b.value + ']');
    });
    list.addEventListener('keydown', function (e) {
      var it = e.target.closest(itemSel); if (!it || it.parentElement !== list) return;
      var onHandle = e.target.classList && e.target.classList.contains('lib-handle');
      if (!(e.altKey || onHandle)) return;
      if (e.target.matches('input, textarea, select')) return;
      var dir = (e.key === 'ArrowUp' || e.key === 'ArrowLeft') ? 'up' : (e.key === 'ArrowDown' || e.key === 'ArrowRight') ? 'down' : '';
      if (!dir) return;
      e.preventDefault(); move(it, dir, onHandle ? '.lib-handle' : null);
    });
    // Pointer drag (mouse, pen and touch) from the ⠿ handle.
    var drag = null;
    list.addEventListener('pointerdown', function (e) {
      var h = e.target.closest('.lib-handle'); if (!h || !list.contains(h) || e.button > 0) return;
      var it = h.closest(itemSel); if (!it || it.parentElement !== list) return;
      e.preventDefault();
      drag = { it: it, id: e.pointerId, before: items().map(function (x) { return x.dataset.id; }).join(',') };
      it.classList.add('lib-dragging'); list.classList.add('lib-sorting');
      try { h.setPointerCapture(e.pointerId); } catch (err) { /* ignore */ }
    });
    list.addEventListener('pointermove', function (e) {
      if (!drag || e.pointerId !== drag.id) return;
      e.preventDefault();
      var over = null;
      items().forEach(function (x) {
        if (x === drag.it || x.hidden) return;
        var r = x.getBoundingClientRect();
        if (e.clientX >= r.left && e.clientX <= r.right && e.clientY >= r.top && e.clientY <= r.bottom) over = x;
      });
      if (!over) { autoscroll(e.clientY); return; }
      // A vertical list compares top/bottom halves; the card grid reads left to right, so left/right halves.
      var r = over.getBoundingClientRect();
      var after = list.tagName === 'OL' ? e.clientY > r.top + r.height / 2 : e.clientX > r.left + r.width / 2;
      if (after) list.insertBefore(drag.it, over.nextSibling); else list.insertBefore(drag.it, over);
      autoscroll(e.clientY);
    });
    function autoscroll(y) {
      if (y < 60) window.scrollBy(0, -12); else if (y > window.innerHeight - 60) window.scrollBy(0, 12);
    }
    function end(e) {
      if (!drag || (e && e.pointerId !== drag.id)) return;
      drag.it.classList.remove('lib-dragging'); list.classList.remove('lib-sorting');
      var changed = items().map(function (x) { return x.dataset.id; }).join(',') !== drag.before;
      var h = drag.it.querySelector('.lib-handle');
      drag = null; refresh();
      if (changed) persist();
      if (h) h.focus();
    }
    list.addEventListener('pointerup', end);
    list.addEventListener('pointercancel', end);
    refreshers.push(refresh);
    refresh();
  }

  if (isManager) {
    sortable(grid, '.form-card', grid.dataset.orderUrl, { el: savedEl });
    document.querySelectorAll('.lib-border').forEach(function (ol) {
      var row = ol.closest('.doc-row'), saved = ol.parentElement.querySelector('.lib-saved');
      sortable(ol, '.lib-bitem', ol.dataset.orderUrl, {
        el: saved, fn: function (ids) {
          // keep the bundle's Edit form in the same order, so saving it doesn't undo the drag
          var checks = row && row.querySelector('.lib-bchecks'); if (!checks) return;
          ids.slice().reverse().forEach(function (id) {
            var l = checks.querySelector('label[data-id="' + id + '"]'); if (l) checks.insertBefore(l, checks.firstChild);
          });
        }
      });
    });
  }

  // ------------------------------------------------------------------ Quick look
  var drawer = document.getElementById('lib-drawer'), backdrop = document.getElementById('lib-backdrop'), opener = null;
  function el(tag, cls, text) { var e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }
  function btn(href, label, cls, blank) { var a = el('a', 'btn ' + (cls || ''), label); a.href = href; if (blank) { a.target = '_blank'; a.rel = 'noopener'; } return a; }
  function close() {
    if (!drawer || drawer.hidden) return;
    drawer.hidden = true; backdrop.hidden = true; drawer.innerHTML = '';
    document.body.classList.remove('lib-noscroll');
    if (opener) opener.focus();
  }
  function head(title, sub) {
    var h = el('div', 'db-drawer-head');
    var t = el('div'); t.appendChild(el('span', 'eyebrow', sub)); var h2 = el('h2', null, title); h2.id = 'lib-drawer-title'; t.appendChild(h2);
    var x = el('button', 'btn btn-quiet lib-x', '×'); x.type = 'button'; x.setAttribute('aria-label', 'Close preview'); x.addEventListener('click', close);
    h.appendChild(t); h.appendChild(x); return h;
  }
  function render(d) {
    drawer.innerHTML = '';
    drawer.appendChild(head(d.name, d.kind_label + (d.page_count ? ' · ' + d.page_count + ' page' + (d.page_count === 1 ? '' : 's') : '')));
    if (d.tags && d.tags.length) { var tg = el('div', 'lib-dtags'); d.tags.forEach(function (t) { tg.appendChild(el('span', 'chip', t)); }); drawer.appendChild(tg); }
    var acts = el('div', 'db-drawer-acts');
    acts.appendChild(btn(d.send_url, 'Send', 'btn-primary'));
    if (d.file_url) acts.appendChild(btn(d.file_url, d.kind === 'pdf' ? 'Open PDF' : 'Download', 'btn-quiet', true));
    if (d.edit_url) acts.appendChild(btn(d.edit_url, 'Edit', 'btn-quiet'));
    drawer.appendChild(acts);
    if (d.description && d.mode !== 'html') { var p = el('div', 'lib-desc small'); p.textContent = d.description; drawer.appendChild(p); }
    var body = el('div', 'lib-pbody');
    if (d.mode === 'pages') {
      d.pages.forEach(function (src, i) { var im = el('img', 'lib-page'); im.loading = 'lazy'; im.alt = 'Page ' + (i + 1) + ' of ' + d.name; im.src = src; body.appendChild(im); });
      if (d.page_count > d.pages.length) body.appendChild(el('p', 'small muted', 'Showing the first ' + d.pages.length + ' of ' + d.page_count + ' pages. Open the file to see the rest.'));
    } else if (d.mode === 'image') {
      var im = el('img', 'lib-page'); im.alt = d.name; im.src = d.image; body.appendChild(im);
    } else if (d.mode === 'html') {
      var f = el('iframe', 'lib-frame'); f.setAttribute('sandbox', ''); f.title = 'Preview of ' + d.name; f.srcdoc = d.html; body.appendChild(f);
    } else if (d.mode === 'download') {
      body.appendChild(el('p', 'small muted', 'This file type can\'t be shown here. Download it to look at it.'));
    }
    if (d.sections && d.sections.length) {
      d.sections.forEach(function (s) {
        body.appendChild(el('h3', null, s.name));
        var ul = el('ul', 'lib-fields small');
        (s.items || []).forEach(function (it) { ul.appendChild(el('li', null, typeof it === 'string' ? it : (it.label || it.name || ''))); });
        body.appendChild(ul);
      });
    }
    if (d.fields && d.fields.length) {
      body.appendChild(el('h3', null, (d.mode === 'form' ? 'Fields the client fills in' : 'Extra fields') + ' (' + d.fields.length + ')'));
      var ul = el('ol', 'lib-fields small');
      d.fields.forEach(function (fd) {
        var li = el('li'); li.appendChild(el('strong', null, fd.label || '(no label)'));
        li.appendChild(el('span', 'muted', ' · ' + fd.type + (fd.required ? ' · required' : '')));
        if (fd.help) li.appendChild(el('div', 'muted', fd.help));
        ul.appendChild(li);
      });
      body.appendChild(ul);
    } else if (d.mode === 'form' && !(d.sections && d.sections.length)) {
      body.appendChild(el('p', 'small muted', 'No fields to fill in: the client reads it and signs.'));
    }
    drawer.appendChild(body);
  }
  function open(b) {
    opener = b;
    drawer.innerHTML = ''; drawer.appendChild(head(b.closest('.form-card').dataset.name, 'Quick look'));
    drawer.appendChild(el('p', 'muted small', 'Loading…'));
    drawer.hidden = false; backdrop.hidden = false; document.body.classList.add('lib-noscroll');
    drawer.focus();
    fetch(b.dataset.url, { credentials: 'same-origin', headers: { 'Accept': 'application/json' } })
      .then(function (r) { if (!r.ok) throw new Error(); return r.json(); })
      .then(function (d) { if (!drawer.hidden && opener === b) { render(d); drawer.focus(); } })
      .catch(function () { drawer.appendChild(el('p', 'small', 'The preview could not be loaded. Try again, or use Preview / Download on the card.')); });
  }
  if (drawer) {
    grid.addEventListener('click', function (e) { var b = e.target.closest('.lib-peek'); if (b) { e.preventDefault(); open(b); } });
    backdrop.addEventListener('click', close);
    document.addEventListener('keydown', function (e) {
      if (drawer.hidden) return;
      if (e.key === 'Escape') { e.preventDefault(); close(); return; }
      if (e.key === 'Tab') {  // keep focus inside the panel
        var f = drawer.querySelectorAll('a[href], button:not([disabled]), iframe, [tabindex="0"]');
        if (!f.length) return;
        var first = f[0], last = f[f.length - 1];
        if (e.shiftKey && (document.activeElement === first || document.activeElement === drawer)) { e.preventDefault(); last.focus(); }
        else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
      }
    });
  }

  apply(false);
})();
