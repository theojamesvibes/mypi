/* Swagger UI boot for /docs. Theme pre-paint is handled by boot.js in the
 * <head>; this script initialises Swagger and keeps the theme in sync. */

'use strict';

window.ui = SwaggerUIBundle({
  url: '/openapi.json',
  dom_id: '#swagger-ui',
  deepLinking: true,
  presets: [SwaggerUIBundle.presets.apis, SwaggerUIBundle.SwaggerUIStandalonePreset],
  layout: 'BaseLayout',
});

// Keep theme in sync with settings changes made in other tabs
window.addEventListener('storage', function (e) {
  if (e.key !== 'mypi-theme') return;
  var pref = e.newValue || 'system';
  var resolved = pref === 'system'
    ? (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light')
    : pref;
  document.documentElement.setAttribute('data-bs-theme', resolved);
});

// Follow system theme changes while pref is 'system'
window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', function (e) {
  if ((localStorage.getItem('mypi-theme') || 'system') === 'system') {
    document.documentElement.setAttribute('data-bs-theme', e.matches ? 'dark' : 'light');
  }
});
