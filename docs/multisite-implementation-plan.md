# Multi-site — implementation plan

Status: **draft**
Branch: `multisite`
Paired with: `docs/multisite-design.md` and `docs/multisite-migration-plan.md`

Phased code order. Each phase should produce a working build with tests
passing, even before the next phase starts. This keeps the branch
reviewable at checkpoints instead of as one giant diff at the end.

---

## Phase 0 — design docs (DONE)

- `docs/multisite-design.md` — decisions locked.
- `docs/multisite-migration-plan.md` — schema plan.
- `docs/multisite-implementation-plan.md` — this file.

First code commit should bump VERSION to `1.11.0-dev.0` (multi-site is
a minor-version feature, not a patch).

---

## Phase 1 — schema + models (DB foundation only)

**Goal:** `alembic upgrade head` works on a fresh DB and on an existing
prod DB. No behavior change in the app yet; everything still reads/writes
like before because the Default site silently wraps existing data.

**Files:**
- `alembic/versions/0013_multisite.py` — new (see migration plan).
- `app/models/site.py` — new file: `Site`, `SiteSlugHistory`, `SiteSetting`
  ORM models.
- `app/models/pihole.py` — add `site_id` FK + `site` relationship to
  `PiholeInstance`. Drop `name UNIQUE`, add `UniqueConstraint("site_id", "name")`.
- `app/models/__init__.py` — export new models.

**Tests:**
- `test_migration_0013.py` (upgrade + downgrade round-trip with fixture
  data).
- Existing model tests pass unchanged.

**Exit criteria:** migration applies cleanly; nothing else in the app
references `Site` yet.

---

## Phase 2 — config loader + Main-site machinery

**Goal:** YAML detection, Main promotion, orphan detection. Still no API
or UI changes.

**Files:**
- `app/config.py` — add `SiteConfig` dataclass; extend
  `load_instance_configs()` to return `list[SiteConfig]` with backcompat
  wrapping. Validate at-most-one `main: true`; if none and new YAML, first
  site implicitly becomes Main; if legacy flat YAML, Default is Main.
- `app/services/config_loader.py` — rename to `sync_sites_and_instances()`:
  1. Upsert sites by slug (or name if no slug given), set Main flag.
  2. Upsert instances by `(site_id, name)`.
  3. Deactivate removed sites (orphan detection).
  4. Run Main-reassignment logic if the currently-Main site is being
     deactivated: promote next-in-YAML-order site, materialize inherited
     settings (copy old Main's resolved values into new Main's
     `site_settings` rows where the new Main currently has NULL).
  5. Evict client_manager clients for deactivated instances.
- `app/services/site_settings.py` — new module (parallel to existing
  `poll_settings.py`, `session_settings.py`, etc.). Functions:
  - `get_setting(site_id, key, default=None)` → resolves with Main
    fallback.
  - `set_setting(site_id, key, value)` → upsert via
    `pg_insert ... ON CONFLICT DO UPDATE` (per CLAUDE.md's established
    pattern).
  - `clear_setting(site_id, key)` → set value to NULL (= inherit).

**Tests:**
- `test_config_loader_multisite.py` — legacy flat YAML, new `sites:` YAML,
  Main-designation rules, orphan detection, Main-reassignment with
  settings materialization.
- `test_site_settings.py` — inheritance resolution, explicit-null
  (inherit) vs. missing row, Main doesn't inherit from itself.

**Exit criteria:** container starts against a legacy YAML, migration
backfills, loader reconfirms the backfilled state on second boot with no
changes. With a `sites:` YAML, multiple sites appear in the DB.

---

## Phase 3 — collector + client_manager (per-site scheduling)

**Goal:** polling respects per-site intervals. Clients are still keyed by
`instance_id` (no change needed there).

**Files:**
- `app/services/collector.py` — restructure scheduler setup:
  - Replace two global APScheduler jobs (`poll_queries`, `poll_stats`)
    with per-site jobs. Each site gets its own pair, using its own
    interval from `site_settings`.
  - Add helpers `schedule_site(site_id)` and `reschedule_site(site_id)`
    for dynamic add/update/remove as YAML changes or settings update.
  - Per-site master-blocklist-delta check (fires against each site's
    master, not a single global master).
  - Circuit breaker, eviction, dedup logic unchanged — they already key
    on instance_id.
- `app/services/client_manager.py` — no changes expected; client keyed by
  instance_id, shared across stats/queries/sync, works per-site
  automatically.

**Tests:**
- Integration test: two sites with different poll intervals, verify each
  fires on its own cadence within a short soak window (or use mocked
  scheduler clock).
- Regression: circuit breaker still trips per-instance, not per-site.

**Exit criteria:** collector logs show per-site poll cycles with site
name in the log line. Blocklist-delta trigger fires per site.

---

## Phase 4 — sync service (per-site master/replicas)

**Goal:** sync is per-site. Scheduler fires one job per site on that
site's cadence. Manual sync API endpoints still work (but only against
one site at a time).

**Files:**
- `app/services/sync_service.py` — change `run_sync()` signature to
  `run_sync(site_id)`. Inside: pick master from the site's active
  instances, fan out to that site's replicas only. All existing logic
  (teleporter export/import, gravity, retry, DB upsert) stays the same,
  just scoped.
- Scheduler registration moves from one global job to per-site jobs in
  Phase 3's collector setup (or lives in sync_service.py — TBD during
  implementation).

**Tests:**
- `test_sync_multisite.py` — two sites each with a master and replicas,
  verify sync in site A doesn't touch site B's instances even if triggered
  simultaneously.
- Regression: single-site legacy YAML still syncs correctly (Default site
  sync against its master, same behavior as pre-multisite).

**Exit criteria:** manual sync via `/api/sync/run` (legacy) works against
Main; manual sync via `/api/sites/{slug}/sync/run` works against the
named site. Auto-sync trigger fires per site.

---

## Phase 5 — API surface

**Goal:** canonical per-site routes live under `/api/sites/{slug}/`.
Legacy un-prefixed routes become Main aliases.

**Files:**
- `app/api/sites.py` — new: `GET /api/sites` (list), `GET /api/sites/{slug}`
  (detail), `GET /api/sites/inactive` (orphans), `DELETE /api/sites/{slug}`
  (cleanup), `PATCH /api/sites/{slug}` (rename/slug-change). Slug changes
  write the old slug into `site_slug_history`.
- `app/api/stats.py`, `queries.py`, `instances.py`, `sync.py`,
  `notifications.py`, `domains.py`, `poll_settings.py` — restructure:
  - Move existing handlers under a new router prefix
    `/api/sites/{slug}` with a `site: Site = Depends(resolve_site)`
    dependency injected.
  - Keep a thin legacy shim at the old un-prefixed path that resolves
    Main and delegates.
  - `resolve_site` dependency: look up slug in `sites`, then
    `site_slug_history` (301 redirect for history hits), then 404.
- `app/main.py` — wire both routers.
- `app/api/__init__.py` — exports.

**Tests:**
- Per-endpoint test: `/api/stats/summary` returns Main's summary;
  `/api/sites/home/stats/summary` returns Home's summary; different data
  when there are multiple sites.
- Slug history: change slug, old URL returns 301 to new URL.
- 404 on unknown slug, 301 on historical slug.
- Reserved slug rejected at save time.

**Exit criteria:** every legacy route still returns the exact same JSON
it returned pre-multisite when only one site exists. OpenAPI schema
renders both route groups clearly.

---

## Phase 6 — web UI

**Goal:** site dropdown in the header switches the whole UI. Every page
is bookmarkable as `/dashboard/{slug}`, `/queries/{slug}`,
`/settings/{slug}`.

**Files:**
- `app/templates/base.html` — add site dropdown component (reads from
  `/api/sites`; selected state from URL path). Dropdown shows site names,
  indicates Main with an icon/label, link to "All Sites" is present but
  disabled with "Coming soon" tooltip (reserves UX space for the deferred
  feature).
- `app/templates/dashboard.html`, `queries.html`, `settings.html` — add
  `{slug}` to route, scope all data fetches to the selected site.
- `app/templates/settings.html` — biggest change:
  - Section headers grouped by setting family (Pushover, Sync, Poll).
  - Each per-site setting input has the "Use [Main name]'s settings"
    checkbox next to it. When checked, input is disabled and stored
    value is NULL.
  - On Main's own settings page, checkboxes are hidden.
  - Orphan sites section near the bottom with cleanup UI + confirm
    modal showing row-count impact.
- `app/static/` — any JS/CSS needed for the dropdown and checkbox
  behavior.

**Tests:**
- Playwright/manual: site switch works, settings inherit correctly,
  bookmarkable URLs survive refresh, orphan cleanup modal shows correct
  counts.

**Exit criteria:** fresh install shows Default site with no dropdown
chrome feeling out of place; multi-site YAML shows dropdown; switching
sites updates dashboard, query log, and settings atomically.

---

## Phase 7 — iOS API discovery (informational only)

Not in this branch. Handled in `mypi-ios` after backend ships.

- iOS `Site` (= one MyPi server) stays as-is.
- New `BackendSite` concept: fetched from `GET /api/sites` on a server
  that exposes multiples. If the server returns one site, iOS hides the
  sub-picker. If it returns N > 1, iOS shows a secondary picker in the
  navigator.
- `APIClient` prepends `/api/sites/{slug}` to every call; the slug lives
  alongside `baseURL` in the `Site` model as a new optional attribute.
- Release iOS update *after* the backend ships so iOS can assume the new
  endpoints exist once multi-site is detected.

---

## Version / CHANGELOG / README workflow (per CLAUDE.md)

Every phase that changes code bumps VERSION and updates CHANGELOG. Rough
plan:

- Phase 1: `1.11.0-dev.0` (schema)
- Phase 2: `1.11.0-dev.1` (config loader)
- Phase 3: `1.11.0-dev.2` (per-site collector)
- Phase 4: `1.11.0-dev.3` (per-site sync)
- Phase 5: `1.11.0-dev.4` (API)
- Phase 6: `1.11.0-dev.5` (UI)
- Final merge to main: `1.11.0`

README update happens in a single commit at the end of Phase 6 reflecting
the new feature, the YAML format addition, and the legacy-compat story.

---

## Rollback safety

At each dev.N tag, a user can:
- Pin to that image and safely stay there indefinitely.
- Downgrade to `1.10.x` *only if they never ran multi-site YAML* — the
  Alembic downgrade to 0012 loses non-Main site config. Documented in
  CHANGELOG.
