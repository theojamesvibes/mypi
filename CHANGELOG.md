# Changelog

All notable changes to MyPi are documented here.

---

## [1.11.0-dev.15] — 2026-04-25

### Added
- **VIP cluster summary in startup log.** When `pihole_instances.yml` has any `vip_master` / `vip_replica` flags, `config_loader.sync_sites_and_instances` now emits one INFO line per site at startup naming the master and the replica list — operators can confirm at a glance that the YAML flags were picked up without having to query the DB. Sites with no VIP-flagged instances stay quiet.

### Changed
- `app/services/config_loader.py` — added the per-site VIP summary loop just before the existing "Config sync complete" line.

---

## [1.11.0-dev.14] — 2026-04-25

The dev.13 stalled-state detector was firing all night against a Pi-hole sitting behind a VIP as a hot standby — by design that node sees no DNS traffic until failover, so its `dns_queries_today` counter and query watermark are flat indefinitely, and the new detector couldn't tell "wedged" from "idle by role." This release re-introduces explicit cluster-membership flags so the detector knows when "no traffic" is normal, and adds a transfer-detection signal as a side benefit.

### Added
- **`vip_master` / `vip_replica` YAML flags** on instances. Both optional; absence preserves the previous behaviour. At most one `vip_master` per site (extras are demoted with a warning); many `vip_replica` allowed. Flags are independent of `master:` (the sync master). New nullable `vip_role` column on `pihole_instances` (migration 0016) backs the flag.
- **VIP-aware stall detection.** Per-instance stall alerts are skipped for any node in a VIP cluster. A new group-level alert (`notify_vip_group_stalled`) fires only if *every* node in the cluster has been flat for ≥ 5 polls (~5 minutes) AND the cluster has historically seen at least one advance — so a fresh install with no traffic doesn't false-positive. Truly-dead instances are still caught by the existing offline check.
- **VIP transfer alert.** When the active node in a VIP cluster shifts (master → replica or back), a `notify_vip_transfer` Pushover alert fires, gated by a new opt-in toggle on Settings → Pushover (default off). Detection waits for the candidate node to advance for `_VIP_TRANSFER_CONFIRM_POLLS = 2` consecutive polls before declaring the transfer, so a single transient on the master doesn't bounce the active label.

### Changed
- `app/services/collector.py` — `_check_stalled` split: a new `_instance_advanced` helper centralises the "did it advance this poll?" check and returns the boolean. Per-instance stall now early-returns for any instance with `vip_role is not None`. New `_check_vip_state` runs once per `poll_stats_for_site` and handles transfer detection + group-level stall. New site-keyed dicts (`_vip_active_node`, `_vip_group_stall_alerted`, `_site_poll_seq`) and instance-keyed dicts (`_vip_last_advance_seq`, `_vip_advance_streak`) tracked + pruned alongside the existing state.
- `poll_stats_for_site` now collects `(snapshot, advanced)` tuples from each per-instance poll and passes them into the VIP check after `asyncio.gather` completes. Switched to `return_exceptions=True` so a single instance crashing the poll doesn't take VIP bookkeeping offline for the rest of the cluster.
- `app/services/pushover.py` — added `_alert_on_vip_transfer` setting (default false), persisted in the `pushover_settings` site_settings JSON. Three new alert helpers: `notify_vip_transfer`, `notify_vip_group_stalled`, `notify_vip_group_recovered`. Group stall reuses the offline alert toggle; transfer has its own dedicated toggle.
- `app/api/notifications.py` — `PushoverSettingsRequest` gains `alert_on_vip_transfer`.
- `app/templates/settings.html` + `app/static/js/dashboard.js` — added "VIP transfer" alert checkbox under the existing alert events column.
- `app/services/config_loader.py` — passes `vip_role` through on insert/update.
- `app/config.py` — `_parse_instance` reads `vip_master` / `vip_replica` (mutually exclusive); `_parse_site` enforces "one vip_master per site."

### Migration notes
- Migration 0016 adds a nullable `vip_role` VARCHAR(16) on `pihole_instances`. Existing rows get NULL and behave exactly as before.
- To opt a cluster in, set `vip_master: true` on the active node and `vip_replica: true` on each standby in the same site, then restart.
- The transfer alert is opt-in via Settings → Pushover. The group-stall alert reuses the existing instance-offline toggle so users who've muted offline alerts don't get woken up twice.

---

## [1.11.0-dev.13] — 2026-04-25

Two fixes that came out of the same incident: the Pi-hole upgrade earlier this evening left pihole1's FTL in a "split-state" (admin API responsive, query logging frozen) for ~4.5 hours, and while diagnosing it we found that Top Clients drill-downs return zero rows for any client whose traffic is mostly permitted.

### Fixed
- **Top Clients drill-down on the per-site dashboard.** Clicking a client (e.g. `wtranon.myssdomain.net`, `pi.hole`, or any LAN host with no blocked traffic) used to open the drill modal with a stale `blocked=true` filter copied from the Top *Blocked* panel, so any client whose queries were all permitted appeared empty. Filter dropped — the drill now shows every matching query, blocked or not. Top *Blocked* still legitimately filters to blocked-only.

### Added
- **Top Permitted drill-down on the per-site dashboard.** Previously you could drill into Top Blocked and Top Clients but not Top Permitted; now all three top-N panels are drillable.
- **Stalled-state detection in the collector.** After each successful stats poll, MyPi compares Pi-hole's `dns_queries_today` counter and our internal `/api/queries` watermark to their values from the previous poll. If both are flat for 5 consecutive polls (~5 minutes) and the admin API is still responsive, the instance is flagged as stalled and a Pushover notification fires (reuses the existing instance-offline alert toggle). On recovery — either signal advancing — a "recovered" notification fires. Midnight counter rollover is treated as a natural reset, never as evidence of stall. Catches the failure mode pihole1 hit tonight: after the FTL upgrade restart, port 53 / query logging stopped while the admin API kept answering, so the existing online check passed and no alert was raised. Detection is in-memory only; state clears on container restart and on instance deactivation.

### Changed
- `app/services/collector.py` — added `_check_stalled` plus the four supporting state dicts (`_prev_dns_queries_today`, `_prev_watermark_for_stall`, `_stall_count`, `_stall_alerted`), wired into `_poll_stats_for` and pruned alongside the existing per-instance state in `prune_inactive_state` and `shutdown`.
- `app/services/pushover.py` — added `notify_instance_stalled` and `notify_instance_recovered_from_stall`, mirroring the offline/back-online pattern. Wording calls out the likely fix (`systemctl restart pihole-FTL`).

### Migration notes
- No DB changes.
- Existing offline alert config governs stalled alerts too; nothing to opt into. If you have offline alerts disabled, you also won't see stalled alerts (this matches user expectation — both are "your Pi-hole isn't really working" alerts).
- Threshold (5 polls) is hardcoded for now. If 5 minutes turns out to be too aggressive or too slow for any deployment, we'll surface it as a Settings field next release.

---

## [1.11.0-dev.12] — 2026-04-24

Cross-site dashboard plus two small quality-of-life changes for the multi-site UX.

### Added
- **Combined Information page** at `/combined` — aggregate dashboard covering every active instance across every active site. Included widgets: the four headline stat cards (summed across sites), DNS Queries over Time (single aggregate line), Query Types pie, a Pi-hole Systems table with a **Site** column, and Top Permitted / Top Blocked (merged by domain across sites — Top Clients is omitted because IP collisions between sites refer to different machines). A live-activity ticker at the top shows the 15 most recent queries across all sites, color-tagged by site, with new rows animated in every ~3s. Nav item appears only when ≥2 active sites are configured; hidden on single-site deployments. Read-only — actions (sync, enable/disable, etc.) stay on per-site pages. Backend reuses the existing `/api/stats/*` global endpoints, which already aggregate across every active instance; `_summary_body` now populates `site_id` / `site_name` / `site_slug` on each per-instance payload so the Combined view can attribute instances back to their site.
- **`combined` added to `RESERVED_SLUGS`** in `app/config.py`. Any site slug resolving to `combined` is rejected at YAML parse with the existing reserved-slug error. Belt-and-suspenders: the nav item is also hidden whenever only one site is configured.
- **Site name in per-screen titles** — on per-slug pages (`/dashboard/<slug>`, `/queries/<slug>`, `/settings/<slug>`) the tab title and in-page heading now append `: <SiteName>` so you can tell at a glance which site you're viewing. Driven client-side by the existing `/api/sites` fetch in `base.html`; legacy `/`, `/queries`, `/settings` URLs and `/combined` are unchanged.
- **Clickable MyPi logo** in the sidebar. Clicking it navigates to the dashboard (for the currently-selected site on multi-site deployments). When already on the dashboard, it calls `loadDashboard()` for an in-place refresh instead of a full page reload, so filter state and chart range are preserved. Middle-click / ctrl-click still open in a new tab.

### Changed
- `app/api/stats.py::_summary_body` joins `PiholeInstance → Site` and returns the site id/name/slug on each per-instance dict. Optional fields; existing clients ignoring them are unaffected.

### Migration notes
- No DB migration.
- No action needed on upgrade: the Combined nav item simply appears when a deployment has ≥2 active sites. Single-site deployments are visually unchanged apart from the clickable logo (which was a no-op affordance before).

---

## [1.11.0-dev.11] — 2026-04-24

Multi-site YAML ergonomics — friendlier field name for the Main flag, optional slugs are now documented up-front, and renaming the default site through YAML is supported in-place (no orphans, no data loss).

### Added
- **`default_site: true`** in `sites:` YAML — friendly alias for `main: true`. Either (or both) flips the Main flag. `main:` stays as a silent back-compat alias so existing configs keep working.
- **Main-site rename detection** in `config_loader.sync_sites_and_instances()`. When the YAML's `default_site` entry names a slug that doesn't exist in the DB **and** the DB's current Main has a slug that isn't referenced anywhere in the new YAML, MyPi recognizes this as a rename: the existing site row is updated in place (name + slug), the old slug is written to `site_slug_history` so bookmarks keep working via 301, and all historical stats, query logs, sync schedule, and Pushover settings stay attached. No orphan Default hanging around. Runs before the normal upsert-by-slug loop so the upsert sees the slug match and treats it as an update, not a new insert.

### Changed
- **`pihole_instances.yml.example`** — multi-site block now uses `default_site: true` as the primary form, documents the rename-in-place flow, and the "Advanced" notes explain when `slug:` is worth setting explicitly. `main: true` is mentioned only as a back-compat alias.

### Migration notes
- Pulling dev.11 without changing your YAML is a no-op. All existing configs keep working.
- When you're ready to rename the Default site, edit `pihole_instances.yml` to the `sites:` format with your preferred name on the `default_site: true` entry (e.g. `name: "WTR"`), then restart the container. A log line `Main-site rename detected in YAML: 'Default' (slug=default) → 'WTR' (slug=wtr). Preserving data in place.` confirms the rename ran.
- Bookmarks to `/dashboard/default`, `/api/sites/default/...`, etc. continue working after the rename via the existing slug-history 301 path in `resolve_site`.

---

## [1.11.0-dev.10] — 2026-04-24

Bugfix — dashboard / settings / query-log pages rendered empty even though the backend was healthy. `window.siteApiUrl` was defined in a trailing `<script>` at the bottom of `base.html`, but each template's `extra_js` block runs *before* that (extra_js is injected via Jinja block, the trailing script follows it). `settings.html::extra_js` calls `loadSettingsInstances()`, `loadSyncStatus()`, etc., which reference `window.siteApiUrl(...)`. With the helper still undefined, each loader threw `TypeError: siteApiUrl is not a function` in the browser console, aborted silently, and left the UI looking like instances + sync settings were gone.

Fix: moved `window.currentSiteSlug`, `window.siteApiUrl`, and `window.currentSection` into the existing early `<script>` in `<head>` (the theme-preference block). They're now guaranteed defined before `dashboard.js` loads and before any `extra_js` runs. The trailing script at the bottom of `base.html` now only contains the site-picker population code, which legitimately needs the DOM to be ready.

Nothing else changed — the underlying site-picker logic, API routes, collector, and sync_service are all untouched. This is purely a JS load-order fix.

---

## [1.11.0-dev.9] — 2026-04-24

Bugfix — app startup failed with `AssertionError: Status code 204 must not have a response body` because `DELETE /api/sites/{slug}` was declared with both `status_code=204` *and* an explicit `-> None` return annotation. FastAPI's post-0.115 route builder interprets any return annotation (including `None`) as a response-model hint, which contradicts 204's "no body" semantics. The existing `DELETE /api/instances/{id}` handler works because it has no return annotation at all — matched that pattern.

Dropped the `-> None` annotation from `delete_orphan_site`. Migration 0013–0015 had already completed cleanly in dev.8, so the DB is in the correct multisite state — only the app import was blocked.

---

## [1.11.0-dev.8] — 2026-04-24

Bugfix — migration `0013_multisite` failed on asyncpg with `DatatypeMismatchError: column "id" is of type uuid but expression is of type character varying`. asyncpg is strict about parameter types: a Python `str` bound to a `uuid` column is rejected, unlike psycopg2 which performs an implicit cast. Two bind sites in the migration (the `INSERT INTO sites` and the `UPDATE pihole_instances` backfill) both pass a stringified UUID, so both were failing.

Fix: wrap the two bind params in explicit SQL casts — `CAST(:id AS uuid)` and `CAST(:sid AS uuid)`. Works uniformly under asyncpg, psycopg2, and psycopg3.

### Migration notes
- The failing migration ran inside a transaction that rolled back cleanly on each failure, so any DB that tried to upgrade to dev.7 is still at revision 0012 with no stale state. Pulling dev.8 and restarting the container will run 0013 → 0014 → 0015 cleanly this time.
- No schema or logic change beyond the cast. Every other behavior of migration 0013 is identical to what dev.0/dev.1 intended.

---

## [1.11.0-dev.7] — 2026-04-24

Multi-site Phase 6 — Web UI. A site picker in the topbar, bookmarkable per-site URLs (`/dashboard/{slug}`, `/queries/{slug}`, `/settings/{slug}`), dashboard JS routes site-scoped fetches through a `window.siteApiUrl()` helper, and a new Orphaned Sites cleanup section in the settings page. The picker is hidden for single-site deployments so nothing about the UI changes in that case.

### Added
- **Site picker** in `app/templates/base.html` topbar — `<select id="site-picker">` populated from `GET /api/sites` on every page load. Hidden when ≤1 active site. Changing the selection navigates to `/{currentSection}/{slug}` preserving the section (dashboard / queries / settings). Sidebar links get rewritten so clicking Dashboard/Queries/Settings preserves the selected site.
- **`window.currentSiteSlug`** (from server-side `site_slug` template var) and **`window.siteApiUrl(path)`** helper: returns `/api${path}` when no slug is set, `/api/sites/{slug}${path}` otherwise. Legacy deployments see no behavior change.
- **Web routes** `/dashboard/{slug}`, `/queries/{slug}`, `/settings/{slug}` in `app/main.py` — render the same three templates with `site_slug` in the context. Existing un-prefixed routes still work and pass `site_slug=""`.
- **Orphaned Sites** card in the settings page. Hidden unless `GET /api/sites/inactive` returns at least one row. Each row shows name, slug, instance count, and a "Remove site + data" button that calls `DELETE /api/sites/{slug}` after a confirm that spells out the cascade (instances + stats + queries + settings). Matches the existing orphan-instances pattern.
- **`loadStaleSites()`** + **`deleteOrphanSite()`** in `dashboard.js` — parallel to `loadStaleInstances` / `deleteStaleInstance` with the site-specific cascade warning.

### Changed
- **`dashboard.js`** — the 15 fetches that are site-scoped by nature (stats summary/history/top, queries list + clients, instances list, sync status/schedule/trigger) now go through `window.siteApiUrl()`. On legacy URLs this is a no-op. On per-site URLs (`/dashboard/home`, etc.) these hit the per-site API variants shipped in dev.6. Domain management, API keys, version-check, session timeout, notifications, and stale-instance management continue to use legacy routes — they're not per-site scoped in v1.
- **`base.html` sidebar links** — Dashboard/Queries/Settings get per-site href rewrites only when a site picker is visible. Single-site users keep the plain `/`, `/queries`, `/settings` URLs.
- **Sync-status badge polling** (in `base.html`) now uses `window.siteApiUrl('/sync/status')` so it reflects the selected site.

### Unchanged
- The Pushover / sync-schedule / poll-interval settings UI stays Main-only for v1. Per-site configuration works at the API layer (dev.6) but the settings form still writes to Main's `site_settings` row via the existing `/api/notifications/settings` and `/api/sync/schedule` handlers. A "Use Main's settings" checkbox / per-site form expansion can ship when real multi-site deployments ask for it.
- All legacy web URLs work verbatim. `/`, `/queries`, `/settings` are unchanged.
- dashboard.js's API-key management, version-check, domain on/off, and session-timeout fetches all stay on legacy routes. These concerns are not site-scoped in the v1 design.

### Migration notes
- No DB migration. Python + templates + JS only.
- Restart into dev.7 on a single-site deployment → pixel-identical UI. The picker renders empty and immediately hides on `GET /api/sites` returning 1.
- Multi-site deployments will see the picker, per-site bookmarkable URLs, and the orphan-sites section if any orphans exist.

---

## [1.11.0-dev.6] — 2026-04-24

Multi-site Phase 5 — per-site API surface. Every legacy un-prefixed route (`/api/stats/summary`, `/api/queries`, `/api/sync/status`, …) is unchanged and still resolves the active Main site by default. Alongside them, a parallel set of routes under `/api/sites/{slug}/…` scopes every read/action to one site. A new `/api/sites` namespace exposes site management (list, inactive, detail, rename, delete). Single-site deployments see zero API behavior change.

### Added
- **`app/api/sites.py`** — site management. `GET /api/sites` (list), `GET /api/sites/inactive` (orphans), `GET /api/sites/{slug}` (detail), `PATCH /api/sites/{slug}` (rename or slug-change; the old slug is moved to `site_slug_history` so bookmarks keep working via a 301), `DELETE /api/sites/{slug}` (orphan-only; cascades to the site's instances, stats, queries, settings). Active sites can't be deleted — must be removed from `pihole_instances.yml` first, matching the orphan-instance pattern.
- **`app/api/_site_dep.py`** — `resolve_site` FastAPI dependency. Active-slug match returns the `Site`; `site_slug_history` match returns HTTP 301 with the `Location` header pointing at the current slug; no match returns 404.
- **Per-site route variants** wired under `/api/sites/{slug}/…`:
  - Sync: `GET /status`, `GET/PUT /schedule`, `POST /` (trigger).
  - Instances: `GET /instances`, `GET /instances/stale`.
  - Stats: `GET /stats/summary`, `GET /stats/history`, `GET /stats/top`.
  - Queries: `GET /queries`, `GET /queries/clients`.
- **`app/services/pushover.py::_resolve_site_config(site_id)`** — resolves a site's effective Pushover config (credentials + alert toggles) from `site_settings` with Main-fallback inheritance.
- **`send(..., site_id=None)`** / **`notify_sync_failure(..., site_id=None)`** / **`notify_instance_offline(..., site_id=None)`** / **`notify_instance_back_online(..., site_id=None)`** — when `site_id` is given, the site's credentials + alert toggles (resolved with Main-fallback) are used; otherwise Main's in-memory config is used as before.

### Changed
- **`app/services/sync_service.py`** — `run_sync` threads the resolved `site_id` into its Pushover notify calls so per-site syncs deliver notifications via the site's credentials when configured.
- **`app/services/collector.py`** — instance-offline / back-online notify calls now pass `site_id=instance.site_id` so multi-site deployments that configure per-site Pushover credentials see the right account fire. Single-site deployments: Main's config is still used (same delivery target as before).
- **`app/api/stats.py`** — `get_summary`, `get_history`, `get_top` handler bodies factored into `_summary_body`, `_history_body`, `_top_body` helpers that accept an optional `site_id` / `site_instance_ids`. Legacy routes call with no site scope; per-site routes pass the site's active instance-id list so `QueryLog` aggregates are constrained to that site.
- **`app/api/queries.py`** — per-site `/queries` and `/queries/clients` reuse the legacy body pattern with an `instance_id IN (site's instances)` filter.
- **`app/main.py`** — registers the new `sites_router` (site management), plus the per-site sub-routers exposed by `sync`, `instances`, `stats`, `queries`. Five `include_router` lines added.

### Unchanged
- Every existing route. The legacy paths continue to serve Main by default for single-site deployments; multi-site users keep the option to target legacy URL = Main.
- Pushover in-memory Main state (`_app_token`, `_user_key`, alert toggles). Phase 5 adds per-site *resolution* for notifications but leaves the legacy Main-only settings page + API intact. Per-site Pushover settings API lands alongside the UI work in Phase 6.
- `get_offline_alert_retries` / `get_offline_alert_max_count` stay sync and Main-only — collector polls them on every tick, and threading per-site async lookups would add 2× N DB hits per poll cycle. Remaining Main-only until per-site alert tuning is a real user-facing feature.

### Migration notes
- No DB migration. All changes are Python-only.
- Single-site deployments see identical behavior: legacy routes hit Main, site CRUD is available but returns a list of one.
- Multi-site deployments now get per-site stats/queries/sync accessible via `/api/sites/{slug}/…`. Each site's sync reads/writes its own `sync_schedule` and `sync_last_result` in `site_settings` (Phase 4b wired that). Inherited Pushover credentials resolve automatically.

---

## [1.11.0-dev.5] — 2026-04-24

Multi-site Phase 4b — `sync_service` is now truly per-site. Every piece of sync state (locks, in-flight `_state`, schedule config, schedule task, blocklist-delta watermark) is keyed by `str(site_id)` in a dict. A site's `run_sync` picks its master and replicas from only that site's active instances, so two sites' syncs can run concurrently without contending on shared state. Existing single-site deployments keep working verbatim because every public function takes `site_id: uuid.UUID | None = None` and defaults to the active Main site when omitted.

### Added
- **Per-site state in `app/services/sync_service.py`** — `_state_by_site`, `_lock_by_site`, `_schedule_task_by_site`, `_last_blocklist_by_site`, `_schedule_by_site`. The old module globals (`_state`, `_lock`, `_schedule_minutes`, `_auto_gravity`, `_schedule_task`, `_last_blocklist_count`, `_sync_opts`) are gone.
- **`_resolve_site_id(site_id)`** — None-to-Main helper used by every public entry point.
- **`_lookup_site_name(site_id)`** — returns the site's name for logs and alerts.
- **Site-labeled Pushover notifications.** `notify_sync_failure(error, site_name=…)` / `notify_instance_offline(name, site_name=…)` / `notify_instance_back_online(name, site_name=…)` append a `(SiteName)` suffix so multi-site users can tell which site fired. `site_name=""` (default) yields the pre-4b message verbatim.

### Changed
- **`run_sync(..., site_id=None)`** — scopes `SELECT … FROM pihole_instances` to the target site. Master-not-configured / no-replicas errors now name the site. Per-site locks mean two sites' syncs are independent.
- **`set_schedule(..., site_id=None)`** — cancels only the target site's interval task and re-arms it under the site's new cadence. Other sites' schedules are untouched.
- **`notify_blocklist_count(site_id, count)`** — signature changed: `site_id` is now required-positional (no default). Collector's caller already threads `instance.site_id` through, so the Phase-3 `is_main_site` guard is dropped. Each site's master maintains its own watermark; auto-sync fires per-site.
- **`load_schedule()`** — iterates every active site via `Site` table, calls `_load_site_schedule(site.id, site.name)` for each. Re-arms the interval task per site.
- **`get_state()` / `get_schedule()`** — now `async` (need to resolve Main on demand). `api/sync.py` handlers now `await` them.
- **`app/services/collector.py`** — `_poll_stats_for` takes `site_name: str = ""` instead of `is_main_site: bool`. `poll_stats_for_site` looks up the site name once via `_get_site_name(site_id)` and threads it through. The Phase-3 `_is_main_site` helper is replaced by `_get_site_name`. Pushover notify calls pass `site_name=site_name`.
- **`app/services/pushover.py`** — notify helpers accept optional `site_name` and append `(site_name)` to the body via a small `_with_site` helper.
- **`app/api/sync.py`** — four call sites updated to `await` the now-async `sync_service.get_state` / `get_schedule`.

### Not yet in this release (coming in Phase 5 + 6)
- Per-site API routes (`/api/sites/{slug}/sync/...`). The existing un-prefixed routes still resolve Main by default.
- Per-site UI site picker and settings pages.
- Per-site Pushover credentials (still one global-for-Main config; messages are site-labeled but the delivery target is Main's Pushover account).

### Migration notes
- No new Alembic migration — schema is unchanged from dev.4. Behavior changes are in Python only.
- Existing deployments restarting into dev.5 see the exact same single-site behavior: `load_schedule()` restores Main's schedule, `run_sync()` (no site_id arg) syncs Main's instances. Multi-site YAML users see per-site schedules restored independently and per-site `Sync started: site=<name> master=…` log lines.

---

## [1.11.0-dev.4] — 2026-04-24

Multi-site Phase 4a — storage-layer reader migration. The three app settings backed by per-site-natured data (Pushover, poll interval, sync schedule + last result) now read and write from `site_settings` under the active Main site instead of the flat `app_settings` table. No user-visible behavior change: everything is still resolved through Main because the surrounding service logic (sync_service's single-master state, the global UI) is still single-site. The functional per-site rewrite of `sync_service` lands next in dev.5 as Phase 4b.

Phase 4 as originally planned was one commit containing the reader migration, the Alembic data move, and the `sync_service` per-site functional rewrite. That produced a ~1200-line diff across four+ files with heavy coupling; splitting into **4a (storage)** and **4b (functional)** lets each half ship against a stable base and keeps reviews tractable. Plan doc updated.

### Added
- **`alembic/versions/0015_move_settings_to_site_settings.py`** — moves four keys from `app_settings` to `site_settings` under Main: `sync_schedule`, `sync_last_result`, `pushover_settings`, `queries_poll_interval`. Uses `INSERT … SELECT … ON CONFLICT DO NOTHING` + `DELETE` to preserve any hand-written destination rows while collapsing to a single source of truth. Fresh installs see a no-op. Downgrade copies Main's values back to `app_settings` (lossy for non-Main overrides, documented).

### Changed
- **`app/services/pushover.py`** — `load_settings()` / `save_settings()` now resolve the active Main site id via `site_settings.get_main_site_id()` and read/write through `site_settings.get_setting()` / `set_setting()`. Dropped the manual `pg_insert … ON CONFLICT DO UPDATE` + fresh-session verify block in favor of the site_settings module's built-in verification. Import surface dropped `AppSetting` and `pg_insert`. Public API (function signatures, module-global readers like `get_offline_alert_retries()`) is unchanged — every existing caller works verbatim.
- **`app/services/poll_settings.py`** — same pattern. `load_settings()` / `save_settings(interval_seconds)` now go through Main's `site_settings` row. Public API unchanged.
- **`app/services/sync_service.py`** — `_db_upsert(key, value)` now writes to Main's `site_settings` row instead of `app_settings`. `load_schedule()` reads both `sync_schedule` and `sync_last_result` from Main's `site_settings`. Dropped `AppSetting` / `pg_insert` imports. `_persist_schedule()` and `_persist_sync_state()` paths unchanged in surface — they still call `_db_upsert`. `run_sync()` behavior and signature unchanged — per-site rewrite is Phase 4b.

### Migration notes
- `alembic upgrade head` runs `0015_move_settings_to_site_settings`. It's idempotent against existing data — re-running (e.g. after a manual backout) behaves correctly.
- If a user hand-wrote `site_settings` rows for Main before upgrading (unlikely but supported), those rows are preserved and the matching `app_settings` rows are deleted anyway.
- dev.4 containers restarting against a dev.3 DB perform the move on first boot. Settings persistence survives the migration untouched.

---

## [1.11.0-dev.3] — 2026-04-24

Multi-site Phase 3 — per-site polling. The collector's two global APScheduler jobs (`poll_stats`, `poll_queries`) are replaced by a pair of jobs per active site (`poll_stats_site_<id>`, `poll_queries_site_<id>`). Each site's polls iterate only that site's instances, so a hang on one site's poll tick can't delay another. Poll intervals are still globally configured for now; Phase 4 adds per-site overrides via `site_settings` when the reader migration lands.

### Added
- **`app/services/collector.py::poll_stats_for_site(site_id)`** / **`poll_queries_for_site(site_id)`** — replace the old global `poll_stats` / `poll_queries`. Each polls only the instances belonging to one site.
- **`schedule_site(scheduler, site_id, stats_interval, queries_interval)`** / **`unschedule_site(scheduler, site_id)`** / **`reschedule_all_queries_jobs(scheduler, interval)`** — scheduler-management helpers. Job ids follow the convention `poll_{kind}_site_{uuid}` so dynamic add/remove/reschedule targets the right pair without touching others.
- **`get_active_site_ids()`** — used by startup to enumerate sites needing a poll pair registered.
- **`prune_inactive_state()`** — dedicated APScheduler job running every 5 minutes. Drops per-instance module state dicts (`_last_seen_ts`, `_prev_status`, circuit breaker, offline-alert counters) for instances that have been deactivated, and evicts any leftover `client_manager` clients. The old in-poll prune logic lived inside `poll_queries` and would have been redundantly run N times per tick in a multi-site world.
- **`_is_main_site(site_id)`** helper — the collector uses it to gate the master-blocklist-delta auto-sync notification so only the Main site's master triggers a sync. `sync_service` still has global single-master state in Phase 3; allowing multiple sites' masters to notify would thrash `_last_blocklist_count`. Phase 4 rewrites sync state per-site and this guard drops.

### Changed
- **`app/main.py`** — scheduler setup iterates active sites and calls `schedule_site` for each instead of registering two fixed jobs. The `poll_settings_service` reschedule callback now re-targets every registered queries-poll job via `reschedule_all_queries_jobs`. A new 5-minute `prune_inactive_state` job replaces the old in-poll prune. Startup log message now reports site count.
- **`_poll_stats_for(instance, is_main_site=True)`** — new keyword arg guards the `sync_service.notify_blocklist_count` call. Defaults `True` for back-compat with any ad-hoc callers.

### Unchanged
- `client_manager` — clients are keyed by `instance_id`, agnostic to site. Shared between a site's stats and queries polls exactly as before.
- Circuit breaker, eviction, dedup, offline-retry/alert-count state — all still per-instance.
- Backfill and version-fetch startup tasks — still iterate all active instances globally; one-shot at startup, no per-site scheduler change needed.

### Migration notes
- No DB migration in this release.
- Deployments that restart into dev.3 will see the logger message `Scheduler started: 1 site(s), stats=60s, queries=10s, prune=5min.` (or whatever their intervals are). Polling cadence and circuit-breaker behavior are unchanged for single-site users.
- Users adopting a multi-site `sites:` YAML will see one `Polling queries for <instance>` line per site per tick in logs — one per tick per site is expected. Log volume scales linearly with site count.

---

## [1.11.0-dev.2] — 2026-04-24

Multi-site Phase 2 — YAML loading, config sync, and the `site_settings` inheritance service. No behavior change for existing deployments: the collector, sync service, APIs, and web UI still run exactly as they did in dev.0/dev.1 because the readers (pushover, poll_settings, sync_service) haven't been migrated onto `site_settings` yet — Phase 4 ships that reader rewrite plus the app_settings→site_settings data move together.

### Added
- **`sites:` YAML format** in `pihole_instances.yml` — up to 10 sites per deployment, each with up to 10 Pi-hole instances, its own master, and its own `main:` flag for inheritance. Legacy flat `instances:` YAML continues to work unchanged and is transparently wrapped into an implicit `Default` site.
- **`app/config.py::SiteConfig`** + **`slugify()`** + **`validate_slug()`** + **`load_site_configs()`** — parse both YAML shapes, derive slugs from names, enforce reserved-slug list (`sites`, `inactive`, `admin`, `main`), resolve Main designation (first `main: true` wins; if none flagged, first site becomes Main). `max_sites` setting added (default 10, per-site `max_pihole_instances` stays 10).
- **`app/services/config_loader.py::sync_sites_and_instances()`** (replaces `sync_instances`) — upserts sites by slug and instances by `(site_id, name)`, detects orphans at both layers, handles Main reassignment with settings materialization (copies old Main's `site_settings` into new Main where new Main has NULL), evicts `client_manager` clients for deactivated instances. `sync_instances` kept as an alias for compatibility.
- **`app/services/site_settings.py`** — per-site settings service with Main-fallback inheritance. `get_setting()` resolves NULL-or-missing values against the active Main; `set_setting()` / `clear_setting()` upsert with the established `pg_insert … ON CONFLICT DO UPDATE` + read-back verification pattern. `get_json_setting` / `set_json_setting` convenience wrappers for JSON-encoded values. Module is available but unwired in Phase 2 — Phase 4 migrates the existing settings readers onto it.
- **`pihole_instances.yml.example`** — documents both formats side by side with the slug/main/inheritance semantics.

### Fixed (pre-release correction to dev.0's migration `0013_multisite.py`)
- **Dropped the per-site `app_settings` backfill from migration 0013.** The draft migration filtered on `key LIKE 'sync.%'` / `'pushover.%'` / `'poll.%'`, but the actual keys in use are flat (`sync_schedule`, `sync_last_result`, `pushover_settings`, `queries_poll_interval`), so the backfill would have matched nothing and left per-site settings in `app_settings` indefinitely. Corrected here because dev.0 was never deployed. The app_settings→site_settings data move is now explicitly scoped to Phase 4, paired with the reader rewrite — see the migration plan.

### Changed
- **`app/main.py::_bootstrap`** calls `sync_sites_and_instances` (renamed from `sync_instances`). One call-site updated.

### Migration notes
- No new Alembic migration in this release. `alembic upgrade head` is still at `0014_api_key_site_scope` from dev.1.
- Existing deployments upgrading to dev.2 see the Default site (created by migration 0013 in dev.0) get upserted by slug on startup. Zero row churn.
- Deployments that want to adopt multi-site can swap their `instances:` YAML for a `sites:` YAML on restart; MyPi will create the new sites, move instances under them where the name matches, and flag the old Default site as an orphan if it's no longer referenced. Orphans stay as `is_active=FALSE` rows for manual cleanup — data is never auto-deleted.

---

## [1.11.0-dev.1] — 2026-04-24

Phase 1 addendum — forward-compatibility hook so a future per-site API-key scoping feature won't require another breaking schema migration. No user-visible change; no existing or newly-created key's behavior changes in v1. All keys continue to have access to every site on the deployment.

### Added
- **`api_keys.allowed_site_ids`** — nullable `JSONB` column. `NULL` (the default, and the only value any key has today) means "unrestricted; all sites." A populated array will mean "scoped to these site ids only" once the scoping feature is exposed in a future release. Middleware enforcement will be a one-line null-check: if `NULL`, allow; else `site_id in allowed_site_ids or 403`.

### Migration notes
- `alembic upgrade head` runs `0014_api_key_site_scope.py` — single `ADD COLUMN` on an already-small table. Fast and trivial to roll back.
- iOS design implication: the one-key-per-MyPi-server model still holds. iOS continues to auth once per server, then calls `GET /api/sites` to discover sites and lets the user pick which backend site to view. Same API key covers every site on that server. The scoping column only comes into play when a future release exposes per-site scoped keys for limited integrations.

### Implementation plan impact
- Phase 2 (config loader + Main-reassignment) ships as `1.11.0-dev.2`, not dev.1. The phase-to-version mapping in `docs/multisite-implementation-plan.md` shifts by one; the phase order is unchanged.

---

## [1.11.0-dev.0] — 2026-04-24

Multi-site foundation (Phase 1 of 6 — schema only, no behavior change). A single MyPi deployment will eventually group its Pi-holes into up to 10 sites, each with its own master, replicas, sync cadence, Pushover config, and polling intervals. This release lays the schema; existing deployments are migrated into an auto-created `Default` site flagged as Main, legacy API surfaces and YAML still work unchanged. See `docs/multisite-design.md`, `docs/multisite-migration-plan.md`, and `docs/multisite-implementation-plan.md` for the full plan.

### Added
- **`sites` table** with `id`, `name`, `slug`, `is_main`, `is_active`, `sort_order`, `created_at`. Partial unique indexes enforce exactly one active row with a given name, exactly one active row with a given slug, and exactly one active Main.
- **`site_slug_history` table** — retired slugs stay behind as permanent aliases so changing a site's slug doesn't break bookmarks or scripts.
- **`site_settings` table** with composite PK `(site_id, key)` — the new home for all per-site app settings (`sync.*`, `pushover.*`, `poll.*`). A `NULL` value means "inherit from Main."
- **`pihole_instances.site_id`** FK → `sites.id` with `ON DELETE CASCADE`.
- **`app/models/site.py`** — `Site`, `SiteSlugHistory`, `SiteSetting` ORM models with relationships + cascade.

### Changed
- **`pihole_instances.name` uniqueness** is now scoped to `(site_id, name)` instead of globally unique. Two sites can now each have e.g. a "Living Room" instance. Global `pihole_instances_name_key` dropped, replaced by `uq_pihole_instances_site_name`.
- **`PiholeInstance` ORM model** gains a required `site_id` FK and a `site` relationship.

### Migration notes
- `alembic upgrade head` runs `0013_multisite.py` in a single transaction. A fresh `Default` site (`slug='default'`) is created and flagged Main; every existing `pihole_instance` is reassigned to it; every per-site `app_settings` row (keys beginning `sync.`, `pushover.`, or `poll.`) moves to `site_settings` under that Default site. Truly-global keys stay in `app_settings`.
- Downgrade is supported but lossy: only Main's non-null settings are copied back into `app_settings`; non-Main site settings and their pihole groupings are discarded. Downgrade is a developer escape hatch, not a routine operation.
- No behavior change in this release — the app still reads/writes exactly as before because the Default/Main site transparently wraps existing data. The collector, sync service, APIs, and web UI are unchanged. Those land in subsequent `1.11.0-dev.N` releases per the implementation plan.

---

## [1.10.0] — 2026-04-23

Removed the `hot_spare` YAML flag and its Pushover-suppression plumbing shipped in 1.9.0. That feature was built for a symptom — a "VIP standby" replica flapping through sync — that a side-by-side A/B later attributed to `pihole-FTL` on a Raspberry Pi 3 wedging at the TLS handshake rather than anything hot-spare-specific. The RPi5 replacement under identical MyPi / FTL config never exhibited the flap. Suppressing notifications for a whole class of replicas was papering over an RPi3-specific FTL bug, so the flag is gone. The general-purpose sync-path retry on transient socket errors (`ssl.SSLError` / `httpx.ConnectError` / `httpx.RemoteProtocolError`) stays — it was never hot-spare-specific.

### Removed
- **`hot_spare: true` field in `pihole_instances.yml`** and its Pushover-suppression branch in `sync_service.py`. A sync failure on any replica now always pages (subject to the existing per-instance circuit breaker and offline-alert retry count).
- **`is_hot_spare` column** on `pihole_instances` (dropped via migration `0012`). Also removed from `GET /api/instances`, `GET /api/instances/stale`, and `GET /api/sync/status.results[]`.
- **`app/services/sync_service.py::InstanceSyncResult.is_hot_spare`**, all the load / persist / pushover-filter code that referenced it, and the master-vs-hot-spare branching in `app/config.py::load_instance_configs`.

### Documentation
- **README "Low-traffic or hot-standby Pi-holes" section** rewritten as **"Flapping on a Raspberry Pi 3"**. Documents the 2026-04-23 A/B that isolated the wedge to `pihole-FTL` on RPi3 hardware (kernel accepts SYN, FTL never picks up the socket), the diagnostic fingerprint (`nc -z` passes while `curl` / `httpx` time out at TLS handshake), and the recovery (`systemctl restart pihole-FTL`; migrate to Pi 4/5 for a durable fix).
- `pihole_instances.yml.example` — removed the dormant-standby block and the `hot_spare:` field documentation.

### Migration notes
- `alembic upgrade head` runs `0012_drop_hot_spare`, which drops the `is_hot_spare` column. No data loss beyond the flag itself — the column only recorded configuration state, not observations.
- If you had `hot_spare: true` in `pihole_instances.yml`, remove it before restarting the container (leaving it won't break anything; MyPi ignores unknown YAML keys, but it's dead config).

---

## [1.9.4] — 2026-04-23

Diagnostic: the collector's poll-failure WARN logged the exception's `str()` but not its type. `httpx.ConnectError()` and similar bare exceptions produce an empty string, so operators saw `Failed to poll stats for pihole4:` with no way to distinguish `ConnectError` from `ReadError` from `SSLError` from `RemoteProtocolError`. This was blocking diagnosis of intermittent wedges on an old Raspberry Pi 3 test target whose hardware telemetry was otherwise clean (no under-voltage, no SD I/O errors, no NIC counter errors, no memory pressure).

### Changed
- **`app/services/collector.py`** — both poll-failure WARN lines now include `type(exc).__name__` alongside the exception message. Matches the diagnostic pattern established in `pihole_client._authenticate` (dev.12, 1.8.0). The next occurrence will log e.g. `Failed to poll stats for pihole4: ConnectError:` instead of the current `Failed to poll stats for pihole4:`, making the exception class visible even when `str(exc)` is empty.

### Migration notes
- None. Log-message-only change.

---

## [1.9.3] — 2026-04-21

Bugfix: the dashboard "Queries over time" chart silently hid outages. Periods with zero DNS queries for every range (`Today`, `Last 24 h`, `Last 48 h`, `Last 7 days`, `Last 30 days`, ad-hoc) were dropped from the response entirely, so Chart.js drew the series as if the outage never happened.

### Fixed
- **`/api/stats/history` now returns every bucket in the requested range, not just buckets that had queries.** The old query did `SELECT date_bin(...) AS bucket, count(*) FROM query_log WHERE timestamp >= since GROUP BY bucket` — any bucket with zero rows was simply absent from the result set. A 3-hour outage across 30-minute buckets produced 6 missing rows, and the bar chart rendered as if those three hours never existed. Rebuilt the query as a `generate_series(date_bin(...start), date_bin(...end), interval)` of every expected bucket, `LEFT JOIN`ed against the aggregated counts, with `COALESCE(..., 0)` on the query/blocked columns. Outages now render as empty bars on the x-axis — reflecting what actually happened rather than smoothing the chart to accommodate missing data. File: `app/api/stats.py`.

### Migration notes
- None. No schema change, no config change. The API response shape is unchanged — existing clients just receive additional zero-valued buckets where they previously got nothing.

---

## [1.9.2] — 2026-04-20

Bugfix: the dashboard "Queries over time" chart rendered incorrectly for every time range past 24 hours. Two issues compounded.

### Fixed
- **`/api/stats/history` bucketing silently ignored strides >60 minutes.** The old bucket expression was `date_trunc("hour", timestamp) + make_interval(mins => floor(extract('minute', timestamp) / bucket_minutes) * bucket_minutes)`. For any `bucket_minutes` between 61 and 1440, `floor(minute / bucket_minutes)` is always 0, so buckets collapsed back to hourly — 48 bars for 48 h, 168 bars for 7 d, 720 bars for 30 d. Replaced with PostgreSQL `date_bin('N minutes', timestamp, epoch)` (PG 14+, so safe on the required PG 18), which honours the full stride and produces correctly-aligned bucket boundaries. File: `app/api/stats.py`.
- **Dashboard chart bucket sizing + labels now match the selected range.** `getTimeParams` in `app/static/js/dashboard.js` now requests 2-hour buckets for `Last 48 hours` (24 bars) and 1-day buckets for `Last 7 days` / `Last 30 days` (7 / 30 bars). Added `fmtChartLabel`: daily buckets show `MMM DD`, sub-daily buckets on a multi-day window show `M/D HH:MM`, intraday buckets keep the existing `HH:MM`. Fixes the "labels wrap around with no date context" complaint on any range past 24 h.

### Migration notes
- None. No schema change, no config change. Clear browser cache or hard-refresh the dashboard after deploying so the updated `dashboard.js` loads.

---

## [1.9.1] — 2026-04-20

Follow-up to 1.9.0 after independent reviews from Gemini and Grok. Both converged on removing the dead `alert_no_logs` wiring; they diverged on proactive connection recycling, which is deliberately **not** shipped here (see note below). No behaviour change beyond the dead-UI removal and doc polish.

### Removed
- **`alert_no_logs` / `no_logs_minutes` toggle and threshold.** The UI checkbox, threshold input, request schema fields, `pushover` module state, and `load_settings` / `save_settings` plumbing all advertised an alert that was never implemented — no code path ever called `notify_no_logs`. An operator toggling it on got zero coverage. Dropped cleanly:
  - `app/templates/settings.html` — checkbox + minutes input removed
  - `app/static/js/dashboard.js` — `po-alert-no-logs` / `po-no-logs-minutes` getters/setters removed from the settings form
  - `app/api/notifications.py::PushoverSettingsRequest` — fields removed
  - `app/services/pushover.py` — module state, `load_settings`, `save_settings`, `get_settings`, `get_settings_raw` no longer reference them. Legacy persisted keys in `app_settings.pushover_settings` JSON are silently ignored on load and drop out on next save — no migration required.

### Documentation
- **README: circuit-breaker tuning profiles** — added an "Aggressive / Stable / Lenient" table under the env-var reference per Gemini's recommendation, and a note that the knobs do **not** auto-scale with poll interval (transport instability isn't proportional to how often you check).
- **README: `max_keepalive_connections=2` is load-bearing** — explicit callout under "Low-traffic or hot-standby Pi-holes" explaining why the 1.7.6 keepalive=0 path was reverted (RPi3 + civetweb + mbedTLS handshake churn wedged FTL's TLS session table). Future maintainers reading this section need the context before they're tempted to force-close keepalive again.

### Deferred
- **Proactive client-recycle** (either Gemini's inline reap-on-use or Grok's APScheduler recycle job). Both were pitched as the "cleaner" structural fix for the residual `pihole3` idle-close WARN. Held in the "next try" bucket: the sync-path flap that motivated 1.9.0 is already fixed, the remaining symptom is cosmetic (dev.12 explicit acceptance), and either form of proactive recycling increases TLS handshake count on healthy instances — measurably worse than today's keepalive-stays-warm steady state on `pihole1` / `pihole2`. If the WARN ever escalates beyond log noise, the preferred shape is last-successful-use-aged eviction in `client_manager`, not wall-clock-aged or background-scheduled.

---

## [1.9.0] — 2026-04-20

Targets the "dormant VIP secondary flap" reported under the 1.8.x soak: a Pi-hole sitting behind a shared VIP as a hot standby receives no DNS traffic while the primary holds the address. Its persistent keepalive socket to MyPi therefore sits idle between 60 s polls, and CivetWeb closes idle keepalive TCP connections on a short timeout. The stats/queries collector already self-heals that symptom by evicting the client and retrying on the next tick; **the sync path did not** — a single half-open socket during sync failed the replica outright and fired a generic Pushover.

### Added
- **`hot_spare: true` field in `pihole_instances.yml`.** Marks a replica as a dormant VIP secondary. If **all** failing replicas in a sync are hot spares, the Pushover is suppressed; the result is still recorded in `sync_last_result` and shown in the UI. If any non-hot-spare replica fails in the same sync, Pushover fires with the full list (including any hot-spare failures) so the operator sees the complete picture. The flag does **not** change the stats-poll offline alert — a hot spare is expected to be online, so a genuine outage still pages. Cannot be combined with `master: true` (logged + ignored).
- **`is_hot_spare` column** in `pihole_instances` (migration `0011`). Also surfaced on `GET /api/instances`, `GET /api/instances/stale`, and each entry of `GET /api/sync/status.results[]` so downstream UI / the iOS app can show which nodes are dormant standbys.

### Changed
- **Sync replicas now retry once on transient connection errors.** `_sync_replica` catches `ssl.SSLError` / `httpx.ConnectError` / `httpx.RemoteProtocolError` on `post_teleporter`, evicts the persistent client via `close_client`, and retries with a fresh connection — matching the self-heal the stats/queries collector already did. This removes the dominant flap source for low-traffic replicas. Other error classes (auth, HTTP 4xx/5xx, post-retry failures) still report `error` for that replica as before.
- **Sync-failure Pushover body now names the failing replicas.** Previous releases sent the generic "One or more replicas failed to sync"; the notification body now reads `pihole-spare: ConnectError(...); pihole3: RemoteProtocolError(...)` — one `{name}: {error}` per failure, semicolon-separated. Master-level failures (no teleporter exported, no master configured) still surface the top-level error message.

### Migration notes
- `alembic upgrade head` adds the `is_hot_spare` column with `server_default=false`, so existing rows are backfilled as non-hot-spare. No behaviour change until an operator sets `hot_spare: true` in `pihole_instances.yml` and restarts the container.

---

## [1.8.1] — 2026-04-20

First Dependabot sweep after activating `.github/dependabot.yml` in 1.8.0. No code changes — all bumps came in as Dependabot PRs, each CI-green before merge. Higher-risk majors (fastapi, bcrypt, cryptography, uvicorn, python 3.14) are held open as PRs for separate review.

### Changed

- **Python dependencies (`requirements.txt`):**
  - `pydantic` 2.10.3 → 2.13.3
  - `pydantic-settings` 2.7.0 → 2.14.0
  - `sqlalchemy` 2.0.36 → 2.0.49
  - `alembic` 1.14.0 → 1.18.4
  - `asyncpg` 0.30.0 → 0.31.0
  - `python-jose` 3.3.0 → 3.5.0
  - `python-multipart` 0.0.20 → 0.0.26
  - `jinja2` 3.1.4 → 3.1.6
  - `pyyaml` 6.0.2 → 6.0.3
- **GitHub Actions (`.github/workflows/docker-publish.yml`):**
  - `actions/checkout` 4.2.2 → 6.0.2
  - `docker/setup-buildx-action` 3.10.0 → 4.0.0
  - `docker/build-push-action` 6.15.0 → 7.1.0
  - `docker/login-action` 3.3.0 → 4.1.0
  - `docker/metadata-action` 5.6.1 → 6.0.0

---

## [1.8.0] — 2026-04-20

1.8.0 closes the `hardening-review` branch. The `dev.1` … `dev.16` entries below capture the full per-iteration detail; this top-level summary is what changed versus **1.7.6**.

### Added
- **Read-only API keys** — `api_keys.is_read_only` column (migration `0009`); `require_mutation` dependency rejects read-only keys from every mutation endpoint with `403`.
- **Security headers middleware** — `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: same-origin`, Content-Security-Policy, and `Strict-Transport-Security` (when `SECURE_COOKIES=true`).
- **Rate limits on mutation endpoints** — `/api/sync` (10/min), `/api/domains/{deny,allow}` (30/min each), `/api/notifications/test` (5/min), `/api/notifications/validate` (10/min), `/api/auth/change-password` (5/min).
- **Audit logging on every mutation handler** — `user=<username>` + action + target, across domains, auth, sync, instances, notifications, and poll settings.
- **Pushover credentials encrypted at rest** using the same Fernet key already protecting Pi-hole API passwords; legacy plaintext rows detected and transparently re-encrypted on next save.
- **Optional split secrets** — `JWT_SECRET_KEY` and `API_KEY_SALT` let operators rotate JWT signing or API key HMAC independently; both fall back to `SECRET_KEY`.
- **Teleporter ZIP validation** — master's export is CRC-checked and sanity-validated client-side before any replica receives it (Pi-hole v6 has no server-side staging — a bad export would otherwise overwrite every replica).
- **Per-instance circuit breaker** — a flap-prone Pi-hole is suspended for a cooldown after N consecutive connection failures; one probe poll closes it on success or re-arms it on failure. Stats and queries share breaker state keyed by instance id. Tunable via `CIRCUIT_FAIL_THRESHOLD` / `CIRCUIT_COOLDOWN_SECONDS` / `CIRCUIT_DEDUP_SECONDS`; `AUTH_BACKOFF_SECONDS` exposes the 429-retry cooldown.
- **Clean session logout** — `PiholeClient.close(logout=True)` now issues `DELETE /api/auth` on shutdown / instance removal so SID slots are released immediately (prevents `webserver.api.max_sessions` saturation across repeated restarts).
- **Periodic backoff-reminder WARN** once per 60 s while the auth backoff is active, so operators can see *why* an instance isn't reauthenticating.
- **API-key traffic tagged in the log** — every request authenticated via `X-API-Key` emits one INFO line naming the key; cookie/Bearer traffic is silent.
- **Dark mode on `/docs`** (Swagger UI) — follows the dashboard theme live, including system-preference changes.
- **Dependabot** on a weekly schedule covering `pip`, `github-actions`, and `docker`.
- **Dockerfile hardening** — image runs as unprivileged user `app` (UID 1000); `HEALTHCHECK` against `/api/health` via `curl`.

### Changed
- **`ENABLE_API_DOCS` default flipped to `false`** — fail-closed. Set `ENABLE_API_DOCS=true` in `.env` to re-enable `/docs`, `/redoc`, and `/openapi.json` together (e.g. for regenerating an iOS OpenAPI client).
- **HTTP keepalive re-enabled on the Pi-hole client** (`max_keepalive_connections=2`) — the 1.7.6 `keepalive=0` override was redundant with the self-healing eviction path and expensive on slow hardware (2 TLS handshakes/min/instance wedged FTL's civetweb TLS stack periodically on a Raspberry Pi 3).
- **Background tasks tracked** — every `asyncio.create_task` spawned from `main.py`, `sync_service.py`, and `collector.py` is now stashed in a module-level set so uncaught exceptions log instead of vanishing, and schedule loops can be cancelled and replaced cleanly.
- **Circuit-breaker + auth-backoff tunables exposed as environment variables** so operators can retune a flap-prone Pi-hole without rebuilding.

### Fixed
- `/openapi.json` 500 caused by `from __future__ import annotations` + slowapi wrappers in `app/api/domains.py`.
- `/api/auth/logout` Bearer-token revocation — the `Authorization` param was declared as `Cookie` instead of `Header`, so Bearer logouts never added the JTI to `revoked_tokens`.
- Collector dict leak — `_prev_status`, `_offline_retry_count`, `_offline_alert_count` now pruned alongside `_last_seen_ts` on instance deactivation.
- `_readonly_flag` ContextVar reset race in `app/auth.py::get_current_user`.
- `YAML load failures` no longer crash startup — a malformed `pihole_instances.yml` now starts the app with no instances instead of erroring before lifespan runs.
- `api_keys.user_id` FK now has `ON DELETE CASCADE` at the DB layer (migration `0010`).
- Persistent Pi-hole clients are evicted when an instance is removed from the YAML.
- Sync service no longer leaks untracked scheduled-loop or auto-sync tasks; schedule reloads cancel and replace cleanly.
- `pushover.save_settings` no longer updates in-memory state before DB verify.
- Sidebar "API Docs" link hidden when `ENABLE_API_DOCS=false` (dev.7 missed the persistent nav entry).
- `poll_settings.save_poll_settings` no longer echoes raw exceptions (schema/column leak vector).
- App lifespan now disposes the asyncpg engine on shutdown so the connection pool is released cleanly.
- Eviction-triggered `DELETE /api/auth` regression from dev.14 fixed in dev.15 — `close()` now takes a `logout` kwarg and only truly-done paths pass `logout=True`.

### Removed
- Dead code: `PiholeClient.get_top_stats`, `PiholeClient.get_history`, `PiholeTopStats`, `Settings.validate_encryption_key_at_startup`, and the `load_schedule` startup diagnostic that re-wrote `app_settings` on every boot.

### Independent reviews

1.8.0 incorporates fixes identified by two external LLM-based audits — **Google Gemini** (adversarial security review) and **Grok** (full architecture + security review). See README → *Independent reviews* for the items verified as already-correct and the items changed in response.

### Known, accepted behaviour

- **Low-traffic / hot-standby Pi-holes may occasionally flap.** Pi-hole v6's CivetWeb closes idle keepalive sockets on a short timeout that is not exposed in the Pi-hole UI or `.toml`. The circuit breaker absorbs the flap locally; if the 5-minute cooldown crosses the offline-alert threshold, raise the offline-alert retry count in Settings → Notifications. See README → *Low-traffic or hot-standby Pi-holes*.

---

## [1.8.0-dev.16] — 2026-04-20

### Changed

- **Circuit-breaker and auth-backoff tunables are now environment variables (`app/config.py`, `app/services/collector.py`, `app/services/pihole_client.py`, `.env.example`).** Previously `_CIRCUIT_FAIL_THRESHOLD` (3), `_CIRCUIT_COOLDOWN` (300 s), `_CIRCUIT_DEDUP_WINDOW` (2 s), and `AUTH_BACKOFF_SECONDS` (300 s) were module-level constants — operators had to rebuild the image to retune a flap-prone Pi-hole. They're now `CIRCUIT_FAIL_THRESHOLD`, `CIRCUIT_COOLDOWN_SECONDS`, `CIRCUIT_DEDUP_SECONDS`, and `AUTH_BACKOFF_SECONDS` on `Settings`, loaded from `.env` via pydantic-settings. Defaults are identical to the previous hard-coded values so behaviour is unchanged unless an override is set. Motivated by Grok's dev.15 audit — zero new behaviour, pure knob exposure.

---

## [1.8.0-dev.15] — 2026-04-20

### Fixed

- **Stop issuing `DELETE /api/auth` on connection-error evictions (`app/services/pihole_client.py::close`, `app/services/client_manager.py::close_client`).** dev.14 made `PiholeClient.close()` unconditionally issue `DELETE /api/auth` before tearing down httpx. That was the right fix for the shutdown path (release Pi-hole session slots immediately) but it also fired on the collector's connection-error eviction path — and eviction there fires once per minute on pihole3's flap-prone keepalive. Each eviction destroyed a SID that was still valid on Pi-hole's side, so the next poll would come back, restore the SID from DB, get a `401`, and have to re-auth. Logs at 15:26–15:28 showed the pattern clearly: eviction → DELETE → restored SID → `401` → `POST /api/auth` → `200`, every minute. `close()` now takes a keyword-only `logout` flag (default `False`). Only the shutdown (`close_all_clients`), config-loader removal (`config_loader.py`), and active-set pruning (`collector.py` watermark cleanup) paths pass `logout=True`. Connection-error evictions in `poll_stats`/`poll_queries` keep the default — drop the broken TCP connection and the stale local state, but leave the SID intact for the next poll to reuse. The logout path also now clears the persisted SID in the DB (`save_sid(..., None)`) so a later startup doesn't try a known-dead one.

---

## [1.8.0-dev.14] — 2026-04-20

### Fixed

- **Clean session logout on client shutdown (`app/services/pihole_client.py::close`).** `PiholeClient.close()` now issues `DELETE /api/auth` with the current SID before tearing down the httpx client, so the session slot is released back to FTL immediately. Previously a MyPi restart (or any `client_manager.close_client` path) walked away silently and left the SID to expire on its own inactivity timeout. Across repeated restarts this accumulated stale sessions against Pi-hole's `webserver.api.max_sessions` (default 16) and eventually saturated the limit — which is exactly how a second MyPi deployment hit a `429 Too Many Requests` from its master (`mnpihole1`) mid-sync today and then stayed stuck in the auth backoff for the next 5 minutes. The DELETE is best-effort (5 s timeout, wrapped in try/except) — if the Pi-hole is already gone we still proceed with the local teardown.

### Added

- **Periodic backoff-reminder WARN while auth is in cooldown (`app/services/pihole_client.py::_authenticate`).** When a 429 triggers the 300-second `AUTH_BACKOFF_SECONDS` window, subsequent `_authenticate` calls are silently skipped — operators joining the log mid-window only saw the generic downstream `Authentication failed` and had no visibility into *why*. Now the first skip in each 60-second slice emits a WARN naming the instance and the remaining cooldown seconds (`Auth to <url> is in cooldown — Ns remaining before next attempt`). Still rate-limited to one WARN per minute so a stuck instance doesn't flood the log. Motivated by the same mnpihole1 incident — once the 429 landed, 5 minutes of downstream failures gave no hint that the client itself was deliberately not retrying.

---

## [1.8.0-dev.13] — 2026-04-20

### Added

- **API-key traffic is now tagged in the log (`app/auth.py::get_current_user`).** Every request that authenticates via `X-API-Key` emits one INFO line naming the key — e.g. `api-key "ios-beta" → GET /api/stats/summary`. Web-UI requests (session cookie) and Bearer JWT requests produce no extra log line, so the signal is bounded to automation traffic. Motivated by the mypi-ios companion app entering beta: being able to tell iOS/automation calls apart from browser calls without grepping headers makes debugging the mobile client considerably less painful.

---

## [1.8.0-dev.12] — 2026-04-20

### Changed

- **Reverted the dev.11 channel split in `app/services/client_manager.py` — back to one `PiholeClient` per instance.** The channel-per-poll-type design fixed dev.10's breaker double-count but introduced a worse failure on slow hardware: every dev.11 eviction triggered *two* simultaneous `POST /api/auth` calls (stats + queries) against the same FTL. pihole3's Raspberry Pi 3 couldn't serve two concurrent argon2 password verifications + TLS handshakes — both auths failed, both channels stayed in an "Authentication failed" loop, and the instance flapped within ~30 minutes of a fresh RPi reboot. pihole1/pihole2 tolerated the auth storm on faster hardware; pihole3 did not. One session per instance (dev.10 structure) is what actually worked on this hardware.

### Added

- **Circuit-breaker failure dedup in `app/services/collector.py` (`_CIRCUIT_DEDUP_WINDOW = 2.0` seconds).** This is the 5-line fix for the original dev.10 problem the channel split was trying to solve. Stats and queries are scheduled concurrently and share the persistent TCP/TLS connection — when it goes bad both polls fail in the same tick. `_breaker_failure` now treats any failure within 2 s of the last counted failure as the same underlying event and doesn't increment the counter. The breaker still trips on three genuinely independent failure events, just not on the same connection dying once. Without this, pihole1 was flapping under dev.10 because a single transient idle-close counted as 2 toward the threshold of 3.

### Fixed

- **Auth failures are now logged at WARNING with the actual cause (`app/services/pihole_client.py::_authenticate`).** Previously a 4xx/5xx auth response was silently dropped and an httpx transport error (SSL handshake, timeout, connection reset during auth) was logged at DEBUG — operators only ever saw the generic downstream `ConnectionError: Authentication failed for <url>`. Now each branch logs the HTTP status + response snippet or the exception type + message, so the next time pihole3 wedges we'll see *why* auth failed instead of having to guess. This is how we would have diagnosed the dev.11 auth storm in minutes instead of an hour.

---

## [1.8.0-dev.11] — 2026-04-20

### Fixed

- **Decoupled stats and queries polling onto independent PiholeClient channels (`app/services/client_manager.py`, `app/services/collector.py`).** dev.10 re-enabled HTTP keepalive, which fixed pihole3's RPi3 wedge but coupled stats and queries onto the same TCP/TLS connection. When that single connection went bad — a civetweb/FTL idle-timeout, a brief network blip — *both* poll paths failed in the same tick, double-counting the per-instance circuit breaker (visible in the dev.10 logs as breaker trips firing at `(3 consecutive failures)` and `(4 consecutive failures)` within the same millisecond). That regression caused pihole1, which had been healthy for weeks, to start flapping.

  `client_manager.get_client` now accepts a `channel` argument and keys its registry by `(instance_id, channel)`. The collector opens separate `stats` and `queries` channels per instance; sync, domain lookups, version checks, and backfill continue to share the `default` channel. The eviction path closes only the failing channel, and the breaker per-instance counter only advances when both paths are failing — which is exactly the signal the breaker is meant to detect. Only the `default` channel restores/persists `pihole_instances.session_sid` to the DB; stats/queries channels auth fresh on first use so they don't race each other to overwrite the canonical SID.

  Net effect: a single bad connection can only take out one poll type; the other keeps polling and its success resets the breaker. Transient blips stop being logged as flaps, and the keepalive benefit from dev.10 is preserved for pihole3.

- **`client_manager.close_client(instance_id, channel=...)` now closes a single channel**; new `client_manager.close_instance(instance_id)` closes all channels for an instance (used by `config_loader` on instance removal and by the `poll_queries` deactivation-prune path).

---

## [1.8.0-dev.10] — 2026-04-20

### Fixed

- **Re-enabled HTTP keepalive on the Pi-hole client (`app/services/pihole_client.py::open`).** The 1.7.6 hardening shipped `max_keepalive_connections=0`, which forced a fresh TCP + TLS handshake on every request. That was belt-and-suspenders on top of the self-healing eviction path in `app/services/collector.py` (which already swaps a dead client on `ssl.SSLError | ConnectError | RemoteProtocolError`). On slow hardware — a Raspberry Pi 3 running Pi-hole/FTL — the resulting 2 TLS handshakes per minute per instance periodically wedged FTL's civetweb TLS stack and produced the `SSLV3_ALERT_HANDSHAKE_FAILURE` recurrence the dev.8 circuit breaker was added to absorb. Bumped the limit to `max_keepalive_connections=2` so the persistent PiholeClient reuses its TCP/TLS connection across polls; the eviction path and the dev.8 breaker remain as safety nets if a connection does go bad.

  **Why this is the right fix and not a regression:** the original 1.7.6 bug was half-open connections accumulating; the eviction-on-error path shipped in the *same* commit is what actually closes that loop. The `keepalive=0` override was redundant, and in this soak window we learned it has a real cost on slow targets.

---

## [1.8.0-dev.9] — 2026-04-20

### Fixed

- **Sidebar "API Docs" link no longer renders when `ENABLE_API_DOCS=false`.** The dev.7 gating covered the Settings page card but missed the persistent nav link in `app/templates/base.html`, so every authenticated page still showed a sidebar entry that 404'd on click. Now wrapped in `{% if enable_api_docs %}` — matches the Jinja global already wired in `app/main.py` and the existing gate in `app/templates/settings.html`.

---

## [1.8.0-dev.8] — 2026-04-20

Reliability fix for slow/flaky Pi-hole instances observed during the 1.8.0 soak.

### Added

- **Per-instance circuit breaker in `app/services/collector.py`.** After 3 consecutive SSL/connection failures against a single Pi-hole, polling for that instance is suspended for 5 minutes; the first poll after cooldown is a probe that either closes the breaker on success or re-arms it on failure. Stats and queries share the same breaker state keyed by instance id (they hit the same FTL). The breaker only gates the network call — stats still writes an `offline` `StatsSnapshot` while the breaker is open, so the UI stays truthful and the existing Pushover retry-then-alert flow continues to work unchanged.

  **Why:** the 1.7.6 self-healing eviction + `max_keepalive_connections=0` fix stops MyPi from accumulating half-open connections, but it cannot un-wedge a Pi-hole's FTL TLS session table once FTL itself gets stuck (reproduces periodically on a Raspberry Pi 3). Hammering at the normal cadence only prolongs the wedge; cooldowns give FTL room to recycle state and dramatically reduce the log-flood + online/offline flapping in the UI. Thresholds (`_CIRCUIT_FAIL_THRESHOLD = 3`, `_CIRCUIT_COOLDOWN = 5 min`) are module-level constants for now — can be promoted to env vars later if tuning proves necessary during soak.

  Wired alongside the existing client-eviction path, pruned in `poll_queries()` when an instance is deactivated, and cleared in `shutdown()`.

---

## [1.8.0-dev.7] — 2026-04-19

Pre-1.8.0 stabilisation pass. Single behaviour change ahead of the soak window.

### Changed

- **`ENABLE_API_DOCS` default flipped to `false`.** Recommended by Grok in the post-dev.6 review pass — fail-closed posture for the upcoming 1.8.0 stable. Setting `ENABLE_API_DOCS=true` in `.env` re-enables `/docs` (Swagger UI), `/redoc`, and `/openapi.json` as a unit; leaving it unset gives all three a 404. Wired in `app/config.py`, `app/main.py` (both `openapi_url=` and `redoc_url=` are now gated — previously `redoc_url` defaulted to `/redoc` and would have served a broken page once the schema endpoint was disabled), `app/templates/settings.html` (the API card hides the `/docs` and `/redoc` links and shows a hint about the env var instead), `.env.example`, and the `README.md` env-var table + REST API section.

  **Upgrade note for anyone driving the iOS OpenAPI client off this server:** add `ENABLE_API_DOCS=true` to your `.env` before pulling 1.8.0-dev.7 — the schema endpoint will otherwise 404 and client regeneration will fail.

---

## [1.8.0-dev.6] — 2026-04-19

Second pass of the Gemini adversarial review across `auth.py`, `config.py`, `main.py`, the `models/`, `services/`, and `api/` modules. Most findings were already addressed by earlier hardening work; this batch closes the 15 items that were both real and worth fixing. Items where Gemini was schema-blind, where the actual code already did more than the proposed fix, or where the suggestion was speculative future scope were rejected.

### Added

- **Optional split secrets — `JWT_SECRET_KEY` and `API_KEY_SALT`.** Both default to empty; when empty they fall back to `SECRET_KEY`, preserving current behaviour. Operators who want to rotate JWT signing keys without invalidating issued API keys (or vice versa) can now do so by setting the relevant variable. Wired in `app/config.py`, `app/auth.py` (`_jwt_key()` / `_api_key_salt()` helpers), and `.env.example`.
- **Audit logging on every mutation endpoint.** Each mutation handler now logs `user=<username>` plus the action and target so the standard application log doubles as an audit trail. Covers `app/api/domains.py` (allow/deny add+remove on all instances), `app/api/auth.py` (API key create+revoke, change-password, set session timeout), `app/api/sync.py` (manual sync trigger + schedule update), `app/api/instances.py` (delete stale instance), `app/api/notifications.py` (Pushover settings save + test send), `app/api/poll_settings.py` (poll interval set).
- **Pushover credentials encrypted at rest.** `app/services/pushover.py` now Fernet-encrypts `app_token` and `user_key` before persisting them to `app_settings`, using the same `_get_fernet()` key already used for `pihole_instances.api_password`. Legacy plaintext rows are detected via `InvalidToken` on load and transparently re-encrypted on the next save (no migration required).
- **Alembic migration `0010_api_key_user_cascade.py`.** Adds `ON DELETE CASCADE` to the `api_keys.user_id` foreign key. The ORM-side relationship already declared `cascade="all, delete-orphan"`, so the in-process behaviour is unchanged — but a direct DB-level user delete (psql, data-fix script) would previously have failed with a FK violation. Schema now matches ORM intent.

### Fixed

- **`_readonly_flag` ContextVar reset race in `app/auth.py::get_current_user`.** The flag was reset to `False` at the *top* of the function, which meant a nested resolution from `get_current_user_optional` (when both deps are wired into the same request) could stomp the parent call's value if it ran second. Now resolved exactly once, at the *end*, based on which auth method actually authenticated the principal — eliminates the cross-call interference.
- **Unmasked `ENCRYPTION_KEY` no longer logged.** When `_ensure_encryption_key` auto-generates a new key (no `ENCRYPTION_KEY` env var, no row in `app_settings`), the warning that prompts the operator to pin it in `.env` no longer includes the raw key in the log line. Operators now read the key from the `app_settings` table instead.
- **YAML load failures no longer crash startup.** `app/config.py::load_instance_configs` wraps `yaml.safe_load(f)` (and the underlying file open) in a `try/except (OSError, yaml.YAMLError)`, logs a warning, and returns an empty list. A malformed `pihole_instances.yml` now starts the app with no instances configured rather than erroring out before the lifespan even runs.
- **`api_keys.user_id` foreign key now has `ON DELETE CASCADE`** at the DB layer (model + migration `0010` above).
- **Persistent Pi-hole clients are evicted when an instance is deactivated.** `app/services/config_loader.py::sync_instances` now collects the IDs of every instance it deactivates (removed from `pihole_instances.yml`) and calls `client_manager.close_client(...)` for each after commit. Previously the client_manager would keep an open authenticated session against a Pi-hole that was no longer part of the active set.
- **`app/services/sync_service.py`: untracked startup `asyncio.create_task`.** `load_schedule()` re-armed the `_scheduled_loop(...)` after a restart but discarded the returned task. The bare task reference could be garbage-collected mid-sleep, AND a subsequent `set_schedule` user action could not cancel it (it would arm a *second* loop alongside the first). Now stashed in the module-level `_schedule_task` so `set_schedule` can cancel-and-replace it.
- **`app/services/sync_service.py`: untracked `asyncio.create_task` in `notify_blocklist_count` auto-sync.** The blocklist-changed auto-sync trigger called `asyncio.create_task(run_sync(...))` and discarded the task; any uncaught exception inside `run_sync` was therefore silently swallowed. Replaced with `_spawn(...)` so failures get logged via the existing `_done` callback.
- **`app/services/pushover.py::save_settings` updated in-memory state *before* DB verification.** If the verify-write read-back failed (DB drift, transient error, schema mismatch), the in-memory globals had already been overwritten with the incoming values while the persisted row still held the previous values — the next `load_settings()` would then silently flip the in-memory state back. The global assignment is now strictly *after* the verify block, so a verify failure raises before any in-memory drift can occur.
- **`app/api/poll_settings.py::save_poll_settings` no longer echoes the raw exception.** The 500 response previously included `f"Failed to save poll settings: {exc}"`, which can leak DB schema names, column names, or SQL fragments to the caller. Logged via `logger.exception(...)` for the operator and a generic detail returned to the client (matches the pattern already used in `app/api/sync.py` for `set_schedule`).
- **`app/main.py` lifespan shutdown leaves no DB connections dangling.** After the scheduler stops and the collector shuts down, the lifespan now waits up to 5 s for any tracked background tasks to finish, then calls `engine.dispose()` to release the asyncpg pool back to PostgreSQL instead of relying on TCP timeouts after the worker exits.
- **Optional startup loads now soft-fail instead of taking the app down.** A new `_soft_load(name, coro)` wrapper in `app/main.py` guards `pushover_service.load_settings()`, `version_check_service.load_settings()`, and `pihole_version_check_service.load_settings()` — all three are non-critical (services degrade gracefully without their persisted state), so a transient DB hiccup at boot now logs a warning and continues. Hard-required loads (`sync_service.load_schedule`, `session_settings.load_settings`, `poll_settings_service.load_settings`) deliberately stay outside the wrapper so misconfiguration is still loud.

### Audit items that did **not** require changes

- **API-key `last_used_at` commit on every authenticated request** — kept as a commit (not a flush). The audit-trail value of having `last_used_at` survive a request failure outweighs the per-request commit cost on this workload (sub-1 rps web UI + low-frequency iOS polling).
- **Legacy SHA-256 → HMAC-SHA256 transparent upgrade** — race claim was a false positive. The upgrade path is idempotent: two concurrent requests with the same legacy key would both attempt to set the same final hash and `key_hash_algo`, no conflict.
- **Backfill loop in `app/services/collector.py::backfill_queries_for`** — Gemini's "fix" was strictly less capable than the existing implementation, which already does hourly window splitting + paginated reads up to 20 × 10k pages per window with a no-progress guard. Gemini was working from a truncated paste.
- **Missing Alembic migrations / `RevokedToken` cleanup / `ON DELETE CASCADE` on `pihole_instances` / compound indexes on `query_logs` and `stats_snapshots` / TOCTOU race on `/api/sync` POST** — all already implemented (migrations 0001–0009; cleanup at `collector.py:399-401`; cascades at `models/pihole.py:94`/`:119`; compound indexes at `models/pihole.py:110`/`:135`; `asyncio.Lock` check at `sync_service.py:269-272`).

### Internal

- `VERSION` → `1.8.0-dev.6`. README badge updated to match.

---

## [1.8.0-dev.5] — 2026-04-19

Follow-up to Grok's full architecture and security review. Verified ~11 items as already correct (JWT cookie flags, `SECRET_KEY` enforcement, bcrypt, rate limiting, query-log indexes, etc.) and closed the three meaningful gaps that remained.

### Added

- **`ENABLE_API_DOCS` setting** (default `true`, preserves current behaviour). Setting `ENABLE_API_DOCS=false` returns `404` from both `/docs` (Swagger UI) and `/openapi.json`. Useful when MyPi is reachable from anything less trusted than the local LAN — both endpoints reveal the full API surface to unauthenticated callers. Wired in `app/config.py`, `app/main.py` (both the `FastAPI(openapi_url=...)` constructor arg and the `/docs` route guard), and `.env.example`.
- **Content-Security-Policy header on every response.** Added to the existing `_security_headers` middleware in `app/main.py`. Policy:
  `default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; img-src 'self' data:; font-src 'self' data: https://cdn.jsdelivr.net; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'`.
  `'unsafe-inline'` on `script-src` is required for the theme pre-paint script in `base.html` and `docs.html`; `cdn.jsdelivr.net` is required for Bootstrap, Chart.js, and Swagger UI. An XSS that tries to fetch from or ex-fil to an arbitrary external origin is now blocked.
- **Dependabot configuration** (`.github/dependabot.yml`) on a weekly cadence covering `pip`, `github-actions`, and `docker` ecosystems. Dependabot PRs are labelled `dependencies` plus the relevant ecosystem label.

### Changed

- **README** — new “Independent reviews” section documenting the Gemini adversarial audit and Grok architecture audit, what was verified as already-correct, and what was changed in response. Environment variable table now also lists `SECURE_COOKIES`, `VERIFY_PIHOLE_SSL`, and `ENABLE_API_DOCS`.

---

## [1.8.0-dev.4] — 2026-04-19

Follow-up to an adversarial audit review. Two items worth fixing; the rest of the audit was already addressed by earlier hardening work.

### Added

- **Teleporter ZIP validation before broadcast.** `sync_service.run_sync` now calls `_validate_teleporter_zip(data)` on the master's export before any replica receives it. The check enforces a minimum byte floor (1 KB — smaller than any realistic Pi-hole v6 teleporter), opens the archive, runs `ZipFile.testzip()` for CRC integrity on every member, and rejects archives with zero members or all-zero-length members. Pi-hole v6's `/api/teleporter` is a single commit point on each replica with no server-side staging, so a corrupt export would otherwise overwrite every replica's working config. This is the guard that keeps a bad export from fanning out.
- **Dockerfile HEALTHCHECK.** Uses the already-installed `curl` to hit `/api/health` (unauthenticated, no DB calls on the hot path). 30 s interval, 5 s timeout, 30 s start period, 3 retries.

### Changed

- **Container now runs as a non-root user.** A system user `app` (UID 1000, GID 1000, `/usr/sbin/nologin`, no home dir) is created in the image, application files are `COPY --chown=app:app`, and the image finishes with `USER app` so the Python process runs unprivileged. Pip install still happens as root so site-packages lands in `/usr/local` (standard Docker Python pattern).

### Audit items that did **not** require changes

- **Error isolation on polling** — already correct. `collector.poll_stats / poll_queries / fetch_all_instance_versions` each `asyncio.gather(...)` over per-instance coroutines whose bodies are wrapped in `try/except Exception`, so one failing Pi-hole cannot propagate and stop the others.
- **Sensitive data exposure** — already handled. `PiholeInstance.api_password` uses the `EncryptedString` (Fernet) TypeDecorator at rest (migration `0006`). No API response schema includes the password. Every log call in `pihole_client.py` uses `self.base_url` or `instance.name`, never the password. httpx exception messages do not include request bodies, so the `_authenticate` failure log is safe.
- **Dependency pinning** — already fully pinned. `requirements.txt` uses `==` on every entry.

---

## [1.8.0-dev.3] — 2026-04-19

### Added

- **Dark mode on the API docs (`/docs`) page.** The Swagger UI page now follows the user's MyPi theme (light/dark/system) via the same `localStorage['mypi-theme']` key used by the rest of the UI. A custom `docs.html` template pre-paints the theme on load (mirrors `base.html` to avoid a flash), listens for `storage` events so a theme change in the dashboard tab updates the docs tab live, and follows system preference changes when the user has the theme set to `system`. Dark overrides live in a new `app/static/css/swagger-dark.css` scoped to `[data-bs-theme="dark"]` and colour-matched to the dashboard palette (`#1a1d20` / `#212529` / `#2b2f33` / `#373b3e` / `#dee2e6`). HTTP method tints (GET/POST/PUT/DELETE) are preserved for readability.

### Changed

- `main.py` no longer uses `fastapi.openapi.docs.get_swagger_ui_html` — `/docs` now renders the new `docs.html` Jinja template so we control the `<head>` (theme script) and can load our CSS alongside the Swagger UI CDN bundle.

---

## [1.8.0-dev.2] — 2026-04-19

### Fixed

- **`/openapi.json` 500 error (and with it, the `/docs` Swagger UI page).** `app/api/domains.py` had `from __future__ import annotations` at the top. Combined with the new `@limiter.limit(...)` decorator wrapping the route handlers, FastAPI could not resolve the `DomainRequest` forward reference through slowapi's wrapper and fell back to treating the body param as a `Query`, which Pydantic then rejected during schema generation (`PydanticUserError: ... is not fully defined`). Removing the future import from this one file restores schema generation. The other API modules using `@limiter.limit` (`sync.py`, `notifications.py`, `auth.py`) never had the future import, which is why they were unaffected.

---

## [1.8.0-dev.1] — 2026-04-19

Development branch (`hardening-review`) — not a production release. A Docker image is published to `ghcr.io/theojamesvibes/mypi:hardening-review` on every push to this branch so it can be pulled and tested. The web UI shows a yellow **DEV** badge next to the version string while this build is running.

This batch applies the security + efficiency review done after the mypi-ios 0.1.0 ship:

### Added

- **Read-only API key scope.** New `is_read_only` column on `api_keys` (migration `0009`). Key creation exposes a `is_read_only: bool` field in the POST body and in the response. A new `require_mutation` dependency rejects read-only keys from every mutation endpoint with HTTP 403. Session cookies and bearer JWTs are always allowed.
- **Security headers middleware.** `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: same-origin` on every response. `Strict-Transport-Security` is added when `SECURE_COOKIES=true` (i.e. when MyPi is known to be behind TLS).
- **Rate limits on mutation endpoints.** `/api/sync` (10/min), `/api/domains/{deny,allow}` (30/min each), `/api/notifications/test` (5/min — protects Pushover quota), `/api/notifications/validate` (10/min), `/api/auth/change-password` (5/min — brute-force hardening).

### Changed

- ~~**`verify_pihole_ssl` now defaults to `True`.**~~ **Reverted**: the flip broke every existing self-signed Pi-hole deployment on upgrade. Default stays `False`; if the MyPi ↔ Pi-hole path is not a trusted network segment, set `VERIFY_PIHOLE_SSL=true` in `.env` explicitly. README will document the recommendation without changing the default.
- **Background tasks** spawned with `asyncio.create_task(...)` in `main.py`, `sync_service.py`, and `collector.py` now go through a `_spawn(...)` / `_track_task(...)` helper that stores a strong reference until completion and logs any uncaught exception. Previously the event loop only held weak references, so GC could drop a task mid-run and an exception could vanish silently.

### Fixed

- **`/api/auth/logout` Bearer-token revocation.** The `authorization` param was declared as `Cookie(default=None)` instead of `Header(default=None)`, so logging out via `Authorization: Bearer …` never added the JTI to `revoked_tokens`. Web-UI cookie logout was unaffected.
- **Collector dict leak.** `_prev_status`, `_offline_retry_count`, `_offline_alert_count` now pruned alongside `_last_seen_ts` when an instance is deactivated (previously only `_last_seen_ts` was cleaned; the others accumulated stale entries for removed instances).

### Removed

- Dead code: `PiholeClient.get_top_stats`, `PiholeClient.get_history`, `PiholeTopStats` dataclass (unused, and `get_history` referenced an undefined `PiholeHistoryBucket` class — would have raised `NameError` if ever called); `Settings.validate_encryption_key_at_startup` (superseded by `main.py:_ensure_encryption_key`); the STARTUP diagnostic block in `sync_service.load_schedule` that dumped `app_settings` and round-tripped a write on every boot.

---

## [1.7.6] — 2026-04-17

### Fixed
- Recurring `[SSL: SSLV3_ALERT_HANDSHAKE_FAILURE]` on Raspberry Pi 3: root cause was httpx's connection pool keeping idle connections alive for 5 seconds (default `keepalive_expiry`) while the poll interval is 10 seconds — so connections were never reused but also never cleanly closed. On a memory-constrained RPi 3, FTL's embedded web server gradually accumulated these half-open TLS connections until its thread pool was exhausted. Fixed by disabling keepalive pooling entirely (`max_keepalive_connections=0`) so every request closes the connection immediately with a proper TLS `close_notify`, giving FTL a clean teardown after each poll.
- Added self-healing recovery: if `ssl.SSLError`, `httpx.ConnectError`, or `httpx.RemoteProtocolError` is caught in either stats or query polling, the stale client is evicted from the client manager so the next poll starts a fresh connection rather than retrying on a broken one.

## [1.7.5] — 2026-04-17

### Changed
- Reverted all SSL context changes from 1.7.1–1.7.4 (SECLEVEL=0, OP_LEGACY_SERVER_CONNECT, TLS 1.2 maximum, custom AsyncHTTPTransport). Root cause of the Raspberry Pi 3 SSL errors was Pi-hole FTL having a corrupted TLS state (likely accumulated stale connections from container restarts during debugging), not a TLS configuration incompatibility. Rebooting the Pi-hole cleared the state and connections resumed normally. Connections are back to the original `verify=settings.verify_pihole_ssl` behavior — secure TLS defaults with cert verification disabled only when `VERIFY_PIHOLE_SSL=false` is explicitly set.
- **Troubleshooting note:** If you see `[SSL: SSLV3_ALERT_HANDSHAKE_FAILURE]` errors for a Pi-hole instance, run `sudo systemctl restart pihole-FTL` on that Pi-hole. This clears any stale TLS connections that have exhausted FTL's connection pool without requiring a full reboot.

## [1.7.4] — 2026-04-17

### Fixed
- SSL handshake failure on Raspberry Pi 3 (ARMv7): Pi-hole FTL on ARMv7 is statically linked against OpenSSL 1.1.x whose TLS 1.3 extension handling is incompatible with the TLS 1.3 ClientHello that Python 3.12 / OpenSSL 3.x sends. The permissive ssl context now sets `maximum_version = TLSv1_2` to avoid TLS 1.3 negotiation entirely. TLS 1.2 with `SECLEVEL=0` and `OP_LEGACY_SERVER_CONNECT` is fully compatible with all Pi-hole builds including older ARM binaries, while remaining secure for internal LAN use.

## [1.7.3] — 2026-04-17

### Fixed
- SSL handshake failure (root cause identified): `httpx 0.28.0` silently dropped support for passing `ssl.SSLContext` as the `verify=` parameter on `AsyncClient` — a truthy SSLContext object was treated as `verify=True` and the custom permissive context from 1.7.1/1.7.2 was never applied. All connections continued using Python's default `SECLEVEL=2` context, which rejects Pi-hole FTL's cipher configuration on Debian 13 ARM. Fixed by building `AsyncHTTPTransport(verify=ssl_context)` explicitly (which still supports SSLContext) and passing it as the `transport=` argument.

## [1.7.2] — 2026-04-17

### Fixed
- SSL handshake failure on Debian 13 ARM Pi-hole instances: dropped SSL security level to `SECLEVEL=0` (from 1) and added `OP_LEGACY_SERVER_CONNECT` to the permissive SSL context. OpenSSL 3.x disables legacy TLS renegotiation by default; Pi-hole's FTL web server on Debian 13 triggers this path. Both flags are only applied when `VERIFY_PIHOLE_SSL=false`.
- Version badge incorrectly showing red (update available) immediately after upgrading MyPi: `up_to_date` now uses semver `>=` comparison so a running version newer than the last cached GitHub check is correctly treated as current.

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
