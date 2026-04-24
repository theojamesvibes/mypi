# Multi-site — migration plan

Status: **draft**
Branch: `multisite`
Paired with: `docs/multisite-design.md`

This doc covers the Alembic schema changes and data backfill needed to
introduce per-site scoping without breaking existing single-site
deployments.

---

## Guiding principles

1. **One Alembic revision, one transaction.** Everything below runs as a
   single migration (`0013_multisite.py`) inside one transaction so a
   failure anywhere rolls back cleanly. No partial state.
2. **Backfill before enforcing constraints.** Add columns nullable, copy
   data, then add NOT NULL + FKs. Classic "expand → migrate → contract" —
   but compressed into one revision since we don't need online migration
   at our scale (~10 sites × ~10 instances × ~weeks of queries).
3. **Preserve every existing row.** No DELETE on data tables. Users
   upgrading keep every stats snapshot and query log they have.
4. **Legacy un-prefixed API routes keep working** because every existing
   pihole_instance gets moved into a new `Default` site and that site is
   promoted to Main. Old clients see the same data they saw yesterday.

---

## New tables

### `sites`

```sql
CREATE TABLE sites (
    id              UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    name            VARCHAR(128) NOT NULL,
    slug            VARCHAR(64)  NOT NULL,
    is_main         BOOLEAN      NOT NULL DEFAULT FALSE,
    is_active       BOOLEAN      NOT NULL DEFAULT TRUE,
    sort_order      INTEGER      NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT sites_name_active_unique UNIQUE (name, is_active),
    CONSTRAINT sites_slug_active_unique UNIQUE (slug, is_active)
);

-- Exactly one Main among active sites.
CREATE UNIQUE INDEX sites_one_main_active
    ON sites (is_main)
    WHERE is_main = TRUE AND is_active = TRUE;
```

Notes:
- `UNIQUE (slug, is_active)` lets deactivated sites hold on to their slug
  (so cleanup can preview what would be deleted without clashing with a
  re-added site). Reactivating: config_loader handles renames/clashes.
- Partial unique index enforces exactly one Main — the database rejects
  any state that would leave the inheritance model broken.

### `site_slug_history`

```sql
CREATE TABLE site_slug_history (
    old_slug   VARCHAR(64) PRIMARY KEY,
    site_id    UUID         NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    retired_at TIMESTAMPTZ  NOT NULL DEFAULT now()
);
```

When a site's slug changes, the old slug is inserted here. The API
resolver checks `sites.slug` first, then falls back to
`site_slug_history.old_slug` and returns HTTP 301 with the new canonical
URL. Bookmarks don't break.

### `site_settings` (replaces per-site portion of `app_settings`)

```sql
CREATE TABLE site_settings (
    site_id UUID         NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    key     VARCHAR(128) NOT NULL,
    value   TEXT,                -- NULL = inherit from Main (for non-Main sites)
    PRIMARY KEY (site_id, key)
);
```

Read resolution:
1. Look up `(site_id, key)`.
2. If row missing OR `value IS NULL`, and `site_id != main_site_id` →
   fall back to `(main_site_id, key)`.
3. If still missing → code-level default.

---

## Altered tables

### `pihole_instances`

```sql
ALTER TABLE pihole_instances
    ADD COLUMN site_id UUID REFERENCES sites(id) ON DELETE CASCADE;

-- After backfill:
ALTER TABLE pihole_instances
    ALTER COLUMN site_id SET NOT NULL;

-- Drop the old global-name uniqueness:
ALTER TABLE pihole_instances
    DROP CONSTRAINT pihole_instances_name_key;

-- Add scoped uniqueness:
ALTER TABLE pihole_instances
    ADD CONSTRAINT pihole_instances_site_name_unique
    UNIQUE (site_id, name);
```

- `ON DELETE CASCADE` on `site_id`: deleting an orphan site cascades to
  its instances, which (already) cascade to their `stats_snapshots` and
  `query_logs`. User sees one confirm modal with the full row-count
  impact before the cascade runs.
- Dropping the global-name uniqueness lets two sites each have a
  `"Living Room"` instance without collision.

### `app_settings`

The existing flat `app_settings` table stays for **global-only** keys
(`session.ttl_minutes`, any future global). Per-site keys are migrated
out to `site_settings`:

```sql
-- Move every existing row into the new Default site's site_settings.
-- Done in the backfill step below.
```

After the backfill, `app_settings` may contain only `session.ttl_minutes`
(or it may be empty if that setting lives in env vars — TBD during
implementation; no schema impact either way).

---

## Backfill sequence (runs inside the same migration)

```python
def upgrade():
    # 1. Create new tables.
    op.create_table("sites", ...)
    op.create_table("site_slug_history", ...)
    op.create_table("site_settings", ...)

    # 2. Add nullable site_id to pihole_instances.
    op.add_column("pihole_instances",
                  sa.Column("site_id", UUID, sa.ForeignKey("sites.id", ondelete="CASCADE"),
                            nullable=True))

    # 3. Create the Default site as Main.
    default_site_id = uuid.uuid4()
    op.execute(sa.text("""
        INSERT INTO sites (id, name, slug, is_main, is_active, sort_order)
        VALUES (:id, 'Default', 'default', TRUE, TRUE, 0)
    """).bindparams(id=default_site_id))

    # 4. Point every existing instance at Default.
    op.execute(sa.text("""
        UPDATE pihole_instances SET site_id = :sid WHERE site_id IS NULL
    """).bindparams(sid=default_site_id))

    # 5. Now enforce NOT NULL.
    op.alter_column("pihole_instances", "site_id", nullable=False)

    # 6. Swap the name-uniqueness constraint.
    op.drop_constraint("pihole_instances_name_key", "pihole_instances", type_="unique")
    op.create_unique_constraint(
        "pihole_instances_site_name_unique", "pihole_instances", ["site_id", "name"]
    )

    # 7. Move per-site app_settings into site_settings under Default.
    #    Classifies which keys are per-site based on a hard-coded list
    #    matching the design doc (all sync.*, pushover.*, poll.*).
    op.execute(sa.text("""
        INSERT INTO site_settings (site_id, key, value)
        SELECT :sid, key, value
        FROM app_settings
        WHERE key LIKE 'sync.%'
           OR key LIKE 'pushover.%'
           OR key LIKE 'poll.%'
    """).bindparams(sid=default_site_id))

    # 8. Delete those rows from app_settings (they live in site_settings now).
    op.execute(sa.text("""
        DELETE FROM app_settings
        WHERE key LIKE 'sync.%'
           OR key LIKE 'pushover.%'
           OR key LIKE 'poll.%'
    """))


def downgrade():
    # Reverse the above. Moves site_settings rows back into app_settings
    # (flattening them — only Main's values survive, others are lost).
    # Acceptable: downgrade is a developer-escape-hatch, not a routine
    # operation. We'll document "downgrade loses non-Main site config."
    ...
```

---

## Existing migration review

Pre-existing migrations that this one touches:

- `0001_initial_schema.py` — creates `pihole_instances` (baseline).
- `0002_add_session_sid.py` — adds `session_sid` (no interaction).
- `0003_add_is_master.py` — adds `is_master` (stays as-is; meaning is now
  "this instance is its site's master").
- `0004_app_settings.py` — creates `app_settings` (we keep it for global
  keys, move per-site keys to `site_settings`).
- `0005`–`0012` — no conflicts.

Revision number for this migration: **`0013_multisite.py`**.
Down-revises: `0012_drop_hot_spare`.

---

## Edge cases covered

| Case | Behavior |
|---|---|
| Fresh install | `alembic upgrade head` creates all tables including `sites` with one Default/Main row, no instances. First YAML load creates instances under Default. |
| Existing single-site install | Migration wraps existing instances + settings into a Default/Main site. Legacy API routes keep working. User-visible: zero change. |
| User adds `sites:` to YAML | Next container restart: config_loader creates the new sites, moves instances, runs the Main-reassignment logic if the old Default is renamed or dropped. |
| User keeps flat `instances:` forever | Works indefinitely. Loader silently wraps them in Default every restart. |
| Migration fails halfway | One transaction → full rollback. User sees an error; no partial schema state. |
| Downgrade | Supported, but non-Main site config is lost. Documented in the migration docstring. |

---

## Testing plan

- Unit test: `test_migration_0013.py` spins up a schema at revision 0012,
  loads fixture data (3 instances, 5 app_settings rows), runs upgrade,
  asserts Default site exists, all 3 instances point at it, per-site keys
  moved to `site_settings`, global keys remain in `app_settings`.
- Manual test: run migration against a copy of the prod DB, verify row
  counts pre/post match for `pihole_instances`, `stats_snapshots`,
  `query_logs`; verify `site_settings` row count matches
  `app_settings.count(sync.*) + count(pushover.*) + count(poll.*)`
  before migration.
