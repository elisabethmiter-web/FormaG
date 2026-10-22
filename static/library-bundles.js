/* New packet: tick a bundle's forms in the bundle's own order (set by dragging in the Form library), so the
   packet's "#1, #2…" order and the forms the client sees follow it. Added to the page by library_plus.py. */
(function () {
  var picker = document.getElementById('picker');
  if (!picker) return;
  function box(id) { return picker.querySelector('input[name=template_ids][value="' + id + '"]'); }
  function set(c, on) { if (c && c.checked !== on) { c.checked = on; c.dispatchEvent(new Event('change', { bubbles: true })); } }
  function apply(ids) {
    var boxes = ids.map(box).filter(Boolean);
    boxes.forEach(function (c) { set(c, false); });   // take them out of the picked order…
    boxes.forEach(function (c) { set(c, true); });    // …and put them back in the bundle's order
  }
  // Clicking a bundle button: the page ticks the forms first (in list order); this runs after and re-orders them.
  document.querySelectorAll('.bundle-btn').forEach(function (b) {
    b.addEventListener('click', function () { apply((b.dataset.ids || '').split(',').filter(Boolean)); });
  });
  // Opened with ?bundle=N: tick in the bundle's order.
  var me = document.getElementById('lp-bundle-order'), pre = [];
  try { pre = JSON.parse(me && me.getAttribute('data-bundle-order') || '[]'); } catch (e) { pre = []; }
  if (pre.length) apply(pre.map(String));
})();
