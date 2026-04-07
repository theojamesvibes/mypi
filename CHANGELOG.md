# Changelog

All notable changes to MyPi are documented here.

---

## [1.3.1] - 2026-04-07

### Fixed
- Pushover test now works regardless of the master enable toggle (uses `send_test()` which only requires saved credentials)
- Saving alert preferences no longer wipes credentials — empty token/user_key fields are ignored on PUT, preserving existing saved values
- Settings panel now shows masked saved credentials (`****xxxx`) below each field so it's clear they are stored
- Master enable toggle now visually distinct with a highlighted box and explanatory sub-label; badge shows "disabled" (yellow) instead of "configured, disabled"

---

## [1.3.0] - 2026-04-07

### Added
- **Pushover notifications** — Settings → Pushover panel with App Token / User Key, enable toggle, Validate and Test buttons
  - Alert: sync failure (any replica fails)
  - Alert: instance goes offline / comes back online (detects transition between polls)
  - Alert: no logs received for configurable time (default 30 min)
  - Alert: high block rate (configurable % above 7-day baseline; requires ≥7 days of data)
  - All settings persisted in `app_settings` DB table, restored on restart
- **Topbar sync badge** — sits between version and online count on every page; green (all replicas synced), yellow (partial), red (all failed / >24 h stale); hidden until first sync completes

### Changed
- Sync badge loads on every page via base.html inline script, not just the dashboard

---

## [1.2.8] - 2026-04-07

### Changed
- Replicas no longer run gravity after teleporter import — when gravity sync is enabled the master's gravity DB is already embedded in the teleporter zip, so a second gravity run on replicas is redundant; master still runs gravity before export
- Removed informational gravity note from Settings sync panel
- Dashboard sync indicator is now always visible (never hidden); uses a raw `fetch` call instead of `apiFetch` so any non-200 or auth redirect is handled gracefully; shows "never run", "unavailable", or the last sync time with red highlight if >24 h old

---

## [1.2.7] - 2026-04-07

### Changed
- Dashboard sync indicator now only fetches `/api/sync/status` (dropped the coupled `Promise.all` with `/api/sync/schedule` that was silently swallowing errors); shows whenever a sync has ever completed
- "Run gravity update after sync" option removed from Settings UI — gravity is always run (on master before export, on replicas after import) and is no longer configurable

---

## [1.2.6] - 2026-04-07

### Changed
- Sync now runs gravity on the **master first**, before exporting the teleporter zip, so replicas receive fresh blocklists in the import payload
- Dashboard sync indicator now shows whenever a sync has ever completed (not only when auto-sync is configured); added try/catch so errors don't silently hide it

### Fixed
- Last sync result (time, status, per-replica outcomes) now persisted to the `app_settings` table and restored on startup — the dashboard sync indicator survives container restarts

---

## [1.2.5] - 2026-04-07

### Fixed
- Sync schedule (interval, auto-gravity, import options) now persists across container restarts via a new `app_settings` DB table; previously all schedule state was in-memory and reset to "disabled" on every restart, causing the dashboard sync indicator to never appear

---

## [1.2.4] - 2026-04-07

### Changed
- Version number moved from sidebar footer to topbar (right of the collapse button) — larger, always visible
- Dashboard: "Pi synced at …" indicator appears below stat cards when automatic sync is enabled; time turns red if the last sync was more than 24 hours ago

---

## [1.2.3] - 2026-04-07

### Added
- Pi-hole sync: push configuration from a master instance to all replicas via the Pi-hole v6 teleporter API
- Sync schedule: configurable automatic sync interval (15 min / 30 min / 1 hr / 6 hr / 24 hr) or manual-only
- Auto-sync on gravity change: detects when the master's blocklist count changes and triggers an immediate sync
- Settings page: online badge now shows correct X/Y count; instances table shows master badge
- Settings page: save schedule button with visual confirmation

### Fixed
- Teleporter import connection reset (Pi-hole FTL restarts after import, closing the HTTP connection before the response completes — now treated as success)

---

## [1.2.2] - 2026-04-07

### Fixed
- Pi-hole sync: `incomplete chunked read` error after teleporter import treated as success (FTL restart is expected behaviour)

---

## [1.2.1] - 2026-04-07

### Fixed
- Pi-hole sync: increased HTTP timeout to 5 minutes for teleporter operations (Raspberry Pi hardware can take 30–90 seconds to process a large gravity database import)

---

## [1.2.0] - 2026-04-07

### Added
- Pi-hole sync feature: export teleporter zip from master, import to all replicas in parallel
- Settings page sync panel: import options (config, gravity, DHCP leases), run gravity toggle, per-replica result display with live polling
- `master: true` flag in `pihole_instances.yml` designates the sync source
- `GET /api/sync/status` and `POST /api/sync` API endpoints
- Favicon: green shield-check SVG matching the sidebar icon

---

## [1.1.3] - 2026-04-07

### Added
- SVG favicon using the Bootstrap shield-fill-check icon in green (`#00a65a`)

### Fixed
- `VERSION` file not copied into Docker image (showed `vdev`)

---

## [1.1.2] - 2026-04-07

### Added
- Top Blocked Domains and Top Clients table rows are now clickable — opens a drill-down modal showing all blocked queries for that domain or client
- Delegated click handling via `data-tbl` / `data-idx` attributes (no inline JS, XSS-safe)
- Hover highlight on drillable rows

---

## [1.1.1] - 2026-04-07

### Changed
- Removed global search button from topbar (query log page has equivalent filtering)
- Query log page topbar now shows "Updated HH:MM:SS" timestamp after each load or live refresh

---

## [1.1.0] - 2026-04-07

### Added
- Traefik integration: app served at `https://mypi.myssdomain.net` via the existing `proxy` network with Cloudflare TLS
- Version number displayed in sidebar footer, sourced from `VERSION` file
- Column sorting on query log: click any header to sort asc/desc
- Live view toggle on query log: refreshes every 2 seconds
- Online badge on query log page (was only updating on dashboard)
- Global search modal accessible from topbar search button
- `VERSION` file as single source of truth; read at startup and injected into all templates

### Fixed
- Query log data not updating: Pi-hole cursor paginates backwards through history; replaced with `from` timestamp parameter so each poll fetches only genuinely new queries
- DNS queries over time chart showing inflated numbers (millions): was summing cumulative daily totals across instances and snapshots; now counts actual `query_logs` rows per 10-minute bucket using `date_trunc`
- Browser caching of API GET responses: added `cache: 'no-store'` to all `fetch` calls
- Status filter dropdown had stale lowercase values; replaced with All / Blocked only / Permitted only using the `blocked` parameter
- Sidebar collapse only hid nav text when wrapped in `<span>`; nav labels now correctly wrapped
- Queries poll interval reduced from 300 s to 10 s for near-real-time log updates

---

## [1.0.0] - 2026-04-07

### Added
- Initial release
- Aggregated dashboard: total queries, blocked count, percent blocked, domains on blocklist across all Pi-hole instances
- Blocklist validation: turns card red if instances report different blocklist counts
- DNS queries over time chart (Chart.js bar)
- Query type breakdown (doughnut chart)
- Per-instance status table with online/offline badge
- Top permitted domains, top blocked domains, top clients panels
- Drill-down modal on top blocked domains and top clients
- Query log page: filterable, paginated, sortable
- Settings page: API key management, instance list, REST API info
- Pi-hole v6 REST API client with persistent sessions, SID persistence across restarts, rate-limit backoff (429 → 5 min pause), async lock preventing concurrent auth
- APScheduler background jobs: stats every 60 s, queries every 10 s, cleanup daily
- JWT session auth (cookie) + API key auth (`X-API-Key` header)
- Bootstrap 5 + Chart.js dashboard matching Pi-hole AdminLTE aesthetic
- Docker Compose setup with PostgreSQL 18
- Alembic migrations
- OpenAPI docs at `/docs`
