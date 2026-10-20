'use strict';
document.addEventListener('htmx:responseError', function (event) {
  const box = document.getElementById('request-error');
  if (box) {
    box.hidden = false;
    let message = 'Nie udało się wykonać operacji. Odśwież stronę i spróbuj ponownie.';
    try { const data = JSON.parse(event.detail.xhr.responseText); if (typeof data.detail === 'string') message = data.detail; } catch (_) { /* Use the generic message. */ }
    box.textContent = message;
  }
});
document.addEventListener('submit', function (event) {
  const form = event.target;
  if (form instanceof HTMLFormElement && form.dataset.confirm && !window.confirm(form.dataset.confirm)) {
    event.preventDefault();
  }
});
document.addEventListener('htmx:afterSwap', function (event) {
  const box = document.getElementById('request-error');
  if (box && event.detail.target.id === 'results') box.hidden = true;
  const status = document.getElementById('results-status');
  if (status) status.focus({preventScroll:true});
  syncCycleControls();
});
function syncCycleControls() {
  const progress = document.getElementById('cycle-progress');
  const save = document.getElementById('save-collection');
  if (progress && save) save.disabled = progress.dataset.running === 'true';
}
document.addEventListener('DOMContentLoaded', syncCycleControls);
