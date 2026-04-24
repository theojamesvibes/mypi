# Multi-site support — design questions

Status: **All decisions locked. Ready for migration + implementation plans.**
Branch: `multisite`

This doc exists to answer design questions *before* any code changes. One MyPi
deployment will group its Pi-holes into up to 10 **sites**, each with up to 10
Pi-hole instances, its own master, and its own sync lifecycle. Existing single-
site deployments must keep working with no config changes.

---

## Core concept introduced by Q3's answer — "Site Main"

One site per deployment is designated the **Main site**. Other sites can opt,
per setting, to *inherit* from Main instead of configuring their own value.

- Designated in YAML with `main: true` on exactly one site. Legacy
  (single-site) deployments: the auto-created `Default` site is Main.
- `sites` table gets an `is_main: bool` column with a partial unique index
  ensuring exactly one row has `is_main = TRUE`.
- Storage: a per-site setting with value `NULL` (or sentinel) means
  "inherit from Main." Reads resolve at query time.
- UI: on Main's settings page the "Use Main's settings" checkbox is hidden
  (would be a cycle). On other sites, each setting shows the checkbox —
  checked = disabled input + stored as inherit.

**Inheritance scope ✅ DECIDED — broad (every per-site setting).** Consistent
UX wins over fine-grained curation. Every per-site setting on a non-Main site
shows the "Use [Main's name]'s settings" checkbox.

### Main-site deletion / YAML removal — reassignment rule ✅ DECIDED

If the currently-designated Main site is removed from YAML (or has its
`main: true` flag dropped), MyPi must **auto-promote a new Main** so inherited
settings across other sites keep resolving.

**Promotion rule:**
1. Pick the first still-active site in YAML-declaration order.
2. If no active sites remain, there's nothing to promote (single-site
   fallback handles this naturally once a site is re-added).

**Settings-materialization rule (the "populate" behavior you asked for):**
- When site X is promoted to Main, any setting on X that was currently
  stored as `NULL` (inheriting from the old Main) is *materialized* by
  copying the old Main's resolved value into X before the old Main is
  deactivated.
- Same pass for every other non-Main site that was inheriting a setting
  that differed between old-Main and new-Main — actually no, those sites
  continue to inherit; now they inherit from the new Main, which now has
  the old Main's values it just absorbed. Net behavior: inherited values
  are preserved across the reassignment. Operators see no change in
  notification routing, sync cadence, etc.

This materialization step runs once, transactionally, inside
`config_loader.sync_instances()` (renamed to `sync_sites_and_instances()`).

### Orphan site cleanup ✅ DECIDED — mirror the orphan-pihole pattern

Sites currently in the DB that are no longer in YAML get marked
`is_active = FALSE` (new column on `sites`), same as the existing
pihole-instance flow. The admin UI exposes:

- An "Orphaned sites" section (parallel to today's "Orphaned instances"
  at `/api/instances/inactive`) listing deactivated sites.
- A cleanup action that cascades the delete:
  site → its pihole_instances → their stats_snapshots + query_logs +
  app_settings rows. All FKs will have `ON DELETE CASCADE`.
- Confirmation modal showing the row-count impact before deletion, because
  deleting a busy site can wipe weeks of query history.

Orphan cleanup is **manual only**. We never auto-delete — same principle
as orphan piholes today.

---

## Back-compat shim (decided — not a question)

`config_loader` detects the YAML shape:

```yaml
# Legacy — keeps working unchanged.
instances:
  - name: "Living Room"
    ...

# New — up to 10 sites, each with up to 10 instances.
sites:
  - name: "Home"
    instances:
      - name: "Living Room"
        ...
  - name: "Cabin"
    instances:
      - ...
```

If `sites:` is present → new path. Else → wrap the flat `instances:` list into
an implicit site named `Default`. Existing DB rows get backfilled into that
same `Default` site by the migration.

---

## Q1 — Which settings are per-site, which stay global? ✅ DECIDED

**All app-level settings are per-site.** The settings page scopes to the
currently selected site (from the site dropdown — see Q5) and edits one
site at a time.

**Exceptions** (stay global — these aren't really "app settings," they're
infrastructure / auth primitives that can't sensibly differ per site):

| Stays global | Why |
|---|---|
| `session.ttl_minutes` (JWT TTL) | Auth is a property of the MyPi deployment; a single browser session can't be "on" two sites at once. |
| Encryption key, DB URL | Infrastructure. |
| API key records themselves | One API key belongs to one user; scoping per-site is a separate future feature (see "Not in scope"). |

**Everything else is per-site**, including:
- `sync.schedule_minutes`, `sync.last_result`, all sync toggles
  (`config_enabled`, `gravity_enabled`, `dhcp_enabled`, `run_gravity`,
  `master_blocklist_delta_threshold`)
- Pushover (`user_key`, `api_token`, `enabled`) — see Q3
- Poll intervals (`poll.query_interval_seconds`, `poll.stats_interval_seconds`)
  — see implications below

### Storage mechanic

Per-site keys use a composite `(site_id, key)` primary key. New schema for
`app_settings`:

```sql
-- Replaces the flat key/value table:
app_settings(
  site_id UUID REFERENCES sites(id) ON DELETE CASCADE,
  key     VARCHAR(128) NOT NULL,
  value   TEXT,            -- NULL = "inherit from Main" (for non-Main sites)
  PRIMARY KEY (site_id, key)
)
```

Read resolution:
1. Look up `(site_id, key)`.
2. If row missing OR `value IS NULL` AND site is not Main → fall back to
   `(main_site_id, key)`.
3. If still missing → hard-coded default.

### Implications of "poll intervals per-site"

Each site gets its own poll interval → the collector has to schedule per-site
jobs, not one global job. That's a small refactor in `collector.py`:
APScheduler already supports multiple `IntervalTrigger` jobs. The scheduler
setup code becomes "for each site, schedule a stats-poll job and a
queries-poll job at that site's configured intervals." Evicting/rescheduling
when a site's interval changes is the new work — maybe +50 LOC in
`collector.py` + `poll_settings.py`.

---

## Q2 — API namespacing ✅ DECIDED — Option A with slug, legacy routes kept forever

**Canonical routes use a slug path prefix:**

```
GET  /api/sites/{slug}/stats/summary
GET  /api/sites/{slug}/queries
POST /api/sites/{slug}/sync/run
```

**Legacy routes (kept forever) resolve to Main:**

```
GET  /api/stats/summary       → /api/sites/{main-slug}/stats/summary
POST /api/sync/run            → /api/sites/{main-slug}/sync/run
```

Legacy handlers are thin shims: they look up the Main site, then delegate
to the real per-site handler. No deprecation timeline — maintenance cost
is near-zero and keeping them forever guarantees old iOS builds, curl
scripts, and third-party integrations never break.

**Slug generation and stability:**
- Slug is derived from site name on first save: `"Home Base" → "home-base"`,
  stored in `sites.slug` (separate column from name).
- Slug is editable in settings, independent of name (operator can pick a
  short memorable slug).
- `UNIQUE` constraint on `sites.slug` among active sites.
- On slug change, the old slug goes into `site_slug_history` and serves as
  a permanent HTTP 301 redirect target to the new slug. Bookmarks and old
  scripts keep working.
- Reserved slugs (cannot be used): `sites` (conflict with `/api/sites`),
  `inactive`, `admin`, `main`, `default`. List enforced at save time.

**Design analysis below is retained for context / future reference.**

### Context — expanded tradeoff analysis

The three options look superficially similar but differ on ~7 axes that all
show up in practice. Rated per axis, A/B/C where applicable.

### Option A — path prefix: `/api/sites/{site}/...` (+ legacy alias)

```
GET  /api/sites/{site}/stats/summary
GET  /api/sites/{site}/queries
POST /api/sites/{site}/sync/run

# Legacy, kept forever, implicitly → Main site:
GET  /api/stats/summary
POST /api/sync/run
```

- **Ergonomics (curl / browser / docs):** best. URL *is* the site identity.
  OpenAPI schema renders it naturally. Logs show which site a request hit
  without extra config. One curl works:
  `curl https://mypi.example.com/api/sites/home/stats/summary -H "X-API-Key: ..."`.
- **Back-compat:** excellent. Legacy routes stay as thin shims that resolve
  to Main and call the real handler. Zero-change for existing iOS builds,
  zero-change for any scripts a user has. If we log a one-time
  `legacy-route-used` WARN per client, we can later see who hasn't migrated.
- **Caching:** correct by default. The URL is the cache key, so browsers,
  CDNs, and HTTP caches (if any are ever added) don't cross-contaminate
  sites.
- **Deployment / Traefik:** no changes needed. Same host, same cert, same
  router.
- **Correctness risk:** low. Wrong site in a URL is visible; typos fail
  loudly with 404.
- **iOS integration effort:** low. `APIClient` builds one base URL per
  `(Site, backendSiteId)` pair and reuses it. The iOS `Site` model stays
  one MyPi server; a `backendSiteId` property gets added.
- **Code churn:** ~20 FastAPI route decorators each gain a `{site}` path
  parameter and a `site: Site = Depends(resolve_site)` injected dependency.
  Actual handler bodies barely change — they just pass `site.id` into the
  query filter instead of not filtering.
- **Sub-decision: UUID or slug in the URL?**
  - **UUID** (`/api/sites/4f3a.../stats/summary`): stable forever, site
    rename is free. URL is ugly, not memorizable.
  - **Slug** (`/api/sites/home/stats/summary`): memorable, curl-friendly.
    Rename breaks existing URLs and any scripts that use them unless we
    keep a slug-history alias table. Adds one small moving part.
  - Recommended path if A wins: **slug**, auto-derived from site name
    (`"Home Base" → "home-base"`), stored separately so rename keeps the
    old slug as a permanent redirect. iOS uses slug for URLs, UUID for
    persistent references.

### Option B — header-based: `X-Site-Id: <uuid>`

```
GET /api/stats/summary
X-Site-Id: 4f3a-...
```

- **Ergonomics:** weakest. Every curl needs `-H "X-Site-Id: ..."`. Logs
  need explicit header logging (not default in uvicorn). OpenAPI schema
  shows the header param but it's less scannable than a URL segment.
- **Back-compat:** also excellent — legacy clients omit the header and
  get Main. But this makes forgotten-header bugs *silent*: you thought
  you were querying Cabin, you get Home.
- **Caching:** hazardous. The URL alone isn't the cache key anymore.
  Every response needs `Vary: X-Site-Id`. Browser/proxy caches that
  ignore Vary will serve wrong-site data. This is a real correctness
  risk, not just an ergonomics nit.
- **Deployment / Traefik:** no change needed. Traefik can route by header
  if we ever split sites onto different backends, but we don't need that
  now.
- **Correctness risk:** highest of the three. "Wrong site" is invisible
  in URL logs; the fingerprint only shows up if you log request headers.
  Integration tests won't naturally catch it — you have to test for it.
- **iOS integration effort:** slightly lower than A's upfront cost (no
  URL-builder changes) but higher ongoing — every new request type is
  one more place to remember to add the header. Easy to regress.
- **Code churn:** less than A — routes keep their current paths. But you
  need a FastAPI middleware to read the header and inject a `site` into
  request state, plus every handler needs to pull it out. Net: similar
  LOC to A, concentrated in a middleware rather than spread across route
  decorators.
- **Sub-decision: what happens on a multi-site deployment if the header is
  missing?** Three sub-options: (i) 400 error; (ii) silently default to
  Main (lossy, breaks least); (iii) return all-sites aggregate (expensive,
  weird). None is obviously right — each has ergonomic/correctness costs.

### Option C — subdomain: `home.mypi.example.com`, `cabin.mypi.example.com`

```
GET https://home.mypi.example.com/api/stats/summary
GET https://cabin.mypi.example.com/api/stats/summary
```

- **Ergonomics:** cleanest URL, worst setup. User mental model maps 1:1
  to DNS.
- **Back-compat:** poor without work. Single-site deployments currently
  live at one hostname (e.g. `mypi.myssdomain.net`); adding sites means
  adding hostnames, which means DNS records and TLS cert SAN entries
  (or a wildcard cert) per site. Self-signed / LAN-only users without
  a DNS server can't use it at all.
- **Caching:** correct by default (hostname is part of origin).
- **Deployment / Traefik:** biggest burden. Every new site = new Traefik
  router rule + new cert SAN. For Let's Encrypt wildcard, requires DNS-01
  challenge which many home setups don't support. Changes
  `docker-compose.yml` in non-trivial ways.
- **Correctness risk:** lowest. Browser origin isolation is free
  protection. Cookie scoping naturally separates sites.
- **iOS integration effort:** user has to enter one "Site" per backend
  site in the iOS app (since each has a different hostname). This breaks
  the current iOS `Site` abstraction cleanly — but it also means iOS
  gets *no benefit* from sub-site discovery. Multi-site stops being a
  backend feature and becomes pure DNS gymnastics.
- **Code churn:** smallest in the backend — routes don't change, a
  middleware reads `request.url.hostname` and resolves to a site. But
  total deployment complexity is highest.
- **When C makes sense:** hosted/SaaS MyPi serving multiple tenants
  behind a wildcard cert. For self-hosted home use, it's overkill.

### Summary table

| Axis | A (path) | B (header) | C (subdomain) |
|---|---|---|---|
| curl ergonomics | ✅ best | ⚠️ needs `-H` | ✅ clean |
| OpenAPI / docs clarity | ✅ | ⚠️ | ✅ |
| Log visibility of site | ✅ in path | ❌ needs header logging | ✅ in host |
| Back-compat for legacy clients | ✅ aliased | ✅ header optional | ❌ requires DNS work |
| Cache-safety | ✅ | ⚠️ Vary required | ✅ |
| Deployment complexity | ✅ zero | ✅ zero | ❌ DNS+certs per site |
| Correctness risk (wrong-site response) | low | **highest** | lowest |
| iOS integration effort | low | low-ongoing-cost | medium (one Site per backend site) |
| Code churn surface | ~20 route params | 1 middleware + handler reads | 1 middleware |

### Security analysis (answering your "which is more secure")

Security for a self-hosted MyPi has a few distinct concerns — they rank
differently on each option:

**1. Cross-site browser isolation (XSS/CSRF containment)**
- **C wins, decisively.** Each site is its own browser origin
  (`home.mypi.example.com` vs `cabin.mypi.example.com`). If any site's UI
  ever served malicious content (e.g. reflected XSS in a query-log
  display), the same-origin policy prevents it from reading or acting on
  other sites. Cookies are naturally separated.
- A and B are tied: all sites share one origin (`mypi.example.com`). A
  hypothetical XSS on any site's UI reaches every site's data, because the
  browser sees them as one origin.
- Practical weight for your deployment: the MyPi web UI doesn't render
  untrusted HTML, API keys aren't copy-pasted from sketchy places, and the
  app is behind Traefik on a home LAN. So this matters in theory more than
  in practice for a one-user home setup. For a hosted/multi-tenant MyPi,
  it would be decisive.

**2. Wrong-site-by-mistake (silent cross-contamination)**
- **A wins.** Site is in the URL. A typo = 404. A copy-paste of a "Cabin"
  URL into a script that should have hit "Home" is visible in every log
  line the moment it runs.
- **C ties.** Wrong hostname = DNS NXDOMAIN or cert mismatch. Also loud.
- **B is the worst of the three.** A missing or wrong `X-Site-Id` header
  silently resolves to Main. Automation scripts that forget the header
  (or set it wrong in one branch of a conditional) modify the wrong site
  and nothing flags it.

**3. Audit trail after an incident**
- **A and C tie.** Site is in the access log for every request by default,
  zero config.
- **B loses.** Uvicorn/nginx/Traefik don't log arbitrary request headers
  by default. You'd have to configure custom log formats on every layer to
  be able to answer "what site did request X hit?" after the fact. Missing
  forensic data is a real operational security cost.

**4. API key scoping**
- **A best.** Per-API-key allowed-site list becomes a trivial middleware
  check: `site_id in api_key.allowed_site_ids`. Path matching is
  self-evident in code review.
- **B works** but the middleware has to be *every*where; skipping a route
  defaults silently to Main. Easier to introduce a scoping bypass bug.
- **C works** via `Host:` header check. Equivalent security to A but the
  middleware reads from a slightly less obvious place (request.url.host
  vs. a path segment).

**5. Cache / cache-poisoning**
- **A and C are safe by default.** URL (path or host) is part of the cache
  key everywhere.
- **B is risky.** A response for Cabin can be cached and served to a
  request for Home if any cache in the chain (browser, proxy, CDN) doesn't
  honor `Vary: X-Site-Id` — and many don't for non-standard headers. For
  home-LAN use with no CDN this is mostly theoretical; for any deployment
  with a reverse-proxy cache or CDN, it's a real concern.

**6. TLS / cert surface**
- **A and B win** (one cert, one host).
- **C has more surface** (wildcard cert or multi-SAN cert, more places a
  private key could leak, more renewal points to monitor). For home use
  this is a minor cost; for hosted, it matters.

**7. URL leakage in bookmarks / browser history / share sheets**
- All three expose site identity in URLs once a user bookmarks or shares.
  A's `/dashboard/home` and C's `home.mypi.example.com/dashboard` are
  equivalent on this axis. B hides the site from the URL, but since the
  site is only reachable with valid auth anyway, "URL leakage" doesn't
  itself breach security — the bookmark without auth gets you a 401.

### Security summary

| Security axis | A | B | C |
|---|---|---|---|
| Cross-site browser isolation | — | — | ✅ |
| Wrong-site-by-mistake | ✅ | ❌ | ✅ |
| Audit trail | ✅ | ❌ | ✅ |
| API key scoping | ✅ best | ⚠️ easy to bypass | ✅ |
| Cache-poisoning resistance | ✅ | ❌ | ✅ |
| TLS surface | ✅ | ✅ | ⚠️ more certs |
| Bookmark/history leakage | neutral | neutral | neutral |

**Bottom line on security:**
- **C is most secure** for multi-tenant / shared-access deployments because
  of real browser origin isolation. The deployment complexity is the cost.
- **A is a clear second** and the pragmatic winner for single-user,
  self-hosted MyPi: it's strictly better than B on every security axis,
  and the only axis where C beats A (browser origin isolation) doesn't
  meaningfully apply when there's one user on a home LAN.
- **B is worst on security.** Silent wrong-site risk, weaker audit, weaker
  API key scoping, cache-poisoning hazard. I wouldn't recommend it even
  without the ergonomic issues.

### Q5 bookmarkability feedback → Q2

Your Q5 lean toward bookmarkable URLs tips the scales further:
- **A (slug) supports** bookmarkable per-site URLs cleanly: `/dashboard/home`.
- **A (UUID) supports** them too but less prettily: `/dashboard/4f3a-...`.
- **B does NOT support** per-site bookmarks — URL doesn't carry the site.
  To bookmark the Cabin dashboard you'd need browser-level per-site
  profiles, which isn't practical.
- **C supports** them via hostname, but again carries the DNS cost.

So your Q5 preference further narrows toward A. Between A-slug and A-UUID,
slug is more usable and the rename risk is easily handled by keeping the
old slug as a permanent alias (one small table: `site_slug_history`).

### Revised recommendation

Given your security preference *and* bookmarkability preference, **A with
slug** is now my recommendation. Security-adequate for home use, best
ergonomics, best audit trail, supports bookmarks. C is the "more secure"
answer only if you're willing to absorb the DNS+cert complexity for browser
origin isolation you don't meaningfully benefit from at single-user scale.

### My read (you still decide)

- **A is the default choice for self-hosted.** It's the most diagnosable
  and the most cache-correct, and the legacy alias handles upgrades
  cleanly. The "every handler gains a param" isn't really 20 separate
  changes — it's one FastAPI router prefix + one dependency, applied
  uniformly.
- **B is only attractive if you want absolute URL stability.** Given that
  Q5 picked a site dropdown UI, the URL is going to change based on UI
  state anyway (see Q5 routing decision), so "URL stability" isn't
  actually a goal we have.
- **C should be reserved for a future hosted mode** if you ever run MyPi
  as a service for other users. Not needed for the home/self-hosted
  flow this feature targets.

> **Decisions needed:**
> - Primary namespacing: [ A | B | C ]
> - If A: UUID or slug in URL? [ uuid | slug (with history-alias) ]
> - If A: legacy aliases kept forever, or deprecated after N releases?
>   [ forever | deprecate after version ___ ]

---

## Q3 — Pushover routing ✅ DECIDED

**Per-site configuration**, with the "Use Main's settings" inheritance
checkbox (see the "Site Main" section at the top of this doc).

- Each site has its own `pushover.user_key`, `pushover.api_token`,
  `pushover.enabled`.
- Non-Main sites see a checkbox: "Use [Main site's name]'s Pushover
  settings." When checked, the three inputs are disabled and the stored
  values are `NULL`. Collector resolves them at send time by falling back
  to Main's values.
- Notification message includes the site name so a shared Pushover device
  can still tell which site fired:
  `"pihole142 (Cabin) is offline"` / `"pihole1 (Home Base) is offline"`.

### Migration

- Existing global `pushover.*` keys → become Main (Default) site's values.
- New sites start with the inheritance checkbox **checked** (so they inherit
  Main's config and "just work" the day they're added). User can uncheck
  and configure their own if needed.

---

## Q4 — Sync scheduling ✅ DECIDED

**Option B — one APScheduler job per site, independent cadences.** This
follows directly from Q1: sync schedule is per-site → each site's schedule
is its own timer.

- On startup, for each site: register an `IntervalTrigger` job running
  that site's `run_sync(site_id)`.
- When a site's `sync.schedule_minutes` is updated via the settings UI,
  reschedule that one job (don't restart the whole scheduler).
- When a site is added/removed via YAML reload, add/remove just that site's
  job.
- The **master-blocklist-delta auto-sync trigger** (`Master blocklist count
  changed X → Y; triggering auto-sync.`) fires per-site, compared against
  each site's master's last-seen blocklist count. Same logic, N times,
  isolated state per site.
- **Failure isolation:** a hang on Cabin's sync doesn't delay Home's next
  run. Each `run_sync(site_id)` is its own async task.

---

## Q5 — Web UI behavior ✅ DECIDED

**Primary UX: site dropdown in the header**, switches the whole UI to the
selected site. Dashboard, queries, settings all scope to the chosen site.

- Default selection on first load after migration: Main site.
- Selection persists across page loads via path, not cookie — bookmarkable
  per-site URLs (assumes Q2 = A, which your bookmarkability preference
  supports).
- Settings page works on one site at a time (per Q1).

**Bookmarkability is not a security risk** — URLs protect nothing on their
own. Every endpoint is still behind JWT/API-key auth; a bookmark without
auth gets a 401. Sharing `/dashboard/home` in a chat is no worse than
sharing `mypi.example.com`; an unauthenticated recipient sees the login
page, not the data.

### Deferred to "maybe we do it" list

**"All Sites" combined view** — e.g. an "All Sites" entry at the top of the
dropdown that renders a merged dashboard with a site column on every
chart/table. Not in v1. Tracked as a future enhancement once the per-site
UX ships and we see whether it's actually wanted.

---

## iOS side (informational)

Minimal changes, *assuming* we pick Q2 option A:

- `GET /api/sites` returns the list of sites exposed by a given MyPi server.
- `Site.swift` in iOS stays as-is (it's still "one MyPi server").
- A new `BackendSite` concept (sub-picker) lives under it — either an
  attribute on `Site` ("which backend site is this pointing at?") or a
  secondary picker in the UI.
- `APIClient` prepends `/api/sites/{backend_site_id}` to every request.
- Server returning only one site (`Default`) → iOS hides the sub-picker → old
  deployments look identical to pre-multisite.

We'll flesh this out once Q1–Q5 are answered; it's small and downstream.

---

## Not in scope for this doc

- Migration file contents — will be written once decisions are locked.
- Exact route handler signatures — straightforward once Q2 is decided.
- Performance — 10 sites × 10 instances = 100 instances max, the collector
  already handles that kind of fan-out per poll tick. No new performance
  work expected.
- Auth/permissions — all users see all sites for now. If per-site ACLs are
  wanted later, that's a separate design.

---

## Locked decisions summary

| # | Decision |
|---|---|
| Back-compat YAML | Detect `sites:` key → new path; else wrap flat `instances:` as implicit `Default` site. |
| Site Main | One site flagged `main: true`; inheritance via NULL in per-site settings. |
| Inheritance scope | Broad — every per-site setting supports "Use Main's settings" checkbox. |
| Main deletion | Auto-promote first remaining active site in YAML order; materialize old-Main's resolved values into new Main. |
| Orphan cleanup | Mirror orphan-pihole pattern: mark `is_active=FALSE`, manual cleanup UI, cascade delete with row-count confirm. |
| Q1 — settings scope | All per-site; global exceptions are infra only (JWT TTL, DB URL, encryption key, API key records). |
| Q2 — API namespace | Path prefix `/api/sites/{slug}/...` with slug, legacy un-prefixed routes kept forever as Main aliases. |
| Q3 — Pushover | Per-site with "Use Main's settings" checkbox; message includes site name. |
| Q4 — Sync scheduler | One APScheduler job per site, independent cadences, per-site master-blocklist-delta trigger. |
| Q5 — Web UI | Site dropdown in header, bookmarkable per-site URLs (`/dashboard/{slug}`), "All Sites" merged view deferred to "maybe" list. |

## Next

1. `docs/multisite-migration-plan.md` — Alembic schema changes and backfill.
2. `docs/multisite-implementation-plan.md` — phased code order.
3. Only then: code, still on this branch.
