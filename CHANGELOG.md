# Changelog

All notable changes to MyPi are documented here.

---

## [1.7.1] — 2026-04-17

### Fixed
- Pi-hole instances reporting SSL handshake failure (`SSLV3_ALERT_HANDSHAKE_FAILURE`) when `VERIFY_PIHOLE_SSL=false`. OpenSSL 3.x defaults to `SECLEVEL=2`, which refuses to offer weak DHE cipher suites that older Pi-hole/lighttpd TLS configurations require. When certificate verification is disabled, MyPi now builds a permissive `ssl.SSLContext` (`SECLEVEL=1`, `CERT_NONE`, `check_hostname=False`) so the TLS handshake succeeds regardless of the Pi-hole's cipher configuration.

## [1.7.0] — 2026-04-17

### Changed
- Domain block/unblock completely rewritten for correctness and clarity.
  - New shield button on each query row opens a modal that fetches the real Pi-hole status (deny list, allow list, or unmanaged) before offering any action — no more acting on stale row data.
  - Four explicit API endpoints replace the old pair: `POST/DELETE /api/domains/deny` and `POST/DELETE /api/domains/allow`.
  - Blocking adds to the deny list and removes any allow override first; allowing adds to the allow list and removes any deny entry first. Both operations run on all instances in parallel (no sync/gravity run needed — FTL reloads lists immediately).
  - Modal shows deny/allow badge indicators, last query status, a plain-English summary of the effective state (Blocked / Allowed / Gravity blocked / Unmanaged / Conflict), and context-appropriate action buttons.
  - Per-instance success/failure results are displayed after each operation.

## [1.6.0] — 2026-04-17

### Changed
- Sync panel last-sync result: master instance now shows a green checkmark and a `master` pill badge (matching the style used in the Instances panels), consistent with replica rows. The plain "Master: name" heading is removed.
- `/docs` Swagger UI now shows the MyPi logo as its favicon instead of the default FastAPI CDN icon.

## [1.5.1] — 2026-04-17

### Fixed
- Dark mode: table headers with `table-light` (instances, API keys, query log, dashboard tables) now render with the same dark background as other card headers instead of white.

## [1.5.0] — 2026-04-17

### Added
- Settings screen: configurable query poll interval (10 s / 30 s / 60 s / 2 min / 5 min). Default 60 s for new installs; existing installs migrate to 10 s via DB migration to preserve prior behavior. Change takes effect immediately without restart.

### Changed
- Query poll interval is now persisted in `app_settings` and managed via the UI rather than the `QUERIES_POLL_INTERVAL` env var (env var still accepted as startup fallback).
- Stats polling no longer fetches Pi-hole version info on every 60 s tick; version checks now run exclusively via the dedicated hourly job — saves 1 API call/min per instance.
- Parallelized `get_top_stats` Pi-hole API calls (3 sequential → concurrent with `asyncio.gather`).
- Limited httpx keepalive pool to 2 connections per Pi-hole (was unlimited default of 20), reducing idle TCP connections held open against FTL.

### Fixed
- `run_gravity()` now respects `VERIFY_PIHOLE_SSL` setting (was hardcoded `verify=False`).

## [1.4.7] — 2026-04-16

### Fixed

- **`/openapi.json` 500 error** — `from __future__ import annotations` in API router files caused Pydantic to treat request body types as unresolved forward references during OpenAPI schema generation, misclassifying them as `Query` parameters and crashing. Removed the import from the five affected files (`auth.py`, `sync.py`, `version.py`, `notifications.py`, `domains.py`); Python 3.12 handles all used type syntax natively.

---

## [1.4.6] — 2026-04-14

### Added

- **`GET /api/health` endpoint** — unauthenticated discovery endpoint used by the iOS companion app. Returns server version, `stats_poll_interval`, and `queries_poll_interval` so the client can configure its polling cadence and detect staleness before an API key is configured.

---

## [1.4.5] — 2026-04-14

### Changed

- **Pi-hole Systems panel sort order** — master instance is always listed first, remaining instances sorted alphabetically. Order is applied both in the API query and client-side sort so it's consistent regardless of call path.
- **Master badge in Systems panel** — master instance now shows the same blue "master" pill used in the Settings → Pi-hole Instances table.

---

## [1.4.4] — 2026-04-14

### Fixed

- **Pi-hole Systems panel respects time range** — the per-instance table (Total Queries, Blocked, % Blocked, Clients) now reflects the selected time window instead of always showing Pi-hole's native "today since midnight" counters. The backend computes per-instance aggregations from `query_logs` grouped by `instance_id` for the active time window, identical to how the stat cards and charts are computed. Blocklist size remains time-independent (from the latest snapshot). Also eliminates a redundant `/api/instances` HTTP call — instance metadata is now returned as part of `/api/stats/summary`.

---

## [1.4.3] — 2026-04-14

### Fixed

- **Drill-down time window mismatch** — the query detail modal now uses the same time window as the graphs above it. Previously it always defaulted to 24 hours regardless of the selected window. Two root causes fixed:
  - `/api/queries` and `/api/queries/clients` now accept an optional `since` ISO datetime parameter (overrides `hours` when provided), allowing the "today" window to be expressed exactly.
  - Dashboard JS now stores `_drillSince` alongside `_drillHours` when `loadDashboard` runs, and passes `since` (or `hours`) to the drill-down query accordingly.

---

## [1.4.2] — 2026-04-14

### Added

- **Change password in settings** — new card on the Settings page lets any logged-in user change their password without leaving the page. Requires the current password, minimum 8 characters, and confirmation match. Uses the new `POST /api/auth/change-password` JSON endpoint.

---

## [1.4.1] — 2026-04-14

### Fixed

- **Smart backfill on startup** — `backfill_all_instances` previously ran the full 24-hour window fetch on every container start, even when the database was healthy and current. Now each instance checks `MAX(timestamp)` in `query_logs` first:
  - Data less than 10 minutes old → backfill skipped entirely.
  - Gap detected (downtime, truncate, first run) → only the missing window from the last stored timestamp forward is fetched, not the full 24h.

---

## [1.4.0] — 2026-04-14

### Security

- **Rate limiting** — `POST /login` (web and API) limited to 10 attempts/minute per source IP via `slowapi`. Returns HTTP 429 on breach.
- **JWT token revocation** — logout (web and API) now inserts the token's JTI into a `revoked_tokens` table. Subsequent requests with a revoked token are rejected immediately rather than being honoured until natural expiry. Expired entries are purged nightly.
- **Domain input validation** — block/unblock/check endpoints now validate domain strings against `^[a-zA-Z0-9._*-]+$`, returning HTTP 422 on invalid input.
- **API key hashing upgrade** — new API keys are hashed with HMAC-SHA256 (keyed on `SECRET_KEY`) instead of plain SHA-256, preventing offline cracking of a database dump. Existing keys are transparently rehashed on first verified use — no re-issue required.
- **`VERIFY_PIHOLE_SSL` setting** — new boolean env var (default `false`) controls whether TLS certificates are verified when connecting to Pi-hole instances over HTTPS. Default preserves existing behaviour for self-signed certificates.

### Upgrade notes

- Alembic migration `0007` runs automatically: adds `revoked_tokens` table and `api_keys.key_hash_algo` column. No manual steps required.
- Add `VERIFY_PIHOLE_SSL=true` to `.env` if your Pi-hole instances use valid TLS certificates.

---

## [1.3.0] — 2026-04-14

### Security
- **Pi-hole API passwords encrypted at rest** — `api_password` is now stored using
  Fernet symmetric encryption (AES-128-CBC + HMAC-SHA256). `ENCRYPTION_KEY` is
  auto-generated and persisted to the database on first startup if not set — no manual
  action required. Optionally add it to `.env` for portability (the key is logged as a
  warning on first boot). Legacy plaintext rows are handled gracefully on read and
  re-encrypted by `config_loader` on first startup after upgrade.
- **DOM-based XSS eliminated** — block/unblock buttons in the query log are now built
  entirely with DOM methods (`createElement` / `addEventListener`). Domain names from
  Pi-hole can no longer inject HTML or JavaScript via `onclick` attributes.
- **Exception details removed from API error responses** — internal exception messages
  (DB errors, stack traces) are no longer returned to clients. Errors are logged
  server-side; clients receive generic messages only.
- **LIKE wildcard injection fixed** — `%` and `_` in domain/client filter parameters
  are now escaped before use in `ILIKE` queries, preventing filter bypass.
- **Password change required on first login** — the initial admin account is created
  with `password_change_required=True`. All web UI routes redirect to `/change-password`
  until a new password is set. Applies to fresh installs only; existing accounts are
  unaffected.
- **`SECURE_COOKIES` env var** — session cookies now respect a `SECURE_COOKIES=true`
  setting that adds the `Secure` flag. Default is `false` for plain-HTTP local access.
  Set to `true` when running behind Traefik or any TLS-terminating reverse proxy.

### Upgrade notes
- No manual steps required. Database migration 0006 (`password_change_required` column
  on `users`) and `ENCRYPTION_KEY` auto-generation both run automatically on first startup.

---

## [1.2.9] — 2026-04-14

### Changed
- **Backfill uses hourly windows instead of a single large page** — the previous 1.2.8
  approach fetched with `length=10000` from a 24h start timestamp, which is insufficient
  for high-volume DNS platforms. The new approach slices the 24h range into one-hour
  clock windows (00:00–01:00, 01:00–02:00, …, current_hour–now), issuing a separate
  request per window. Each window still paginates internally (up to 20 pages × 10k rows
  = 200k queries per hour) if Pi-hole returns a full page. Added `until` parameter
  support to `PiholeClient.get_queries` to bound each fetch precisely.

---

## [1.2.8] — 2026-04-14

### Added
- **24h query backfill on startup** — at startup MyPi now fetches up to 24 hours of
  historical queries from each Pi-hole instance and inserts any rows not already in the
  database. This recovers the query log automatically after a `TRUNCATE query_logs` (e.g.
  following page corruption). Backfill runs in parallel across all instances as a
  background task so it does not delay startup. Pagination handles high-volume instances
  (10k queries per page, up to 50 pages); existing rows are skipped via `pihole_query_id`
  deduplication, so running against a live table is safe.

---

## [1.2.7] — 2026-04-14

### Fixed
- **Unblock now uses the correct Pi-hole DELETE endpoint** — confirmed from Pi-hole
  FTL source (`src/api/list.c`): `DELETE /api/domains/deny/exact/{domain}` takes the
  domain name as the URI path segment, not a numeric database ID. The 1.2.4 change
  to look up a numeric `id` and DELETE by that (e.g. `/api/domains/deny/exact/5`)
  caused Pi-hole to treat `5` as an unknown domain name, return 404, and throw an
  exception that was silently swallowed — leaving the domain still blocked while
  MyPi reported success. `unblock_domain` is now a direct one-shot DELETE by domain
  name with no prior GET required.

---

## [1.2.6] — 2026-04-14

### Fixed
- **Block status check now covers all instances** — `GET /api/domains/block/{domain}`
  previously only checked the master Pi-hole. If an unblock succeeded on master but
  failed on a replica, MyPi would report the domain as unblocked while DNS queries
  to the replica still returned `DENYLIST`. The check now queries every active
  instance and returns `blocked: true` as soon as any one of them has the entry.
- **Deny-list lookup now matches both `domain` and `name` fields** — Pi-hole's
  `GET /api/domains/deny/exact` response may use a `name` key rather than `domain`
  for the domain string. Both `unblock_domain` and `is_domain_blocked` now check
  either key, preventing silent failures where the entry existed but was never found.
- **INFO-level logging added** for all deny-list operations — the number of entries
  returned by Pi-hole, the field keys in each entry, and the ID used for DELETE are
  now logged so problems can be diagnosed from `docker compose logs mypi`.

---

## [1.2.5] — 2026-04-14

### Fixed
- **Block/Unblock button now reflects actual Pi-hole deny-list state** — the
  button's initial label (Block vs Unblock) was inferred from the query log status,
  which reflects past queries and not the current blocking state. A domain blocked
  after queries were logged would still show "Block" on those old rows. Now, clicking
  the button calls `GET /api/domains/block/{domain}` to check the real current state
  on the master Pi-hole before showing the confirm dialog — so "Unblock?" is shown
  for currently-blocked domains regardless of the logged query status.

---

## [1.2.4] — 2026-04-14

### Fixed
- **Unblock domain now works correctly** — two root causes addressed:
  1. Pi-hole v6 assigns each deny-list entry a numeric database ID; the DELETE
     endpoint requires that ID, not the domain string. `unblock_domain` now GETs
     the full deny list first, finds the entry by domain name, and DELETEs by ID
     (falling back to domain-name-in-path if no `id` field is present).
  2. The post-unblock sync called `run_sync` which always runs gravity on the
     master before exporting — this re-added the domain to `gravity.db` from adlists
     and re-blocked it on every replica. Unblock now calls `unblock_domain` directly
     on every active instance instead of triggering a full sync, so no gravity runs
     and adlist-based re-adds cannot occur.
- **Block/Unblock requires two clicks** — single-click accidental blocks/unblocks
  eliminated. Clicking Block or Unblock now shows an inline confirmation prompt
  ("Block? ✓ ✗" / "Unblock? ✓ ✗"). The ✓ confirms the action; ✗ restores the
  original button without taking any action.

---

## [1.2.3] — 2026-04-14

### Fixed
- **Startup crash on FastAPI route registration** — `status_code=204` on routes
  that accept a request body triggers a FastAPI assertion (`Status code 204 must
  not have a response body`). Both domain block/unblock endpoints now return
  `Response(status_code=204)` explicitly instead of declaring it on the decorator,
  which satisfies FastAPI's validator and preserves the correct HTTP semantics.

---

## [1.2.2] — 2026-04-14

### Fixed
- **Version Check panel: Pi-hole version numbers now display in green** — the
  core / FTL / web latest-version text in the Settings Version Check panel was
  using `text-muted` instead of `text-success`, unlike the MyPi version line.

---

## [1.2.1] — 2026-04-14

### Added
- **Block/Unblock domains from the query log** — each row in the Query Log now
  shows a Block or Unblock button. Blocked-status queries show Unblock; all others
  show Block. Clicking calls `POST /api/domains/block` or
  `DELETE /api/domains/block/{domain}`, which adds or removes the domain from the
  exact deny list on the master Pi-hole then triggers a gravity sync to all
  replicas in the background. HTTPS with self-signed certs was already supported
  via `verify=False` on all Pi-hole API calls.

---

## [1.2.0] - 2026-04-13

### Added
- **Dark mode** — Settings → Appearance card lets users choose Light, Dark, or System
  (follows OS `prefers-color-scheme`). The preference is stored in `localStorage` and
  applied synchronously before first paint to eliminate any flash of wrong theme.
  Built on Bootstrap 5.3's native `data-bs-theme` attribute; custom overrides handle
  the topbar, card headers, table hover rows, and status pills which Bootstrap does not
  automatically retheme. Chart.js axes, grid lines, and tooltips update to match.

---

## [1.1.3] - 2026-04-13

### Fixed
- **Pi-hole version check DB churn** — `_refresh_instance_update_flags()` is now skipped when the fetched latest versions are unchanged from the cached values (e.g. repeated GitHub failures or no new release). Eliminates redundant writes on every hourly check when GitHub is unreachable.
- **Pi-hole installed versions fetched at startup and post-sync** — a lightweight version fetch (no stats, no query poll) now runs for all active instances immediately at startup and 15 seconds after each sync completes (to allow FTL to restart after teleporter import). This ensures the Instances table always shows current installed versions without waiting for the next 60-second stats poll.
- **Version check failure notices** — the Instances table now shows a concise warning at the bottom if the MyPi or Pi-hole GitHub version check failed (e.g. no internet access to GitHub). The notice only appears when a check was attempted and produced no result; it is hidden when checks are disabled or passing normally.

---

## [1.1.2] - 2026-04-13

### Fixed
- **Sync socket pollution (intermittent "illegal status line" error)** — `run_gravity()` now uses a dedicated throwaway HTTP connection instead of the shared persistent client. Pi-hole's gravity endpoint has inconsistent HTTP framing that can leave stale bytes in the socket; by isolating gravity to its own connection the persistent client is never contaminated. The same session SID is reused so Pi-hole's `max_sessions` limit is unaffected.
- **Teleporter export retry** — if the teleporter export from the master fails (e.g. due to residual socket data), MyPi automatically retries once after a 2-second delay before reporting the sync as failed. Most intermittent sync errors will now self-recover without user intervention.
- **Version info fetch failures now logged at WARNING** — previously a failure to retrieve Pi-hole version data was silently swallowed at DEBUG level, causing the version columns in the Instances table to remain blank with no visible indication of why. These failures are now visible in the log.

---

## [1.1.1] - 2026-04-13

### Changed
- **Pi-hole version pills** — the Pi-hole, FTL, and Web version columns in the Settings → Instances table now render as filled Bootstrap pill badges (green = up to date, red = update available, grey = not yet checked) matching the MyPi version badge style. Each pill is a clickable link to the corresponding release on GitHub (pi-hole/pi-hole, pi-hole/FTL, pi-hole/web).
- **Settings instances legend removed** — the "Up to date / Update available" legend row below the instances table has been removed; the pills are self-explanatory.

---

## [1.1.0] - 2026-04-13

### Added
- **Stat card footer links** — each of the four dashboard summary cards now has a Pi-hole–style footer strip with a clickable link: Total Queries → Query Log (Unique Clients view); Queries Blocked → Query Log (blocked only); Percent Blocked → Query Log (all queries); Domains on Blocklist → master Pi-hole's `groups-lists` page in a new tab. The client count in the Total Queries footer is populated from live summary data.
- **Unique Clients view in Query Log** — new "Unique Clients" option in the Show dropdown. Switches the table to a per-client aggregate view (client name/IP, total queries, blocked count, % blocked, last seen) backed by a new `GET /api/queries/clients` endpoint. Navigating from the dashboard card pre-selects this view automatically.
- **Query Log URL param pre-selection** — navigating to `/queries?blocked=true`, `/queries?blocked=false`, or `/queries?show=clients` now pre-sets the Show filter automatically, enabling direct deep links from the dashboard.
- **Dashboard time range expansion** — added "Last 15 minutes", "Last 1 hour", and "Today" (since local midnight) to the time range selector. Sub-hour and today windows pass an ISO `since` timestamp to the backend; chart bucket granularity scales automatically (1 min for 15 min, 5 min for 1 hr, 30 min for Today, 60 min for 7 d, etc.). Default remains 24 hours.
- **Version check** — MyPi now checks its own GitHub repository for a new release once per hour. The version badge in the top bar turns green (up to date) or red (update available) and links directly to the GitHub releases page. An initial check runs at startup. A new "Version Check" card on the Settings page shows current vs latest version, last check time, a "Check now" button, and a toggle to disable checking entirely. State (enabled flag, latest version, last checked) is persisted in the `app_settings` table.
- **Settings URL hyperlinks** — the URL column in the Pi-hole Instances table is now a clickable link that opens the Pi-hole web UI in a new tab.

### Changed
- **Settings panel order** — Pushover Notifications moved below Session Timeout (less frequently used settings lower in the page). New order: Instances → Sync → Session Timeout → Pushover → Version Check → API Keys → REST API.
- **Stats API** — all three endpoints (`/api/stats/summary`, `/api/stats/history`, `/api/stats/top`) now accept an optional `since` (ISO 8601 datetime) parameter in addition to `hours`, enabling sub-hour and calendar-day windows. `/api/stats/history` also accepts `bucket_minutes` to control chart bucket granularity.

---

## [1.0.18] - 2026-04-13

### Added
- **Retries before offline alert** — new "Retries before alert" selector (1–10) in the Pushover Notifications panel under the "Instance goes offline" toggle. Each retry waits one poll cycle (60 s) before firing the first alert; the existing "Repeat alert while offline" count takes over after that. If the instance recovers before retries are exhausted, no alert is sent and the spurious "back online" notification is also suppressed. Implemented via `_offline_alert_retries` in `pushover.py` and a per-instance `_offline_retry_count` in `collector.py`.

---

## [1.0.17] - 2026-04-13

### Changed
- **Pi-hole software versions moved into the instances table** — replaces the separate 1.0.16 versions card. Pi-hole (core), FTL, and Web interface versions are now shown as three additional columns directly in the "Pi-hole Instances" table on the Settings page. Versions are fetched from each Pi-hole's `/api/info/version` endpoint on every stats poll cycle and persisted to new columns on `pihole_instances` (migration 0005), so they survive container restarts and are always available from the database rather than requiring a live round-trip. Color coding: green = up to date, red = update available, muted = no remote comparison data yet.

### Removed
- Dedicated "Pi-hole Software Versions" card and `GET /api/instances/versions` endpoint introduced in 1.0.16 (superseded by the above).

---

## [1.0.16] - 2026-04-12

### Added
- **Pi-hole software versions table** — new "Pi-hole Software Versions" card on the Settings page, placed between the instances list and the sync card. Shows a table with Pi-hole (core), FTL, and web interface versions for every active instance. Versions are fetched concurrently from each Pi-hole's `/api/info/version` endpoint using the shared persistent clients from `client_manager`. Up-to-date versions render in green; versions where an update is available render in red with the latest version shown inline. A refresh button re-fetches on demand. Instances that are offline or unreachable show their error inline. Implemented via `PiholeVersionInfo`/`ComponentVersion` dataclasses in `pihole_client.py`, `InstanceVersionInfo`/`ComponentVersionSchema` Pydantic models in `schemas/instance.py`, and a new `GET /api/instances/versions` endpoint in `api/instances.py`.

---

## [1.0.15] - 2026-04-11

### Added
- **Configurable offline alert repeat count** — new "Repeat alert while offline" select in the Pushover notification settings, grouped under the "Instance goes offline / comes back" toggle. Options: once (transition only, default — preserves previous behaviour), up to 2–10 times, or "Always (every check)". The counter resets automatically when the instance recovers. Stored in `pushover_settings` alongside the other notification preferences. Implemented via `_offline_alert_max_count` in `pushover.py` and a per-instance counter in `collector.py`.

---

## [1.0.14] - 2026-04-11

### Fixed
- **Sync status badge showed wrong total count on partial failure** — when 1 of 3 instances (master + 2 replicas) failed, the badge showed `1/2` instead of `2/3`. The master is now counted in both the numerator and denominator when per-replica results are present (a master failure produces a global error, not individual results).

### Added
- **Configurable dashboard session timeout** — new "Session Timeout" card in Settings with options: 15 minutes, 1 hour, 8 hours (default), 1 day, 1 week, Never. Setting is persisted to the `app_settings` table and applied to both the JWT expiry and cookie `max_age` on the next login. Implemented via `app/services/session_settings.py` following the same DB persistence pattern as sync and Pushover settings.
- **Log MyPi version at startup** — INFO-level log line during lifespan startup makes the running version immediately visible in `docker logs mypi`.

---

## [1.0.13] - 2026-04-10

### Fixed
- **Dashboard stat cards did not update when switching time ranges** — the summary API endpoint always returned snapshot-based counters (Pi-hole's "today" totals), ignoring the selected time range. The endpoint now computes total queries, blocked, percent blocked, forwarded, cached, and unique clients directly from the `query_log` table filtered by the selected window, consistent with how the history and top-domains charts already work. Blocklist size remains snapshot-derived (it is time-independent).

### Changed
- "Total Queries Today" stat card label shortened to "Total Queries" since the value now reflects whichever time range is selected, not just today.
- Added **Last 30 days** (720 h) option to the dashboard time-range selector.
- Raised the `hours` upper bound from 168 to 720 on the `/api/stats/summary`, `/api/stats/history`, and `/api/stats/top` endpoints to support the 30-day range.

---

## [1.0.12] - 2026-04-08

### Fixed
- **Sync teleporter export fails with `illegal status line`** — Pi-hole's gravity endpoint has inconsistent HTTP framing that leaves response body bytes in the TCP socket regardless of whether the response is drained at the application level. When `get_teleporter()` reused the same persistent connection, httpx read the leftover gravity body as the beginning of the teleporter response status line, producing a mangled bytearray. Fixed by resetting the httpx connection pool in a `finally` block at the end of `run_gravity()` so the next request always starts on a fresh socket. The authenticated SID is preserved so no re-authentication is required.
- **Pi-hole session exhaustion** — the sync service was creating throwaway `PiholeClient` instances for every operation (up to `2 + 2N` new sessions per sync run for N replicas) and never released them server-side, causing sessions to accumulate until hitting `webserver.api.max_sessions`.

### Changed
- Extracted Pi-hole client lifecycle into a new `app/services/client_manager.py` module. Both the collector (polling) and the sync service now share one persistent authenticated client per instance. No new Pi-hole sessions are created during a sync — the existing polling session is reused, with automatic re-authentication on 401.

---

## [1.0.8] - 2026-04-08

### Fixed
- Pi-hole instances with no password set are now handled correctly. Pi-hole v6 returns `200` with `"sid": null` when authentication is not required; MyPi previously treated the null SID as an auth failure and marked the instance offline. A `_no_auth` flag is now set in this case and no `X-FTL-SID` header is sent on subsequent requests.

---

## [1.0.7] - 2026-04-08

### Fixed
- `run_gravity()` no longer raises a JSON parse error when Pi-hole returns a plaintext streaming log instead of JSON. The gravity endpoint streams progress text on instances where FTL does not restart after completion; the response body is now intentionally ignored.

---

## [1.0.6] - 2026-04-08

### Fixed
- **Sync gravity**: Replicas now run a gravity update after each teleporter import when "Gravity" sync is enabled. The teleporter carries the adlist sources but the compiled domain list must be rebuilt on each Pi. Previously, replicas received the correct adlists but their `domains_being_blocked` count stayed stale, causing the "Instances disagree" warning to persist after a successful sync.
- **Dashboard instance count**: `GET /api/instances` now filters to active instances only. Renamed or removed instances that remain soft-deleted in the database no longer appear in the dashboard or inflate the "N online" badge.

### Added
- **Orphaned Instances** section in Settings: when `pihole_instances.yml` is changed (instances renamed or removed), the old records are detected and shown in a warning card. Each can be individually removed — along with all associated stats and query logs — or wiped in bulk via "Remove all orphaned instances".
- `GET /api/instances/stale` — lists inactive (orphaned) instances.
- `DELETE /api/instances/{id}` — permanently removes an inactive instance and its data; returns 409 if the instance is still active.

---

## [1.0.5] - 2026-04-08

### Added
- Startup diagnostics in `load_schedule()`: on every container start, MyPi now logs a dump of all rows in `app_settings` (at WARNING level so it's never filtered out), runs a write/read-back round-trip test, and logs a clear PASS or FAIL — making it immediately visible in `docker logs mypi` whether settings are in the database and whether the DB is writable.

---

## [1.0.4] - 2026-04-08

### Fixed
- Settings persistence now verified: after every DB write, a fresh session immediately reads the row back and raises an error if it is missing or wrong — no more silent failures
- `PUT /api/sync/schedule` and `PUT /api/notifications/settings` now return HTTP 500 with the exact error message if the write or verification fails, instead of returning 200 regardless
- Save buttons in the UI now show a red "Save failed" state and an alert with the server error message if the API returns an error; previously they always showed "Saved"

---

## [1.0.3] - 2026-04-08

### Fixed
- Settings persistence (third time): replaced `session.merge()` with a native PostgreSQL `INSERT … ON CONFLICT DO UPDATE` in both `sync_service` and `pushover`. The ORM merge performed a hidden SELECT round-trip and silently swallowed any exception, so the UI showed "Saved" while nothing was written to the database. The native upsert is a single atomic statement with no ORM state management involved.

---

## [1.0.2] - 2026-04-07

### Fixed
- Settings persistence second attempt: replaced PostgreSQL-specific `pg_insert … ON CONFLICT DO UPDATE` upsert with SQLAlchemy's standard `session.merge()`, which is more reliable in async context and avoids any dialect-specific edge cases
- Added try/except around DB queries in `load_schedule()` and `load_settings()` so a table error on startup is caught and logged rather than silently ignored
- Added diagnostic logging on load: logs clearly whether persisted settings were found in DB or not, making it easier to diagnose persistence failures from container logs

---

## [1.0.1] - 2026-04-07

### Fixed
- Sync schedule and Pushover settings now correctly persist across container restarts. `set_schedule()` was synchronous and used a fire-and-forget `asyncio.get_event_loop().create_task()` to write to the database — in some uvicorn/Python 3.12 contexts this task would silently fail, leaving settings un-persisted. Converted to a proper `async` function with an awaited DB write so persistence is guaranteed before the response is returned. All remaining `asyncio.get_event_loop().create_task()` calls replaced with `asyncio.create_task()`.

---

## [1.0.0] - 2026-04-07 — Public release

First public release. Full feature set:

- Unified dashboard aggregating stats from up to 10 Pi-hole v6 instances
- DNS Queries over Time chart, Query Type breakdown, per-system status table
- Drill-down modals on Top Blocked Domains and Top Clients
- Consolidated query log with sorting, filtering, live view, and pagination
- Pi-hole Sync: master → replicas via teleporter API with gravity-first order
- Configurable auto-sync schedule (15 min – 24 hr) and gravity-change detection
- Sync schedule and last result persisted across container restarts
- Topbar sync badge (green / yellow / red) on every page
- Pushover push notifications: sync failure, instance offline, no logs, high block rate
- JWT session auth for web UI, API key auth for mobile/automation
- Full REST API with OpenAPI docs at `/docs` and `/redoc`
- Docker Compose setup with PostgreSQL 18 and optional Traefik integration

---

## [0.4.1] - 2026-04-07

### Fixed
- Pushover test now works regardless of the master enable toggle (uses `send_test()` which only requires saved credentials)
- Saving alert preferences no longer wipes credentials — empty token/user_key fields are ignored on PUT, preserving existing saved values
- Settings panel now shows masked saved credentials (`****xxxx`) below each field so it's clear they are stored
- Master enable toggle now visually distinct with a highlighted box and explanatory sub-label; badge shows "disabled" (yellow) instead of "configured, disabled"

---

## [0.4.0] - 2026-04-07

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

## [0.3.8] - 2026-04-07

### Changed
- Replicas no longer run gravity after teleporter import — when gravity sync is enabled the master's gravity DB is already embedded in the teleporter zip, so a second gravity run on replicas is redundant; master still runs gravity before export
- Removed informational gravity note from Settings sync panel
- Dashboard sync indicator is now always visible (never hidden); uses a raw `fetch` call instead of `apiFetch` so any non-200 or auth redirect is handled gracefully; shows "never run", "unavailable", or the last sync time with red highlight if >24 h old

---

## [0.3.7] - 2026-04-07

### Changed
- Dashboard sync indicator now only fetches `/api/sync/status` (dropped the coupled `Promise.all` with `/api/sync/schedule` that was silently swallowing errors); shows whenever a sync has ever completed
- "Run gravity update after sync" option removed from Settings UI — gravity is always run (on master before export, on replicas after import) and is no longer configurable

---

## [0.3.6] - 2026-04-07

### Changed
- Sync now runs gravity on the **master first**, before exporting the teleporter zip, so replicas receive fresh blocklists in the import payload
- Dashboard sync indicator now shows whenever a sync has ever completed (not only when auto-sync is configured); added try/catch so errors don't silently hide it

### Fixed
- Last sync result (time, status, per-replica outcomes) now persisted to the `app_settings` table and restored on startup — the dashboard sync indicator survives container restarts

---

## [0.3.5] - 2026-04-07

### Fixed
- Sync schedule (interval, auto-gravity, import options) now persists across container restarts via a new `app_settings` DB table; previously all schedule state was in-memory and reset to "disabled" on every restart, causing the dashboard sync indicator to never appear

---

## [0.3.4] - 2026-04-07

### Changed
- Version number moved from sidebar footer to topbar (right of the collapse button) — larger, always visible
- Dashboard: "Pi synced at …" indicator appears below stat cards when automatic sync is enabled; time turns red if the last sync was more than 24 hours ago

---

## [0.3.3] - 2026-04-07

### Added
- Pi-hole sync: push configuration from a master instance to all replicas via the Pi-hole v6 teleporter API
- Sync schedule: configurable automatic sync interval (15 min / 30 min / 1 hr / 6 hr / 24 hr) or manual-only
- Auto-sync on gravity change: detects when the master's blocklist count changes and triggers an immediate sync
- Settings page: online badge now shows correct X/Y count; instances table shows master badge
- Settings page: save schedule button with visual confirmation

### Fixed
- Teleporter import connection reset (Pi-hole FTL restarts after import, closing the HTTP connection before the response completes — now treated as success)

---

## [0.3.2] - 2026-04-07

### Fixed
- Pi-hole sync: `incomplete chunked read` error after teleporter import treated as success (FTL restart is expected behaviour)

---

## [0.3.1] - 2026-04-07

### Fixed
- Pi-hole sync: increased HTTP timeout to 5 minutes for teleporter operations (Raspberry Pi hardware can take 30–90 seconds to process a large gravity database import)

---

## [0.3.0] - 2026-04-07

### Added
- Pi-hole sync feature: export teleporter zip from master, import to all replicas in parallel
- Settings page sync panel: import options (config, gravity, DHCP leases), run gravity toggle, per-replica result display with live polling
- `master: true` flag in `pihole_instances.yml` designates the sync source
- `GET /api/sync/status` and `POST /api/sync` API endpoints
- Favicon: green shield-check SVG matching the sidebar icon

---

## [0.2.3] - 2026-04-07

### Added
- SVG favicon using the Bootstrap shield-fill-check icon in green (`#00a65a`)

### Fixed
- `VERSION` file not copied into Docker image (showed `vdev`)

---

## [0.2.2] - 2026-04-07

### Added
- Top Blocked Domains and Top Clients table rows are now clickable — opens a drill-down modal showing all blocked queries for that domain or client
- Delegated click handling via `data-tbl` / `data-idx` attributes (no inline JS, XSS-safe)
- Hover highlight on drillable rows

---

## [0.2.1] - 2026-04-07

### Changed
- Removed global search button from topbar (query log page has equivalent filtering)
- Query log page topbar now shows "Updated HH:MM:SS" timestamp after each load or live refresh

---

## [0.2.0] - 2026-04-07

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

## [0.1.0] - 2026-04-07

### Added
- Initial build
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
