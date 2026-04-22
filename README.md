# MyPi
[![build](https://img.shields.io/github/actions/workflow/status/theojamesvibes/mypi/docker-publish.yml?style=flat-square)](https://github.com/theojamesvibes/mypi/actions)
[![version](https://img.shields.io/badge/version-1.9.3-blue?style=flat-square)](https://github.com/theojamesvibes/mypi)
[![platform](https://img.shields.io/badge/platform-linux%2Famd64%20|%20linux%2Farm64-teal?style=flat-square)](https://github.com/theojamesvibes/mypi/pkgs/container/mypi)

> **⚠️ Vibe Code Disclosure**
> This project was generated entirely through AI-assisted development (Claude Code / Anthropic). The code has been reviewed and iterated on collaboratively, but it has not been audited for production use. Deploy on trusted local networks only, review the code before relying on it, and proceed with the usual amount of healthy scepticism you'd apply to any AI-generated codebase.
>
> **Independent reviews.** Two LLM-based audits have been applied as part of the `hardening-review` branch — by **Google Gemini** (adversarial security review) and **Grok** (full architecture + security review). Both are documented in [Independent reviews](#independent-reviews) below, along with what was verified as already-correct and what was changed in response. These reviews are not a substitute for a professional audit.

A self-hosted dashboard that consolidates up to 10 locally running [Pi-hole](https://pi-hole.net/) v6 instances into a single screen, paired with a REST API designed for iOS app consumption.

---

## Screenshots

<img width="3837" height="1963" alt="mypi 1 1 2 mainscreen" src="https://github.com/user-attachments/assets/8d9716ab-a1b0-43cd-9777-788d4ef76c3f" />
<img width="3832" height="1958" alt="mypi 1 1 2 settings" src="https://github.com/user-attachments/assets/a1701726-dd90-4f99-8b39-bb002fe28d51" />


---

## Features

### Dashboard
- **Unified stat cards** — Total queries, blocked count, % blocked, and domains on blocklist aggregated across all instances; blocklist card turns red if instances disagree
- **Stat card footer links** — each card has a Pi-hole–style footer: unique client count (→ Unique Clients view), List blocked queries, List all queries, Manage lists (→ master Pi-hole admin in new tab)
- **Time range selector** — Last 15 minutes, Last 1 hour, Today, Last 24 hours (default), 48 hours, 7 days, 30 days; chart bucket granularity scales automatically with the window
- **DNS Queries over Time chart** — configurable bucket size per time range, from actual query log data (not cumulative counters)
- **Query type breakdown** — Doughnut chart (Forwarded / Cached / Blocked / Other)
- **Per-system panel** — Each Pi-hole shown individually with its own stats and online/offline badge
- **Top Permitted Domains, Top Blocked Domains, Top Clients** — Clickable rows open a drill-down modal with all matching queries for that domain or client

### Query Log
- Consolidated query log across all instances with instance badge per row
- Column sorting (click any header), pagination, and a **Live View** toggle that refreshes every 2 seconds
- Filter by instance, domain, client, blocked/permitted status, and time range
- **Block / Unblock domains** — every row has an inline button: blocked-status queries show **Unblock**, all others show **Block**. One click adds or removes the domain from the master Pi-hole's exact deny list and triggers a gravity sync to all replicas in the background. The button toggles in-place without a page reload.
- **Unique Clients view** — Show dropdown option that switches to a per-client aggregate table (total queries, blocked count, % blocked, last seen); accessible directly from the dashboard stat card
- Deep-link URL params: `/queries?blocked=true`, `/queries?blocked=false`, `/queries?show=clients` pre-set the Show filter automatically
- Last-updated timestamp shown in the topbar after each refresh

### Pi-hole Sync
- Push full configuration from a designated **master** Pi-hole to all replicas via the Pi-hole v6 teleporter API
- Sync order: master runs gravity first (fetches fresh blocklists) → exports teleporter zip → replicas import → replicas run gravity
- Selectable import options: configuration settings, gravity (adlists/blocklists/domains/clients), DHCP leases
- Configurable automatic sync interval: 15 min / 30 min / 1 hr / 6 hr / 24 hr, or manual-only
- **Auto-sync on gravity change** — detects when the master's blocklist count changes and triggers an immediate sync
- Schedule and last sync result persist across container restarts (stored in PostgreSQL)
- Dashboard shows **"Pi synced: \<time\>"** whenever a sync has run; time turns red if last sync was more than 24 hours ago

### Pushover Notifications
- Push alerts to any device via [Pushover](https://pushover.net) (iOS, Android, desktop)
- Configurable alerts: sync failure, instance offline/back online, high block rate
- **High block rate** alert requires ≥7 days of data to establish a baseline before firing
- No-logs and block-rate thresholds are configurable in Settings
- Credentials (App Token + User Key) stored encrypted in PostgreSQL, survive restarts
- Validate credentials and send a test notification directly from the Settings page

### Settings
- **Appearance** — Light / Dark / System theme selector; preference stored in the browser and applied before first paint (no theme flash); Dark mode covers all UI surfaces including charts
- API key management (create / revoke) for iOS app authentication
- Instance list showing all active Pi-hole instances with online/offline badge, master indicator, and clickable URL links (open Pi-hole web UI in new tab)
- **Orphaned instance cleanup** — when an instance is renamed or removed from `pihole_instances.yml`, the old record is detected and shown with an option to permanently remove it along with all associated stats and query log data, individually or in bulk
- **Software versions** — Pi-hole (core), FTL, and web interface versions shown as columns in the instances table; fetched on each stats poll and persisted to the database so they survive restarts; color-coded green (up to date) or red (update available)
- Sync panel: import options, schedule configuration, live sync result with per-replica status
- Session Timeout panel: configure how long the web UI session stays active
- Pushover panel: credentials, master enable toggle, per-alert toggles, thresholds
- **Version Check panel** — shows running vs latest version, last check time; version badge in topbar turns green/red; checks GitHub once per hour (can be disabled)

### API & Auth
- Full REST API under `/api/` with auto-generated OpenAPI docs (Swagger UI at `/docs`, ReDoc at `/redoc` — opt-in via `ENABLE_API_DOCS=true`)
- Username/password login for the web UI (JWT session cookie)
- API key auth (`X-API-Key` header) for mobile clients and automation
- **Read-only API keys** — mark a key read-only on creation; the `require_mutation` dependency rejects it from every mutation endpoint with `403`. Session cookies and bearer JWTs are always full-access
- Version badge in topbar is green (up to date) or red (update available), links to GitHub releases

### Security & Hardening
- **Security headers on every response** — `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: same-origin`, a Content-Security-Policy that denies framing and arbitrary external script origins, and `Strict-Transport-Security` when `SECURE_COOKIES=true`
- **Rate limits on mutation endpoints** — `/api/sync` (10/min), `/api/domains/{deny,allow}` (30/min each), `/api/notifications/test` (5/min), `/api/notifications/validate` (10/min), `/api/auth/change-password` (5/min)
- **Audit logging on mutations** — every mutation handler logs `user=<username>` plus the action and target, so the application log doubles as an audit trail
- **Encrypted secrets at rest** — Pi-hole API passwords and Pushover credentials are Fernet-encrypted in PostgreSQL using `ENCRYPTION_KEY`
- **Optional split secrets** — `JWT_SECRET_KEY` and `API_KEY_SALT` let you rotate JWT signing keys without invalidating API keys (or vice versa); both fall back to `SECRET_KEY` when unset
- **Per-instance circuit breaker** — a flap-prone Pi-hole is absorbed locally: after N consecutive connection failures, polling for that instance is suspended for a cooldown window, then one probe either closes the breaker or re-arms it. Defaults: 3 failures, 300 s cooldown, 2 s dedup (stats+queries share one connection). Tunable via `CIRCUIT_FAIL_THRESHOLD` / `CIRCUIT_COOLDOWN_SECONDS` / `CIRCUIT_DEDUP_SECONDS`
- **Container runs as non-root** (UID 1000, no shell); image ships with a `HEALTHCHECK` against `/api/health`
- **Dependabot** on a weekly schedule covers `pip`, `github-actions`, and `docker` ecosystems

---

## Architecture

```
┌─────────────────────────────────────────────────┐
│  Pi-hole 1   Pi-hole 2   …   Pi-hole N          │
│  (Pi-hole v6, local network)                    │
└────────────────┬────────────────────────────────┘
                 │  HTTP or HTTPS (Pi-hole v6 REST API)
                 ▼
┌────────────────────────────────────────────────────────┐
│                    MyPi (Docker)                       │
│                                                        │
│  ┌─────────────┐   ┌────────────────────────────────┐ │
│  │  APScheduler│   │  FastAPI                       │ │
│  │  poll stats │──▶│  • Web UI (Jinja2 / Bootstrap) │ │
│  │  poll queries    │  • REST API (/api/*)           │ │
│  │  sync service    └──────────────┬────────────────┘ │
│  └─────────────┘                  │                   │
│                    ┌──────────────▼──────────────┐    │
│                    │  PostgreSQL 18               │    │
│                    │  stats · queries · users     │    │
│                    │  settings · sync schedule    │    │
│                    └─────────────────────────────┘    │
└────────────────────────────────────────────────────────┘
                 │  REST API (JWT / API key)
                 ▼
           iOS App (coming soon)
```

### Tech Stack

| Layer | Technology |
|---|---|
| Web / API framework | **FastAPI** + Uvicorn |
| Database | **PostgreSQL 18** |
| ORM / migrations | **SQLAlchemy 2.0 async** + Alembic |
| Background polling | **APScheduler** (in-process) |
| HTTP client | **httpx** (async) |
| Auth | **python-jose** (JWT) + **passlib** (bcrypt) |
| Frontend | **Jinja2** + **Bootstrap 5** + **Chart.js** |
| Config | **PyYAML** + **pydantic-settings** |

---

## Getting Started

### Prerequisites

- Docker + Docker Compose
- Pi-hole v6 instances running on your local network

No need to clone the repository. The app is published as a pre-built image on the GitHub Container Registry.

### 1. Download the config files

```bash
mkdir mypi && cd mypi

# Docker Compose
curl -fsSL https://raw.githubusercontent.com/theojamesvibes/mypi/main/docker-compose.yml -o docker-compose.yml

# Environment variables template
curl -fsSL https://raw.githubusercontent.com/theojamesvibes/mypi/main/.env.example -o .env

# Pi-hole instances template
curl -fsSL https://raw.githubusercontent.com/theojamesvibes/mypi/main/pihole_instances.yml.example -o pihole_instances.yml
```

### 2. Configure

Edit `.env` — at minimum set these two values:

```bash
POSTGRES_PASSWORD=change-me
SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")
```

Edit `pihole_instances.yml` with your Pi-hole URLs and passwords (see [Configuration](#configuration) below). Instances with no password set are supported — leave `password` empty or omit it.

### 3. Run

```bash
docker compose up -d
```

> **Important — stop timeouts:** The provided `docker-compose.yml` sets `stop_grace_period` on both services (`60s` for Postgres, `30s` for the app). Do not remove these. Without them Docker sends `SIGKILL` after only 10 seconds, which can interrupt a Postgres checkpoint mid-write and corrupt the database — requiring a table rebuild to recover.

> **Important — passing environment variables to the container:** Compose's `.env` file is used for `${VAR}` substitution inside `docker-compose.yml`, **not** automatically injected into the container. The security-relevant vars (`SECURE_COOKIES`, `VERIFY_PIHOLE_SSL`, `ENABLE_API_DOCS`, `JWT_SECRET_KEY`, `API_KEY_SALT`, `ENCRYPTION_KEY`) must either be listed explicitly under `environment:` **or** passed through with `env_file:`. If you customise the compose file, the safest pattern is:
>
> ```yaml
>   app:
>     image: ghcr.io/theojamesvibes/mypi:latest
>     env_file:
>       - .env                    # pass every var from .env into the container
>     environment:
>       # keep only values that need compose-level substitution or validation
>       DATABASE_URL: postgresql+asyncpg://${POSTGRES_USER:-mypi}:${POSTGRES_PASSWORD}@db:5432/${POSTGRES_DB:-mypi}
>       SECRET_KEY: ${SECRET_KEY:?SECRET_KEY is required}
>       PIHOLE_CONFIG_PATH: /app/pihole_instances.yml
> ```
>
> Without `env_file:`, setting `SECURE_COOKIES=true` in `.env` has no effect — cookies will be issued without the `Secure` flag and HSTS will not be sent, even behind a TLS reverse proxy.

Docker pulls the pre-built image automatically. The dashboard is available at **http://localhost:8080** (or whichever `APP_PORT` you set in `.env`).

Log in with username `admin` (or `INITIAL_ADMIN_USER` if overridden) and password `changeme`. You will be required to set a new password immediately before accessing the dashboard.

---

### Building from source

```bash
git clone https://github.com/theojamesvibes/mypi.git && cd mypi
cp .env.example .env && cp pihole_instances.yml.example pihole_instances.yml
# edit both files, then:
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d
```

---

## Configuration

### Environment variables (`.env`)

| Variable | Default | Description |
|---|---|---|
| `POSTGRES_PASSWORD` | *(required)* | PostgreSQL password |
| `SECRET_KEY` | *(required)* | JWT signing secret — generate with `secrets.token_hex(32)` |
| `INITIAL_ADMIN_USER` | `admin` | Username for the first admin account — password is always `changeme` and must be changed on first login |
| `APP_PORT` | `8080` | Port to expose the dashboard on |
| `STATS_POLL_INTERVAL` | `60` | Seconds between stats polls |
| `QUERIES_POLL_INTERVAL` | `10` | Seconds between query log polls |
| `DATA_RETENTION_DAYS` | `30` | Days of history to retain |
| `SECURE_COOKIES` | `false` | Adds the `Secure` flag to session cookies and turns on HSTS. Enable when MyPi is behind a TLS-terminating reverse proxy. |
| `VERIFY_PIHOLE_SSL` | `false` | Verify TLS certificates when connecting to Pi-hole instances. Leave `false` for self-signed certs on a trusted LAN. |
| `ENABLE_API_DOCS` | `false` | Expose Swagger UI at `/docs`, ReDoc at `/redoc`, and the OpenAPI schema at `/openapi.json`. Default flipped to `false` in 1.8.0 — fail-closed posture. Set to `true` when you actively need the docs (local development, regenerating an iOS OpenAPI client, etc.). |
| `JWT_SECRET_KEY` | *(falls back to `SECRET_KEY`)* | Separate signing key for session JWTs. Set alongside `API_KEY_SALT` if you want to rotate one without invalidating the other. |
| `API_KEY_SALT` | *(falls back to `SECRET_KEY`)* | Separate HMAC salt for stored API keys. |
| `ENCRYPTION_KEY` | *(auto-generated on first boot)* | Fernet key used to encrypt Pi-hole API passwords and Pushover credentials at rest. Auto-generated and persisted to the database if unset; pin it in `.env` for portability. |
| `CIRCUIT_FAIL_THRESHOLD` | `3` | Consecutive connection failures against one Pi-hole before the per-instance circuit breaker trips. |
| `CIRCUIT_COOLDOWN_SECONDS` | `300` | How long polling is suspended for a tripped instance before the next probe. |
| `CIRCUIT_DEDUP_SECONDS` | `2.0` | Window in which a stats+queries failure on the shared connection counts as one event. |
| `AUTH_BACKOFF_SECONDS` | `300` | How long to pause `/api/auth` attempts after a `429 Too Many Requests` from a Pi-hole. Lower it if your fleet has a higher `webserver.api.max_sessions` and a 5-minute blackout is too long. |

#### Circuit-breaker tuning profiles

The breaker defaults (`3 / 300 / 2.0`) are tuned for a stable home/lab fleet. If your environment is lossier or more sensitive, the three knobs compose into these rough profiles:

| Profile | `CIRCUIT_FAIL_THRESHOLD` | `CIRCUIT_COOLDOWN_SECONDS` | `CIRCUIT_DEDUP_SECONDS` | When to pick it |
|---|---|---|---|---|
| **Aggressive** | `2` | `120` | `2.0` | You want a flappy Pi-hole isolated quickly and probed back in sooner. Expect more transition-alert noise. |
| **Stable** *(default)* | `3` | `300` | `2.0` | Three independent failure events before isolation; 5-minute cooldown. Balanced for most fleets. |
| **Lenient** | `5` | `600` | `3.0` | Tolerates more transient blips before isolating; useful on a noisy LAN or WAN-reachable Pi-holes. Slower to surface a real outage. |

These scale **independently** of `STATS_POLL_INTERVAL` / `QUERIES_POLL_INTERVAL` — transport instability isn't proportional to how often you check for it, so the breaker values are deliberately not auto-scaled with the poll cadence.

### Pi-hole instances (`pihole_instances.yml`)

```yaml
instances:
  - name: "Living Room"
    url: "http://192.168.1.100"
    password: "your-pihole-password"
    color: "#00a65a"   # shown in charts
    master: true       # sync source — exactly one instance should be master

  - name: "Office"
    url: "http://192.168.1.101"
    password: "your-pihole-password"
    color: "#3c8dbc"

  - name: "Bedroom"
    url: "http://192.168.1.102"
    password: "your-pihole-password"
    color: "#f39c12"

  # Pi-hole with no password set — omit the field or leave it empty:
  - name: "Garage"
    url: "http://192.168.1.103"
    password: ""
    color: "#9b59b6"

  # HTTPS with a self-signed certificate — just use https:// in the URL:
  - name: "Secure Pi"
    url: "https://192.168.1.104"
    password: "your-pihole-password"
    color: "#e74c3c"

  # Dormant VIP secondary (hot spare) — fully online, no DNS traffic:
  - name: "Standby"
    url: "http://192.168.1.105"
    password: "your-pihole-password"
    color: "#95a5a6"
    hot_spare: true
```

- Up to **10 instances** supported
- Instances are loaded at startup and synced to the database; restart the container to pick up changes
- `password` is the Pi-hole web interface password (Pi-hole v6 API). Leave empty (`""`) or omit entirely for instances with no password configured — MyPi detects the passwordless state automatically and connects without authentication. **Note:** passwordless mode only works for plain-`http://` Pi-holes. With TLS enabled, Pi-hole v6's `/api/auth` rejects empty-password requests with 401 even when the web UI has no password set, so any `https://` instance must have a password configured
- `color` is used in charts to distinguish each instance visually
- `master: true` designates the sync source for the Pi-hole Sync feature — exactly one instance should be marked master
- `hot_spare: true` marks a replica as a dormant VIP secondary (see [Low-traffic or hot-standby Pi-holes](#low-traffic-or-hot-standby-pi-holes) below). Cannot be combined with `master: true` — MyPi logs a warning and ignores `hot_spare` if both are set on the same instance

#### HTTP vs HTTPS

Both `http://` and `https://` URLs are supported. If your Pi-hole is configured with HTTPS (including a self-signed certificate), simply use `https://` in the `url` field — MyPi accepts self-signed certificates without any additional configuration. Using HTTPS is recommended when your Pi-hole is reachable over a network segment you don't fully control.

#### Low-traffic or hot-standby Pi-holes

Pi-holes that sit behind a VIP as a standby, or otherwise receive very little DNS traffic, can occasionally flap in MyPi's view. Pi-hole v6's embedded webserver (CivetWeb) closes idle keepalive TCP sockets on a short timeout; between 60 s stats polls the socket can go stale, so the next poll's first request fails (`ConnectError` / `RemoteProtocolError`) before MyPi reconnects. This is not a Pi-hole-side block — session counts and `webserver.api.max_sessions` are not involved, and Pi-hole v6 does not expose a CivetWeb keepalive knob.

MyPi mitigates this on **three** layers:

1. **Self-heal on the stats/queries polls** — the collector evicts the persistent client on `ssl.SSLError` / `httpx.ConnectError` / `httpx.RemoteProtocolError` and the next poll opens a fresh connection.
2. **Self-heal on the sync path** (added in 1.9.0) — `_sync_replica` evicts and retries the teleporter POST once on the same error classes. Before 1.9.0, a single half-open socket during sync would fail the replica outright and fire a Pushover.
3. **Per-instance circuit breaker** — 3 consecutive failures → 5 min cooldown on that instance (tunable via `CIRCUIT_FAIL_THRESHOLD` / `CIRCUIT_COOLDOWN_SECONDS`).

> **Note on `max_keepalive_connections=2`.** The HTTP client is deliberately configured with keepalive enabled (`max_keepalive_connections=2` in `app/services/pihole_client.py::open`) rather than forced-close. The 1.7.6 release briefly ran `max_keepalive_connections=0`, which traded slightly fewer half-opens for **two TLS handshakes per minute per instance**. On slow hardware (specifically a Raspberry Pi 3 running civetweb/mbedTLS) this periodically wedged FTL's TLS session table, producing recurring `SSLV3_ALERT_HANDSHAKE_FAILURE` episodes. Re-enabling keepalive in dev.10 fixed it. The self-heal layers above exist to absorb the residual idle-close symptom without paying that handshake tax — do not disable keepalive without re-reading the `hardening-review` CHANGELOG first.

If flaps still reach you after that, two levers:

- **Mark the replica as `hot_spare: true` in `pihole_instances.yml`** — sync-failure Pushover is suppressed for hot spares if *all* failing replicas in the sync are hot spares; a mixed primary/spare failure still pages. The stats-poll offline alert is NOT suppressed — a hot spare going genuinely unreachable still fires the normal offline notification (adjustable via the offline-alert retry count).
- **Raise the offline-alert retry count** in **Settings → Notifications** so a single cooldown window (≈5 missed polls) can't on its own trigger Pushover. A value of 8–10 turns the breaker into a silent absorber for transient idle-socket flaps while still paging on a real outage.
- **Lengthen `STATS_POLL_INTERVAL` / `QUERIES_POLL_INTERVAL`** for the whole fleet if idle-socket flaps are widespread — less useful when only a subset of instances are low-traffic, since it slows recovery detection for everyone.

---

## Pi-hole Sync

MyPi can push the full Pi-hole configuration from a master instance to all replicas using the Pi-hole v6 teleporter API (the same mechanism as Pi-hole's built-in backup/restore).

**Sync order:**
1. Master runs `gravity update` — pulls the latest blocklists
2. Master exports a teleporter ZIP (contains gravity database, config, adlists, etc.)
3. All replicas import the ZIP in parallel
4. Replicas run `gravity update` to finalize

**Configure in Settings → Pi-hole Sync:**
- Choose what to include: configuration, gravity, DHCP leases
- Set an automatic interval or leave as manual-only
- Enable auto-sync on gravity change to react immediately when the master's blocklist is updated

The sync schedule and last sync result are stored in the database and survive container restarts. The dashboard displays the last sync time; it turns red if more than 24 hours have elapsed since the last successful sync.

---

## REST API

The full API is available under `/api/`. Interactive documentation is at **`/docs`** (Swagger UI) and **`/redoc`** when `ENABLE_API_DOCS=true` is set in `.env` (default is `false` since 1.8.0-dev.7).

### Authentication

| Method | Header | Use case |
|---|---|---|
| JWT Bearer | `Authorization: Bearer <token>` | After `/api/auth/login` |
| API Key | `X-API-Key: <key>` | iOS app / automation |

### Key endpoints

```
# Auth
POST    /api/auth/login                  # { "username": "...", "password": "..." } → JWT token
POST    /api/auth/logout                 # Clear session cookie
GET     /api/auth/me                     # Current user info
POST    /api/auth/api-key                # Create API key (requires JWT)
GET     /api/auth/api-keys               # List active API keys
DELETE  /api/auth/api-key/{id}           # Revoke an API key

# Stats
GET     /api/stats/summary               # Aggregated + per-instance stats
GET     /api/stats/history               # Over-time query data (?hours=24)
GET     /api/stats/top                   # Top domains and clients (?hours=24&limit=10)

# Query log
GET     /api/queries                     # Paginated, filterable, sortable query log
                                         # ?page, page_size, instance_id, domain, client,
                                         #  blocked, hours, sort_by, sort_dir

# Domain blocking
POST    /api/domains/block               # { "domain": "example.com" } — add to master exact deny list + sync
DELETE  /api/domains/block/{domain}      # Remove domain from master exact deny list + sync

# Instances
GET     /api/instances                   # Active instance list with latest stats, status, and software versions
GET     /api/instances/stale             # Orphaned instances (removed from YAML, not yet deleted)
DELETE  /api/instances/{id}              # Permanently delete an orphaned instance and its data

# Sync
GET     /api/sync/status                 # Last sync state (idle / running / success / error)
POST    /api/sync                        # Trigger a sync (runs in background)
GET     /api/sync/schedule               # Get sync schedule settings
PUT     /api/sync/schedule               # Update sync schedule settings

# Notifications
GET     /api/notifications/settings      # Get Pushover settings (credentials masked)
PUT     /api/notifications/settings      # Save Pushover settings
POST    /api/notifications/test          # Send a test Pushover notification
POST    /api/notifications/validate      # Validate App Token + User Key with Pushover API
```

---

## Database Schema

```
users              — dashboard login accounts
api_keys           — iOS app / API client keys
pihole_instances   — Pi-hole instance registry (from YAML); is_master flag
stats_snapshots    — Periodic stats snapshots (one per instance per poll)
query_logs         — Consolidated DNS query log entries (30-day retention)
app_settings       — Key/value store for persisted app config:
                       sync_schedule        sync interval + import options
                       sync_last_result     last sync outcome + per-replica status
                       pushover_settings    Pushover credentials + alert toggles + thresholds
```

---

## Project Structure

```
mypi/
├── docker-compose.yml
├── Dockerfile
├── VERSION                        # Single source of truth for version number
├── requirements.txt
├── .env.example
├── pihole_instances.yml.example
├── alembic.ini
├── alembic/
│   └── versions/
│       ├── 0001_initial_schema.py
│       ├── 0002_add_session_sid.py
│       ├── 0003_add_is_master.py
│       └── 0004_app_settings.py
└── app/
    ├── main.py               # FastAPI app entry point + scheduler lifecycle
    ├── config.py             # Settings (pydantic-settings + YAML loader)
    ├── database.py           # Async SQLAlchemy engine
    ├── auth.py               # JWT + API key auth helpers
    ├── models/               # SQLAlchemy ORM models
    ├── schemas/              # Pydantic request/response schemas
    ├── api/                  # FastAPI route handlers
    │   ├── auth.py
    │   ├── domains.py        # Block/unblock domain endpoints
    │   ├── instances.py
    │   ├── notifications.py  # Pushover settings + test/validate endpoints
    │   ├── queries.py
    │   ├── stats.py
    │   └── sync.py
    ├── services/
    │   ├── pihole_client.py  # Pi-hole v6 REST API client (teleporter support)
    │   ├── client_manager.py # Shared persistent client registry (one session per instance)
    │   ├── collector.py      # APScheduler background jobs + offline alerts
    │   ├── config_loader.py  # YAML → DB sync
    │   ├── pushover.py       # Pushover notification service
    │   └── sync_service.py   # Pi-hole config sync (master → replicas)
    ├── static/               # CSS + JS
    └── templates/            # Jinja2 HTML templates
```

---

## Pi-hole Compatibility

MyPi targets **Pi-hole v6** which introduced a new REST API. It uses the following endpoints:

- `POST /api/auth` — password authentication
- `GET /api/stats/summary` — aggregated stats
- `GET /api/stats/history` — over-time data
- `GET /api/stats/top_domains` — top domains
- `GET /api/stats/top_clients` — top clients
- `GET /api/queries` — paginated query log
- `GET /api/info/version` — installed + latest-available version info (core / FTL / web)
- `GET /api/teleporter` — export configuration ZIP (sync)
- `POST /api/teleporter` — import configuration ZIP (sync)
- `POST /api/action/gravity` — trigger gravity update (sync)
- `POST /api/domains/deny/exact` — add a domain to the exact deny list (block)
- `DELETE /api/domains/deny/exact/{domain}` — remove a domain from the exact deny list (unblock)

Pi-hole v5 (the `api.php` interface) is **not** supported.

---

## Development

```bash
# Run locally without Docker (requires a running PostgreSQL instance)
pip install -r requirements.txt

export DATABASE_URL="postgresql+asyncpg://mypi:mypi@localhost:5432/mypi"
export SECRET_KEY="dev-secret-key"

alembic upgrade head
uvicorn app.main:app --reload --port 8080
```

---

## Roadmap

- [x] Web dashboard (Pi-hole look-alike)
- [x] Per-system stats panel
- [x] Consolidated query log with sorting, filtering, and live view
- [x] REST API with OpenAPI docs
- [x] JWT + API key authentication
- [x] Traefik reverse-proxy integration
- [x] Pi-hole config sync (master → replicas via teleporter API)
- [x] Configurable auto-sync schedule and gravity-change detection
- [x] Drill-down modals on top blocked domains and top clients
- [x] Pushover push notifications (sync failure, offline, high block rate)
- [x] Topbar sync status badge (green/yellow/red) on every page
- [x] Dark / Light / System theme with no flash on load
- [x] Block / Unblock domains from the query log (master deny list + replica sync)
- [ ] iOS app

---

## Changelog

See [CHANGELOG.md](CHANGELOG.md) for the full version history.

---

## Independent reviews

MyPi has been reviewed by two external LLM-based audits as part of the `hardening-review` branch. Each review was triaged by Claude Code against the live source: items already correct were recorded as verified, items with real gaps were fixed. Neither review is a substitute for a professional audit.

### Google Gemini — adversarial security review (2026-04-19)

Focused on four areas. Outcome:

| Gemini ask | Outcome |
|---|---|
| **Teleporter atomic operations** — does a mid-broadcast failure leave a replica broken? | Pi-hole v6's `/api/teleporter` is a single commit point with no server-side staging, so upload-then-swap is not possible on the Pi-hole side. Addressed on the MyPi side: the master's ZIP export is now validated client-side (min size, valid archive, per-member CRC, non-empty members) before any replica receives it — see `sync_service._validate_teleporter_zip`. |
| **Error isolation in polling** — does one slow Pi-hole block the others? | Verified correct. `collector.poll_stats` / `poll_queries` / `fetch_all_instance_versions` all `asyncio.gather(*[...])` over per-instance coroutines, each wrapped in `try/except Exception`. |
| **Sensitive data exposure** — can a token leak to logs, API responses, or the frontend? | Verified correct. `PiholeInstance.api_password` uses Fernet encryption at rest (`EncryptedString`, migration `0006`). No API response schema exposes passwords. Every log call uses `self.base_url` or `instance.name`, never the password. |
| **Dependency / Docker hardening** — pinning and non-root runtime. | `requirements.txt` was already fully pinned with `==`. Container now runs as a non-root user (UID 1000) and has a `HEALTHCHECK` against `/api/health`. |

### Grok — full architecture and security review (2026-04-19)

Verified ~11 items as already correct (JWT cookie flags `HttpOnly` + `Secure`-when-TLS + `SameSite=Lax`; `SECRET_KEY` required at boot; bcrypt; rate limiting on mutation endpoints; encrypted Pushover creds; query-log indexes; etc.). Three meaningful gaps led to additional hardening in `1.8.0-dev.5`:

| Grok finding | Change |
|---|---|
| **`/openapi.json` exposed unauthenticated.** | Gated behind `ENABLE_API_DOCS`; default flipped to `false` in 1.8.0-dev.7 (fail-closed). Setting `ENABLE_API_DOCS=true` re-enables `/docs`, `/redoc`, and `/openapi.json` together. |
| **No Content-Security-Policy header.** | Added to the security-headers middleware on every response. Allows `cdn.jsdelivr.net` for Bootstrap / Chart.js / Swagger UI, `'unsafe-inline'` for the theme pre-paint script, and denies framing / arbitrary external script origins. |
| **No Dependabot configuration.** | Added `.github/dependabot.yml` on a weekly schedule covering `pip`, `github-actions`, and `docker`. |

---

## License

MIT

---

## Credits

- **[Pi-hole](https://pi-hole.net/)** — the network-wide ad blocker that makes all of this possible. MyPi is a companion dashboard for Pi-hole v6 and would not exist without the Pi-hole project and its community. If you find Pi-hole useful, consider [donating to the project](https://pi-hole.net/donate/).

- **[Nebula Sync](https://github.com/lovelaze/nebula-sync)** — inspired the Pi-hole sync design. Nebula Sync is a purpose-built tool for keeping multiple Pi-hole v6 instances in sync; MyPi's sync feature draws on the same approach of using the Pi-hole v6 teleporter API to push configuration from a master to replicas.

---

> **Vibe coded with [Claude Code](https://claude.ai/code) by Anthropic.**
> Use at your own risk. Review before trusting.
