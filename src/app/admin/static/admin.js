'use strict';
document.addEventListener('htmx:responseError', function (event) {
  const box = document.getElementById('request-error');
  if (box) { box.hidden = false; box.textContent = 'Nie udało się odświeżyć listy. Sprawdź filtry lub odśwież stronę.'; }
});
document.addEventListener('htmx:afterSwap', function () {
  const box = document.getElementById('request-error');
  if (box) box.hidden = true;
  const status = document.getElementById('results-status');
  if (status) status.focus({preventScroll:true});
});
