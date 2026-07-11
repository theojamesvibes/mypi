/* ─── MyPi base-layout wiring ─────────────────────────────────────────────────
 *
 * Runs on every page that extends base.html, after dashboard.js and the
 * page's own script. Drives the chrome shared by all pages: sidebar toggle,
 * site picker, Combined nav visibility, sync badge, and version badge.
 * window.currentSiteSlug / siteApiUrl / currentSection are defined by
 * boot.js in the <head>.
 */

'use strict';

document.getElementById('sidebar-toggle').addEventListener('click', () => {
  document.getElementById('sidebar').classList.toggle('collapsed');
  document.getElementById('main-content').classList.toggle('expanded');
});

// Populate the site picker on every page. Hidden when ≤1 active site so
// single-site deployments see no UI change at all.
//
// Also drives:
//   • the Combined nav item (shown only when ≥2 active sites, hidden on
//     single-site deployments)
//   • page-title updates — document.title and any .page-title-site span
//     get the current site's name appended on per-slug pages so the tab
//     and heading read e.g. "Dashboard: WTR" when multi-site is configured
//   • the clickable sidebar logo — refreshes the dashboard when already
//     on it, otherwise navigates to the dashboard for the current site
(async function () {
  const picker  = document.getElementById('site-picker');
  const combinedNavItem = document.getElementById('nav-combined-item');
  const section = window.currentSection;

  let sites = [];
  try {
    const res = await fetch('/api/sites', { credentials: 'include', cache: 'no-store' });
    if (res.ok) sites = await res.json();
  } catch (_) {}
  sites = sites || [];

  const multi = sites.length > 1;

  // Combined nav visibility — only when ≥2 sites configured. Hidden on
  // the Combined page itself isn't necessary (no redirect loop); we keep
  // it visible there so users can tell which page they're on via 'active'.
  if (combinedNavItem) {
    combinedNavItem.style.display = multi ? '' : 'none';
  }

  // Site picker — irrelevant on /combined (view is cross-site by nature).
  if (picker) {
    if (!multi || section === 'combined') {
      picker.style.display = 'none';
    } else {
      picker.innerHTML = '';
      const currentSlug = window.currentSiteSlug;
      let haveActiveSelection = false;
      for (const s of sites) {
        const opt = document.createElement('option');
        opt.value = s.slug;
        opt.textContent = s.name + (s.is_main ? ' ★' : '');
        if (s.slug === currentSlug) { opt.selected = true; haveActiveSelection = true; }
        picker.appendChild(opt);
      }
      if (!haveActiveSelection) {
        const main = sites.find(s => s.is_main) || sites[0];
        if (main) {
          for (const opt of picker.options) {
            if (opt.value === main.slug) { opt.selected = true; break; }
          }
        }
      }
      picker.style.display = '';
      picker.addEventListener('change', () => {
        const slug = picker.value;
        window.location.href = '/' + window.currentSection + '/' + encodeURIComponent(slug);
      });
    }
  }

  // Rewrite sidebar links so navigating between sections preserves the
  // currently-selected site. Falls back to the Main site when the user
  // landed on a legacy no-slug URL.
  if (multi) {
    const navSlug = window.currentSiteSlug || (sites.find(s => s.is_main) || sites[0]).slug;
    const dash = document.getElementById('nav-dashboard');
    const que  = document.getElementById('nav-queries');
    const set  = document.getElementById('nav-settings');
    if (dash) dash.href = '/dashboard/' + encodeURIComponent(navSlug);
    if (que)  que.href  = '/queries/'  + encodeURIComponent(navSlug);
    if (set)  set.href  = '/settings/' + encodeURIComponent(navSlug);
  }

  // Title update: on per-slug pages, append the site name so tabs read
  // e.g. "Dashboard: WTR — Pi-hole Dashboard" and the in-page heading
  // matches. On /combined or single-site, leave the default as-is.
  if (section !== 'combined' && window.currentSiteSlug) {
    const current = sites.find(s => s.slug === window.currentSiteSlug);
    if (current && current.name) {
      // tab title
      document.title = document.title.replace(/^([^—]+?)(\s+—\s+Pi-hole Dashboard)?$/,
        function (_m, base, tail) {
          return base.trim() + ': ' + current.name + (tail || '');
        });
      // in-page heading span (added in dashboard/queries/settings.html)
      document.querySelectorAll('.page-title-site').forEach(el => {
        el.textContent = ': ' + current.name;
      });
    }
  }

  // Sidebar logo click — refresh on the dashboard (calls loadDashboard if
  // defined, since that's what the refresh button does), otherwise
  // navigate to the dashboard for the currently-selected site.
  const logoLink = document.getElementById('sidebar-logo-link');
  if (logoLink) {
    // Point the href at the correct target so middle-click / ctrl-click
    // still open the dashboard in a new tab.
    const logoSlug = multi
      ? (window.currentSiteSlug || (sites.find(s => s.is_main) || sites[0]).slug)
      : '';
    logoLink.href = logoSlug ? '/dashboard/' + encodeURIComponent(logoSlug) : '/';
    logoLink.addEventListener('click', (ev) => {
      if (ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.button === 1) return; // allow new-tab
      if (section === 'dashboard' && typeof window.loadDashboard === 'function') {
        ev.preventDefault();
        window.loadDashboard();
      }
    });
  }
})();

// Sync badge — runs on every page. Uses per-site status when on a site URL.
(async function () {
  try {
    const res = await fetch(window.siteApiUrl('/sync/status'), { credentials: 'include', cache: 'no-store' });
    if (res.ok) renderSyncBadge(await res.json());
  } catch (_) {}
})();

// Version badge colour — green = up to date, red = update available
(async function () {
  try {
    const res = await fetch('/api/version/status', { credentials: 'include', cache: 'no-store' });
    if (!res.ok) return;
    const data = await res.json();
    const badge = document.getElementById('version-badge');
    if (!badge || !data.enabled || data.up_to_date === null) return;
    badge.classList.remove('bg-secondary', 'bg-success', 'bg-danger');
    badge.classList.add(data.up_to_date ? 'bg-success' : 'bg-danger');
    if (data.release_url) badge.href = data.release_url;
  } catch (_) {}
})();
setInterval(async () => {
  try {
    const res = await fetch(window.siteApiUrl('/sync/status'), { credentials: 'include', cache: 'no-store' });
    if (res.ok) renderSyncBadge(await res.json());
  } catch (_) {}
}, 60000);
