/* Guided tours ("Show me how"): a spotlight on the part of the page to look at and a bubble with Back / Next /
   Skip. A tour can run across pages: its steps and progress live in sessionStorage, so it carries on after
   Next (or clicking the highlighted link) opens the next page. Esc ends it. If a step's target isn't on the
   page (another layout, no rows yet, staff view…), the bubble shows in the middle of the screen instead.

   Tours are defined as JSON by the page that starts them (help.html: <script type="application/json"
   id="fs-tours">) and started by any element with data-tour-start="<id>". Step format:
     { "path": "/packets/new",            page the step is on (location.pathname); omitted = any page
       "sel": ["#client_name", "..."],     first visible match is highlighted; none = centred bubble
       "open": "details#upload-panel",     optional <details> to open first
       "title": "...", "text": "..." } */
(function () {
  'use strict';
  var KEY = 'fs-tour';
  var state = null, ui = null, target = null, raf = 0, waitTimer = 0, lastFocus = null;

  function load() { try { return JSON.parse(sessionStorage.getItem(KEY) || 'null'); } catch (e) { return null; } }
  function store(s) { try { if (s) sessionStorage.setItem(KEY, JSON.stringify(s)); else sessionStorage.removeItem(KEY); } catch (e) { /* private mode */ } }
  function here(step) { return !step.path || step.path === location.pathname; }
  function q(sel) { try { return document.querySelector(sel); } catch (e) { return null; } } // :has() etc. on old browsers
  function visible(el) {
    if (!el || !el.getClientRects().length) return false;
    var r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && getComputedStyle(el).visibility !== 'hidden';
  }
  function find(step) {
    var sels = step.sel ? [].concat(step.sel) : [];
    for (var i = 0; i < sels.length; i++) {
      var list; try { list = document.querySelectorAll(sels[i]); } catch (e) { list = []; }
      for (var k = 0; k < list.length; k++) if (visible(list[k])) return list[k];
    }
    return null;
  }

  function build() {
    if (ui) return ui;
    var spot = document.createElement('div'); spot.className = 'fs-tour-spot'; spot.setAttribute('aria-hidden', 'true');
    var shade = document.createElement('div'); shade.className = 'fs-tour-shade'; shade.setAttribute('aria-hidden', 'true');
    var b = document.createElement('div');
    b.className = 'fs-tour-bubble'; b.setAttribute('role', 'dialog'); b.setAttribute('aria-modal', 'false');
    b.setAttribute('aria-labelledby', 'fs-tour-title'); b.setAttribute('aria-describedby', 'fs-tour-text'); b.tabIndex = -1;
    b.innerHTML = '<div class="fs-tour-top"><span class="fs-tour-count"></span><button type="button" class="fs-tour-x" aria-label="Close the tour">×</button></div>' +
      '<h2 class="fs-tour-title" id="fs-tour-title"></h2><p class="fs-tour-text" id="fs-tour-text"></p>' +
      '<div class="fs-tour-dots" aria-hidden="true"></div>' +
      '<div class="fs-tour-acts"><button type="button" class="fs-tour-skip">Skip tour</button><span class="fs-tour-sp"></span>' +
      '<button type="button" class="btn fs-tour-back">Back</button><button type="button" class="btn btn-primary fs-tour-next">Next</button></div>';
    document.body.appendChild(shade); document.body.appendChild(spot); document.body.appendChild(b);
    b.querySelector('.fs-tour-x').addEventListener('click', function () { end(); });
    b.querySelector('.fs-tour-skip').addEventListener('click', function () { end(); });
    b.querySelector('.fs-tour-back').addEventListener('click', function () { go(state.i - 1); });
    b.querySelector('.fs-tour-next').addEventListener('click', function () {
      go(here(state.steps[state.i]) ? state.i + 1 : state.i);   // "Go there" re-opens the step's own page
    });
    ui = { spot: spot, shade: shade, bubble: b };
    return ui;
  }

  function end() {
    store(null); state = null; target = null;
    cancelAnimationFrame(raf); clearTimeout(waitTimer);
    if (ui) { ui.spot.remove(); ui.shade.remove(); ui.bubble.remove(); ui = null; }
    document.removeEventListener('click', onTargetClick, true);
    if (lastFocus && document.contains(lastFocus)) { try { lastFocus.focus(); } catch (e) { /* ignore */ } }
  }

  function go(i) {
    if (!state) return;
    if (i >= state.steps.length) { end(); return; }
    if (i < 0) i = 0;
    state.i = i; store(state);
    var step = state.steps[i];
    if (!here(step)) { location.href = step.url || step.path; return; }  // carries on after the page loads
    show();
  }

  // Clicking the highlighted link (e.g. "New packet") moves the tour on, as if Next had been pressed.
  function onTargetClick(e) {
    if (!state || !target || !target.contains(e.target)) return;
    var next = state.steps[state.i + 1];
    var a = e.target.closest('a[href]');
    if (next && a && !here(next)) { state.i += 1; store(state); }
  }

  function show() {
    var step = state.steps[state.i];
    var u = build(), b = u.bubble;
    clearTimeout(waitTimer);
    if (!here(step)) {  // tour was left on another page: offer to go back to it
      target = null;
      b.querySelector('.fs-tour-count').textContent = state.title;
      b.querySelector('.fs-tour-title').textContent = 'Carry on with the tour?';
      b.querySelector('.fs-tour-text').textContent = 'The next step, “' + step.title + '”, is on another page.';
      b.querySelector('.fs-tour-next').textContent = 'Go there';
      b.querySelector('.fs-tour-back').hidden = true;
      paintDots(); place(); b.focus();
      return;
    }
    if (step.open) { var d = q(step.open); if (d && d.tagName === 'DETAILS') d.open = true; }
    var tries = 0;
    (function look() {
      target = find(step);
      if (!target && step.sel && tries++ < 8) { waitTimer = setTimeout(look, 150); return; } // late content
      fill(step);
    })();
  }

  function fill(step) {
    var b = ui.bubble, n = state.steps.length;
    b.querySelector('.fs-tour-count').textContent = state.title + ' · ' + (state.i + 1) + ' of ' + n;
    b.querySelector('.fs-tour-title').textContent = step.title || '';
    b.querySelector('.fs-tour-text').textContent = step.text || '';
    var back = b.querySelector('.fs-tour-back'), next = b.querySelector('.fs-tour-next');
    back.hidden = state.i === 0;
    next.textContent = state.i === n - 1 ? 'Done' : 'Next';
    b.classList.toggle('fs-tour-centre', !target);
    paintDots();
    if (target) {
      var r = target.getBoundingClientRect();
      if (r.top < 70 || r.bottom > view().h - (narrow() ? 230 : 40)) {
        target.scrollIntoView({ block: 'center', behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth' });
      }
    }
    place();
    setTimeout(place, 350);   // after smooth scrolling
    next.focus({ preventScroll: true });
  }

  function paintDots() {
    var d = ui.bubble.querySelector('.fs-tour-dots'); d.innerHTML = '';
    state.steps.forEach(function (_s, k) { var s = document.createElement('span'); if (k === state.i) s.className = 'on'; else if (k < state.i) s.className = 'done'; d.appendChild(s); });
  }

  // The visual viewport: on phones a page wider than the screen widens the layout viewport, so "100vw" and
  // right: 12px would push the bubble off screen. Position from what the person actually sees.
  function view() {
    var v = window.visualViewport;
    return v ? { w: v.width, h: v.height, x: v.offsetLeft, y: v.offsetTop }
      : { w: document.documentElement.clientWidth, h: window.innerHeight, x: 0, y: 0 };
  }
  function narrow() { return view().w <= 600; }

  function place() {
    if (!ui) return;
    var b = ui.bubble, spot = ui.spot, pad = 6, gap = 12, m = 12, v = view();
    b.style.right = b.style.bottom = '';
    b.style.width = Math.min(narrow() ? v.w - 2 * m : 360, v.w - 2 * m) + 'px';
    var bw = b.offsetWidth, bh = b.offsetHeight;
    if (!target || !visible(target)) {
      spot.hidden = true; ui.shade.hidden = false;
      b.classList.add('fs-tour-centre');
      b.style.left = (v.x + (v.w - bw) / 2) + 'px'; b.style.top = (v.y + Math.max(m, (v.h - bh) / 2)) + 'px';
      return;
    }
    ui.shade.hidden = true;
    b.classList.remove('fs-tour-centre');
    var r = target.getBoundingClientRect();
    spot.hidden = false;
    spot.style.left = (r.left - pad) + 'px'; spot.style.top = (r.top - pad) + 'px';
    spot.style.width = (r.width + pad * 2) + 'px'; spot.style.height = (r.height + pad * 2) + 'px';
    var vr = r.top - v.y;   // target top within what's visible
    if (narrow()) {  // phone: a full-width sheet at the bottom (or the top, if the target is low on the screen)
      b.style.left = (v.x + m) + 'px';
      b.style.top = (vr + r.height / 2 > v.h * 0.55 ? v.y + m : v.y + v.h - bh - m) + 'px';
      return;
    }
    var top;
    if (r.bottom + pad + gap + bh <= v.y + v.h - m) top = r.bottom + pad + gap;
    else if (r.top - pad - gap - bh >= v.y + m) top = r.top - pad - gap - bh;
    else top = Math.max(v.y + m, Math.min(v.y + v.h - bh - m, r.top + r.height / 2 - bh / 2));
    var left = Math.max(v.x + m, Math.min(v.x + v.w - bw - m, r.left + r.width / 2 - bw / 2));
    // beside the target if above/below would cover it
    if (top < r.bottom && top + bh > r.top) {
      if (r.right + pad + gap + bw <= v.x + v.w - m) left = r.right + pad + gap;
      else if (r.left - pad - gap - bw >= v.x + m) left = r.left - pad - gap - bw;
    }
    b.style.left = left + 'px'; b.style.top = top + 'px';
  }

  function tick() { cancelAnimationFrame(raf); raf = requestAnimationFrame(place); }
  window.addEventListener('resize', tick);
  window.addEventListener('scroll', tick, true);
  if (window.visualViewport) { visualViewport.addEventListener('resize', tick); visualViewport.addEventListener('scroll', tick); }
  document.addEventListener('keydown', function (e) {
    if (!state) return;
    if (e.key === 'Escape') { e.preventDefault(); end(); return; }
    if (!ui || !ui.bubble.contains(document.activeElement)) return;
    if (e.key === 'ArrowRight') { e.preventDefault(); ui.bubble.querySelector('.fs-tour-next').click(); }
    else if (e.key === 'ArrowLeft' && state.i > 0) { e.preventDefault(); go(state.i - 1); }
  });

  function start(def) {
    if (!def || !def.steps || !def.steps.length) return;
    lastFocus = document.activeElement;
    state = { id: def.id, title: def.title || 'Tour', steps: def.steps, i: -1 };
    document.addEventListener('click', onTargetClick, true);
    go(0);
  }

  function defs() {
    var el = document.getElementById('fs-tours');
    if (!el) return {};
    try { var list = JSON.parse(el.textContent), out = {}; list.forEach(function (t) { out[t.id] = t; }); return out; } catch (e) { return {}; }
  }

  function init() {
    var all = defs();
    document.querySelectorAll('[data-tour-start]').forEach(function (btn) {
      var def = all[btn.getAttribute('data-tour-start')];
      if (!def) return;
      btn.hidden = false;
      btn.addEventListener('click', function (e) { e.preventDefault(); start(def); });
    });
    document.querySelectorAll('.fs-tour-only').forEach(function (el) { el.hidden = false; });
    var saved = load();
    if (saved && saved.steps && saved.steps[saved.i]) {
      state = saved;
      document.addEventListener('click', onTargetClick, true);
      show();
    }
  }

  window.FSTour = { start: start, end: end };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})();
