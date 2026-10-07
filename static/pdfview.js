// Renders every page of the PDF with pdf.js so it displays on phones too (iframes don't).
(function () {
  const box = document.getElementById('pdf-pages');
  if (!box) return;
  const src = box.dataset.src;
  function fallback() {
    box.innerHTML = '<iframe class="pdf-frame" src="' + src + '#view=FitH" title="Document"></iframe>';
  }
  if (!window.pdfjsLib) return fallback();
  pdfjsLib.GlobalWorkerOptions.workerSrc = 'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js';
  pdfjsLib.getDocument(src).promise.then(async pdf => {
    box.innerHTML = '';
    const width = box.clientWidth, dpr = window.devicePixelRatio || 1;
    for (let n = 1; n <= pdf.numPages; n++) {
      const page = await pdf.getPage(n);
      const base = page.getViewport({ scale: 1 });
      const vp = page.getViewport({ scale: (width / base.width) * dpr });
      const c = document.createElement('canvas');
      c.width = vp.width; c.height = vp.height; c.className = 'pdf-page';
      c.setAttribute('aria-label', 'Page ' + n + ' of ' + pdf.numPages);
      box.appendChild(c);
      await page.render({ canvasContext: c.getContext('2d'), viewport: vp }).promise;
    }
    const note = document.createElement('p');
    note.className = 'muted small'; note.textContent = 'End of document · ' + pdf.numPages + ' page' + (pdf.numPages > 1 ? 's' : '');
    box.appendChild(note);
  }).catch(fallback);
})();
