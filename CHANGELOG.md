# Changelog

All notable changes to MyPi are documented here.

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
