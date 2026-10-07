// Field list editor shared by the PDF upload form and the fillable-form builder.
window.FormBuilder = {
  types: [["text","Short answer"],["textarea","Long answer"],["email","Email"],["phone","Phone"],
          ["date","Date"],["number","Number"],["checkbox","Checkbox (yes/no)"],["initials","Initials"]],
  mount(list, addBtn, hidden, initial) {
    const self = this;
    function sync() {
      const items = [...list.querySelectorAll('.builder-item')].map(r => ({
        label: r.querySelector('.f-label').value.trim(),
        type: r.querySelector('.f-type').value,
        required: r.querySelector('.f-req').checked,
      })).filter(f => f.label);
      hidden.value = JSON.stringify(items);
    }
    function add(f) {
      f = f || {label: '', type: 'text', required: true};
      const row = document.createElement('div');
      row.className = 'builder-item';
      const opts = self.types.map(([v, l]) => `<option value="${v}">${l}</option>`).join('');
      row.innerHTML = `<input type="text" class="f-label" placeholder="Question or label" aria-label="Field label">
        <select class="f-type" aria-label="Field type">${opts}</select>
        <label class="req"><input type="checkbox" class="f-req"> Required</label>
        <button type="button" class="btn btn-quiet f-del" aria-label="Remove field">Remove</button>`;
      row.querySelector('.f-label').value = f.label;
      row.querySelector('.f-type').value = f.type;
      row.querySelector('.f-req').checked = !!f.required;
      row.querySelector('.f-del').onclick = () => { row.remove(); sync(); };
      row.addEventListener('input', sync);
      row.addEventListener('change', sync);
      list.appendChild(row);
      sync();
      return row;
    }
    (initial || []).forEach(add);
    addBtn.addEventListener('click', () => add().querySelector('.f-label').focus());
    hidden.form.addEventListener('submit', sync);
  }
};
