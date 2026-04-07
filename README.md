# MyPi

> **⚠️ Vibe Code Disclosure**
> This project was generated entirely through AI-assisted development (Claude Code / Anthropic). The code has been reviewed and iterated on collaboratively, but it has not been audited for production use. Deploy on trusted local networks only, review the code before relying on it, and proceed with the usual amount of healthy scepticism you'd apply to any AI-generated codebase.

A self-hosted dashboard that consolidates up to 10 locally running [Pi-hole](https://pi-hole.net/) v6 instances into a single screen, paired with a REST API designed for iOS app consumption.

---

## Screenshots

> *(Add screenshots here once running)*

---

## Features

- **Unified dashboard** — Aggregate stats across all Pi-hole instances with a look and feel matching the native Pi-hole admin UI (Bootstrap 5 + Chart.js)
- **Per-system panel** — Each Pi-hole shown individually with its own stats and online/offline status badge
- **Consolidated query log** — All query logs in one searchable, filterable table with an Instance column
- **Background collection** — Stats polled every 60 seconds; query logs every 5 minutes. All data stored in PostgreSQL
- **30-day history** — Time-series charts and query log data retained for 30 days, cleaned up automatically
- **REST API** — Full JSON API with auto-generated OpenAPI docs (Swagger UI at `/docs`) ready for iOS app integration
- **Authentication** — Username/password login for the web UI (JWT session cookie); API key auth (`X-API-Key`) for mobile clients
- **Docker-native** — Single `docker compose up` gets everything running

---

## Architecture

```
┌─────────────────────────────────┐
│  Pi-hole 1  Pi-hole 2  …  Pi-hole N   (Pi-hole v6, local network)
└──────────────┬──────────────────┘
               │  HTTP (Pi-hole v6 REST API)
               ▼
┌──────────────────────────────────────────────────────┐
│                    MyPi (Docker)                     │
│                                                      │
│  ┌─────────────┐   ┌──────────────────────────────┐ │
│  │  APScheduler│   │  FastAPI                     │ │
│  │  (polling)  │──▶│  • Web UI (Jinja2/Bootstrap) │ │
│  └─────────────┘   │  • REST API (/api/*)          │ │
│                    └──────────────┬───────────────┘ │
│                                   │                  │
│                    ┌──────────────▼───────────────┐ │
│                    │  PostgreSQL 18               │ │
│                    │  (stats, query logs, users)  │ │
│                    └──────────────────────────────┘ │
└──────────────────────────────────────────────────────┘
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

The dashboard is available at **http://localhost:8080** (or whichever `APP_PORT` you set).

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
| `QUERIES_POLL_INTERVAL` | `300` | Seconds between query log polls |
| `DATA_RETENTION_DAYS` | `30` | Days of history to retain |

### Pi-hole instances (`pihole_instances.yml`)

```yaml
instances:
  - name: "Living Room"
    url: "http://192.168.1.100"
    password: "your-pihole-password"
    color: "#00a65a"   # shown in charts

  - name: "Office"
    url: "http://192.168.1.101"
    password: "your-pihole-password"
    color: "#3c8dbc"
```

- Up to **10 instances** supported
- Instances are loaded at startup and synced to the database
- Restart the container to pick up changes: `docker compose restart app`
- The `password` is the Pi-hole web interface password (Pi-hole v6 API)
- `color` is used in charts to visually distinguish each instance

---

## REST API

The full API is available under `/api/`. Interactive documentation (Swagger UI) is at **http://localhost:8080/docs**.

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
GET   /api/queries          # Paginated query log (filterable)
GET   /api/instances        # Instance list with status
```

---

## Database Schema

```
users              — dashboard login accounts
api_keys           — iOS app / API client keys
pihole_instances   — Pi-hole instance registry (from YAML)
stats_snapshots    — Periodic stats snapshots (one per instance per poll)
query_logs         — Consolidated DNS query log entries
```

---

## Project Structure

```
mypi/
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── .env.example
├── pihole_instances.yml.example
├── alembic.ini
├── alembic/
│   └── versions/0001_initial_schema.py
└── app/
    ├── main.py               # FastAPI app entry point + scheduler lifecycle
    ├── config.py             # Settings (pydantic-settings + YAML loader)
    ├── database.py           # Async SQLAlchemy engine
    ├── auth.py               # JWT + API key auth helpers
    ├── models/               # SQLAlchemy ORM models
    ├── schemas/              # Pydantic request/response schemas
    ├── api/                  # FastAPI route handlers
    ├── services/
    │   ├── pihole_client.py  # Pi-hole v6 REST API client
    │   ├── collector.py      # APScheduler background jobs
    │   └── config_loader.py  # YAML → DB sync
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
- [x] Consolidated query log
- [x] REST API with OpenAPI docs
- [x] JWT + API key authentication
- [ ] iOS app
- [ ] Blocking / unblocking domains via the aggregated UI
- [ ] Per-instance Pi-hole management (gravity update, etc.)
- [ ] Email / push notifications for instances going offline

---

## License

MIT

---

> **Vibe coded with [Claude Code](https://claude.ai/code) by Anthropic.**
> Use at your own risk. Review before trusting.
