// Forms with data-confirm ask before submitting.
document.querySelectorAll('form[data-confirm]').forEach(f => {
  f.addEventListener('submit', e => { if (!window.confirm(f.dataset.confirm)) e.preventDefault(); });
});
