# MyPi

> **⚠️ Vibe Code Disclosure**
> This project was generated entirely through AI-assisted development (Claude Code / Anthropic). The code has been reviewed and iterated on collaboratively, but it has not been audited for production use. Deploy on trusted local networks only, review the code before relying on it, and proceed with the usual amount of healthy scepticism you'd apply to any AI-generated codebase.

A self-hosted dashboard that consolidates up to 10 locally running [Pi-hole](https://pi-hole.net/) v6 instances into a single screen, paired with a REST API designed for iOS app consumption.

---

## Screenshots

> *(Add screenshots here once running)*

---

## Features

### Dashboard
- **Unified stat cards** — Total queries, blocked count, % blocked, and domains on blocklist aggregated across all instances; blocklist card turns red if instances disagree
- **DNS Queries over Time chart** — Bar chart per 10-minute bucket from actual query log data (not cumulative counters)
- **Query type breakdown** — Doughnut chart (Forwarded / Cached / Blocked / Other)
- **Per-system panel** — Each Pi-hole shown individually with its own stats and online/offline badge
- **Top Permitted Domains, Top Blocked Domains, Top Clients** — Clickable rows open a drill-down modal with all matching queries for that domain or client

### Query Log
- Consolidated query log across all instances with instance badge per row
- Column sorting (click any header), pagination, and a **Live View** toggle that refreshes every 2 seconds
- Filter by instance, domain, client, blocked/permitted status, and time range
- Last-updated timestamp shown in the topbar after each refresh

### Pi-hole Sync
- Push full configuration from a designated **master** Pi-hole to all replicas via the Pi-hole v6 teleporter API
- Sync order: master runs gravity first (fetches fresh blocklists) → exports teleporter zip → replicas import → replicas run gravity
- Selectable import options: configuration settings, gravity (adlists/blocklists/domains/clients), DHCP leases
- Configurable automatic sync interval: 15 min / 30 min / 1 hr / 6 hr / 24 hr, or manual-only
- **Auto-sync on gravity change** — detects when the master's blocklist count changes and triggers an immediate sync
- Schedule and last sync result persist across container restarts (stored in PostgreSQL)
- Dashboard shows **"Pi synced: \<time\>"** whenever a sync has run; time turns red if last sync was more than 24 hours ago

### Settings
- API key management (create / revoke) for iOS app authentication
- Instance list with online/offline badge and master indicator
- Sync panel: import options, schedule configuration, live sync result with per-replica status

### API & Auth
- Full REST API under `/api/` with auto-generated OpenAPI docs (Swagger UI at `/docs`, ReDoc at `/redoc`)
- Username/password login for the web UI (JWT session cookie)
- API key auth (`X-API-Key` header) for mobile clients and automation
- Version number displayed in the topbar for quick at-a-glance identification

---

## Architecture

```
┌─────────────────────────────────────────────────┐
│  Pi-hole 1   Pi-hole 2   …   Pi-hole N          │
│  (Pi-hole v6, local network)                    │
└────────────────┬────────────────────────────────┘
                 │  HTTP (Pi-hole v6 REST API)
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

### 1. Clone and configure

```bash
git clone <this-repo> mypi
cd mypi

# Environment variables
cp .env.example .env
# Edit .env — at minimum set POSTGRES_PASSWORD, SECRET_KEY, INITIAL_ADMIN_PASSWORD

# Pi-hole instances
cp pihole_instances.yml.example pihole_instances.yml
# Edit pihole_instances.yml with your Pi-hole URLs and passwords
```

### 2. Generate a secret key

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
# Paste the output into SECRET_KEY in .env
```

### 3. Run

```bash
docker compose up -d
```

The dashboard is available at **http://localhost:8080** (or whichever `APP_PORT` you set). If you have Traefik on a `proxy` network, uncomment the labels in `docker-compose.yml` and set your hostname.

Log in with the credentials from `INITIAL_ADMIN_USER` / `INITIAL_ADMIN_PASSWORD`.

---

## Configuration

### Environment variables (`.env`)

| Variable | Default | Description |
|---|---|---|
| `POSTGRES_PASSWORD` | *(required)* | PostgreSQL password |
| `SECRET_KEY` | *(required)* | JWT signing secret — generate with `secrets.token_hex(32)` |
| `INITIAL_ADMIN_PASSWORD` | *(required)* | Password for the first admin account |
| `INITIAL_ADMIN_USER` | `admin` | Username for the first admin account |
| `APP_PORT` | `8080` | Port to expose the dashboard on |
| `STATS_POLL_INTERVAL` | `60` | Seconds between stats polls |
| `QUERIES_POLL_INTERVAL` | `10` | Seconds between query log polls |
| `DATA_RETENTION_DAYS` | `30` | Days of history to retain |

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
```

- Up to **10 instances** supported
- Instances are loaded at startup and synced to the database; restart the container to pick up changes
- The `password` is the Pi-hole web interface password (Pi-hole v6 API)
- `color` is used in charts to distinguish each instance visually
- `master: true` designates the sync source for the Pi-hole Sync feature — exactly one instance should be marked master

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

The full API is available under `/api/`. Interactive documentation is at **`/docs`** (Swagger UI) and **`/redoc`**.

### Authentication

| Method | Header | Use case |
|---|---|---|
| JWT Bearer | `Authorization: Bearer <token>` | After `/api/auth/login` |
| API Key | `X-API-Key: <key>` | iOS app / automation |

### Key endpoints

```
POST  /api/auth/login       # { "username": "...", "password": "..." } → token
POST  /api/auth/api-key     # Create API key (requires JWT)
GET   /api/stats/summary    # Aggregated + per-instance stats
GET   /api/stats/history    # Over-time query data (?hours=24)
GET   /api/stats/top        # Top domains and clients (?hours=24&limit=10)
GET   /api/queries          # Paginated query log (filterable, sortable)
GET   /api/instances        # Instance list with status
GET   /api/sync/status      # Last sync state
POST  /api/sync             # Trigger a sync
GET   /api/sync/schedule    # Get sync schedule settings
PUT   /api/sync/schedule    # Update sync schedule settings
```

---

## Database Schema

```
users              — dashboard login accounts
api_keys           — iOS app / API client keys
pihole_instances   — Pi-hole instance registry (from YAML); is_master flag
stats_snapshots    — Periodic stats snapshots (one per instance per poll)
query_logs         — Consolidated DNS query log entries (30-day retention)
app_settings       — Key/value store for persisted app config (sync schedule,
                     last sync result)
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
    │   ├── instances.py
    │   ├── queries.py
    │   ├── stats.py
    │   └── sync.py
    ├── services/
    │   ├── pihole_client.py  # Pi-hole v6 REST API client (teleporter support)
    │   ├── collector.py      # APScheduler background jobs
    │   ├── config_loader.py  # YAML → DB sync
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
- `GET /api/teleporter` — export configuration ZIP (sync)
- `POST /api/teleporter` — import configuration ZIP (sync)
- `POST /api/gravity` — trigger gravity update (sync)

Pi-hole v5 (the `api.php` interface) is **not** supported.

---

## Development

```bash
# Run locally without Docker (requires a running PostgreSQL instance)
pip install -r requirements.txt

export DATABASE_URL="postgresql+asyncpg://mypi:mypi@localhost:5432/mypi"
export SECRET_KEY="dev-secret-key"
export INITIAL_ADMIN_PASSWORD="admin"

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
- [ ] iOS app
- [ ] Blocking / unblocking domains via the aggregated UI
- [ ] Email / push notifications for instances going offline

---

## License

MIT

---

> **Vibe coded with [Claude Code](https://claude.ai/code) by Anthropic.**
> Use at your own risk. Review before trusting.
