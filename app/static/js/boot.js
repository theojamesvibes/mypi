/* ─── MyPi boot ───────────────────────────────────────────────────────────────
 *
 * Loaded synchronously in <head> (parser-blocking on purpose) so both jobs
 * run before paint and before dashboard.js / any page script:
 *
 *   1. Theme pre-paint — apply the stored theme before first paint to avoid
 *      a flash of the wrong theme.
 *   2. Multi-site helpers — currentSiteSlug / siteApiUrl / currentSection
 *      must exist before dashboard.js and any page script call them
 *      synchronously on page-load. Historically these lived in a trailing
 *      <script> at the bottom of base.html; that ran AFTER settings.html's
 *      page script and produced a `siteApiUrl is not a function` TypeError
 *      that made every loader silently no-op.
 *
 * Server data arrives via the #mypi-ctx JSON island (a
 * <script type="application/json"> block, which the strict script-src CSP
 * never executes). Pages without the island (docs.html) get single-site
 * defaults.
 */

'use strict';

(function () {
  var pref = localStorage.getItem('mypi-theme') || 'system';
  var resolved = pref === 'system'
    ? (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light')
    : pref;
  document.documentElement.setAttribute('data-bs-theme', resolved);
})();

(function () {
  var ctx = {};
  var el = document.getElementById('mypi-ctx');
  if (el) {
    try { ctx = JSON.parse(el.textContent) || {}; } catch (e) { /* malformed island — single-site defaults */ }
  }

  window.currentSiteSlug = ctx.site_slug || '';
  window.siteApiUrl = function (pathAfterApi) {
    if (pathAfterApi.indexOf('/') !== 0) pathAfterApi = '/' + pathAfterApi;
    if (window.currentSiteSlug) {
      return '/api/sites/' + encodeURIComponent(window.currentSiteSlug) + pathAfterApi;
    }
    return '/api' + pathAfterApi;
  };
  window.currentSection = (function () {
    var path = window.location.pathname;
    if (path.indexOf('/queries')  === 0) return 'queries';
    if (path.indexOf('/settings') === 0) return 'settings';
    if (path.indexOf('/combined') === 0) return 'combined';
    return 'dashboard';
  })();
})();
