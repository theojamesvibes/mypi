/* Combined page boot + control wiring. Loader functions live in
 * /static/js/dashboard.js; inline handlers are not allowed under the
 * strict script-src CSP, so the controls are wired here. */

'use strict';

document.getElementById('time-range').addEventListener('change', () => loadCombined());
document.getElementById('combined-refresh-btn').addEventListener('click', () => loadCombined());

loadCombined();
startCombinedTicker();
setInterval(loadCombined, 60000);
