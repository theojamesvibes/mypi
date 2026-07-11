/* Query Log page boot + filter wiring. Loader functions live in
 * /static/js/dashboard.js; inline handlers are not allowed under the
 * strict script-src CSP, so the controls are wired here. */

'use strict';

// Pre-set filters from URL params (e.g. when navigating from dashboard cards)
(function () {
  const p = new URLSearchParams(window.location.search);
  if (p.has('domain')) { const el = document.getElementById('f-domain'); if (el) el.value = p.get('domain'); }
  if (p.has('client')) { const el = document.getElementById('f-client'); if (el) el.value = p.get('client'); }
  if (p.get('blocked') === 'true')  { const el = document.getElementById('f-blocked'); if (el) el.value = 'true'; }
  if (p.get('blocked') === 'false') { const el = document.getElementById('f-blocked'); if (el) el.value = 'false'; }
  if (p.get('show') === 'clients')  { const el = document.getElementById('f-blocked'); if (el) el.value = 'clients'; }
})();

document.getElementById('live-toggle').addEventListener('change', function () { toggleLiveView(this.checked); });
document.getElementById('refresh-btn').addEventListener('click', () => loadQueries(1));
document.getElementById('f-instance').addEventListener('change', () => loadQueries(1));
document.getElementById('f-blocked').addEventListener('change', () => loadQueries(1));
document.getElementById('f-hours').addEventListener('change', () => loadQueries(1));
['f-domain', 'f-client'].forEach(id => {
  document.getElementById(id).addEventListener('keydown', e => {
    if (e.key === 'Enter') loadQueries(1);
  });
});

loadInstanceFilter();
loadQueries(1);
