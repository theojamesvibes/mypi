/* Dashboard page boot + control wiring. Loader functions live in
 * /static/js/dashboard.js; inline handlers are not allowed under the
 * strict script-src CSP, so the controls are wired here. */

'use strict';

document.getElementById('time-range').addEventListener('change', () => loadDashboard());
document.getElementById('dash-refresh-btn').addEventListener('click', () => loadDashboard());

loadDashboard();
loadSyncIndicator();
setInterval(loadDashboard, 60000);
setInterval(loadSyncIndicator, 60000);
