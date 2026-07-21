"""Application configuration.

Two responsibilities: (1) the `Settings` class reads environment
variables / .env for things like the database URL and secret keys;
(2) the `load_site_configs` functions parse `pihole_instances.yml`
into the sites and Pi-hole instances the app manages.
"""

from __future__ import annotations

import logging
import os
import re
import stat
from pathlib import Path

import yaml
from pydantic import field_validator  # noqa: F401 — kept for database_url validator
from pydantic_settings import BaseSettings, SettingsConfigDict

SESSION_COOKIE_NAME = "session_token"
SESSION_COOKIE_MAX_AGE = 60 * 60 * 8  # 8 hours

# Slugs that would clash with API route segments or reserved URL space.
# Enforced for user-provided slugs in YAML; the system-generated `default`
# slug used when wrapping legacy flat `instances:` YAML is exempt.
RESERVED_SLUGS = frozenset({"sites", "inactive", "admin", "main", "combined"})

# Turn runs of anything that isn't a lowercase letter or digit into a hyphen.
_SLUG_STRIP_RE = re.compile(r"[^a-z0-9]+")
# Strip leading/trailing hyphens left behind after the substitution above.
_SLUG_EDGE_RE = re.compile(r"(^-+)|(-+$)")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    secret_key: str
    # Optional split secrets — when set, used in place of secret_key for the
    # respective use. When empty, both fall back to secret_key (preserves
    # current behaviour). Rotating one without the other lets an operator
    # invalidate JWT sessions without nuking issued API keys (or vice versa).
    jwt_secret_key: str = ""
    api_key_salt: str = ""
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 60 * 8  # 8 hours

    initial_admin_user: str = "admin"
    # Empty by default — _bootstrap() generates a random password on first
    # run when none is set, logs it once, and forces a change on first login.
    # This closes the deploy-gap window where an attacker could log in as
    # admin/changeme and lock out the legitimate operator before they got
    # to change the password themselves.
    initial_admin_password: str = ""

    # Fernet key for encrypting Pi-hole API passwords at rest.
    # Generate with: python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    encryption_key: str = ""

    # Set to true when MyPi is behind a TLS-terminating reverse proxy (Traefik, nginx, etc.)
    # so session cookies carry the Secure flag.  Leave false for plain-HTTP local access.
    secure_cookies: bool = False

    # Verify TLS certificates when connecting to Pi-hole instances over HTTPS.
    # Defaults to False because most Pi-hole deployments use self-signed
    # certs on a trusted LAN segment — flipping the default broke every
    # existing self-signed deployment on the 1.8.0-dev hardening branch.
    # If your MyPi ↔ Pi-hole path is NOT a trusted segment, set
    # VERIFY_PIHOLE_SSL=true in .env; the 1.8.0-dev `hardening-review`
    # CHANGELOG recommends this explicitly.
    verify_pihole_ssl: bool = False

    pihole_config_path: str = "/app/pihole_instances.yml"
    max_sites: int = 10
    max_pihole_instances: int = 10  # per site, not total

    # Expose FastAPI's auto-generated Swagger UI (/docs) and OpenAPI schema
    # (/openapi.json). Both are useful for development and for driving iOS-side
    # client generation, but they reveal the full API surface unauthenticated.
    # Default flipped to False in 1.8.0-dev.7 — fail-closed for the stable
    # release. Set ENABLE_API_DOCS=true in .env when you need them (local
    # development, regenerating an iOS client from the schema, etc.).
    enable_api_docs: bool = False

    stats_poll_interval: int = 60
    queries_poll_interval: int = 10
    data_retention_days: int = 30

    # Adlists in this Pi-hole group are treated as security/threat feeds, so the
    # "Blocked by list" breakdown can flag their blocks as threats rather than
    # ads. Create the group in Pi-hole and assign malware/phishing feeds to it
    # (e.g. HaGeZi TIF, URLhaus). Matched case-insensitively. Blank disables the
    # security flag entirely (all lists shown, none marked as threats).
    security_group_name: str = "security"
    # How often to mirror each instance's /api/lists into pihole_lists.
    list_sync_interval_minutes: int = 15

    # Per-instance circuit breaker — suspends polling for a wedged Pi-hole
    # after N consecutive failures, for M seconds, instead of hammering it at
    # the normal cadence.  Failures within the dedup window of the previous
    # failure count as the same event (stats+queries share the connection).
    circuit_fail_threshold: int = 3
    circuit_cooldown_seconds: int = 300
    circuit_dedup_seconds: float = 2.0

    # PiholeClient auth backoff — on 429 from /api/auth, don't retry for this
    # many seconds.  Protects Pi-hole from auth hammering when its session
    # table is saturated; surfaces as a periodic WARN in the log.
    auth_backoff_seconds: int = 300

    # Brute-force protection on the user-facing /login endpoint. SlowAPI's
    # per-IP rate limit (10/minute) is fine on a LAN but trivial to sidestep
    # behind a NAT or any reverse proxy that aggregates clients into one
    # source IP. After `login_lockout_threshold` *consecutive* failures for
    # a username, that account is locked for `login_lockout_minutes`. A
    # successful login resets the counter. Set threshold to 0 to disable.
    login_lockout_threshold: int = 5
    login_lockout_minutes: int = 15

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, v: str) -> str:
        if not v.startswith("postgresql"):
            raise ValueError("DATABASE_URL must be a PostgreSQL URL")
        return v


class PiholeInstanceConfig:
    """One Pi-hole instance: its name, URL, admin password, chart color,
    and VIP role."""

    def __init__(
        self,
        name: str,
        url: str,
        password: str,
        color: str,
        master: bool = False,
        vip_role: str | None = None,
    ):
        self.name = name
        self.url = url.rstrip("/")
        self.password = password
        self.color = color
        self.master = master
        # None / "master" / "replica" — VIP cluster membership. The collector
        # uses this to suppress per-instance stall alerts for replicas (idle
        # is the normal state on a standby) and to detect VIP transfer events.
        self.vip_role = vip_role


class SiteConfig:
    """A site from pihole_instances.yml — one or more pihole instances with a
    master, wrapped in a site scope.

    Legacy flat `instances:` YAML is parsed into a single implicit SiteConfig
    named "Default" with slug "default" and is_main=True.
    """

    def __init__(
        self,
        name: str,
        slug: str,
        main: bool,
        instances: list[PiholeInstanceConfig],
    ):
        self.name = name
        self.slug = slug
        self.main = main
        self.instances = instances


def slugify(name: str) -> str:
    """Derive a URL slug (a short, URL-safe name like `home-lab`) from a
    site name. Lowercase, hyphen-delimited, alphanumeric. Returns empty
    string when nothing usable is left."""
    slug = name.lower()
    slug = _SLUG_STRIP_RE.sub("-", slug)
    slug = _SLUG_EDGE_RE.sub("", slug)
    return slug[:64]


def validate_slug(slug: str, source: str) -> None:
    """Raise ValueError with source context if the slug is unusable."""
    if not slug:
        raise ValueError(f"{source}: slug is empty or contains only punctuation")
    if len(slug) > 64:
        raise ValueError(f"{source}: slug exceeds 64 characters")
    if slug in RESERVED_SLUGS:
        raise ValueError(f"{source}: slug '{slug}' is reserved")
    # must look like "abc-def-123": lowercase groups joined by single hyphens
    if not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", slug):
        raise ValueError(
            f"{source}: slug '{slug}' must be lowercase alphanumerics "
            "separated by single hyphens"
        )


def _parse_instance(item: dict) -> PiholeInstanceConfig:
    # vip_master / vip_replica are mutually exclusive. Cross-instance
    # validation (one vip_master per site) happens in _parse_site.
    is_vip_master = bool(item.get("vip_master", False))
    is_vip_replica = bool(item.get("vip_replica", False))
    vip_role: str | None = None
    if is_vip_master and is_vip_replica:
        logging.getLogger(__name__).warning(
            "Instance '%s' has both vip_master and vip_replica set — "
            "ignoring both. Pick one.", item.get("name", "?"),
        )
    elif is_vip_master:
        vip_role = "master"
    elif is_vip_replica:
        vip_role = "replica"

    return PiholeInstanceConfig(
        name=item["name"],
        url=item["url"],
        password=item.get("password", ""),
        color=item.get("color", "#3c8dbc"),
        master=bool(item.get("master", False)),
        vip_role=vip_role,
    )


def _parse_site(item: dict, fallback_index: int) -> SiteConfig:
    name = item.get("name") or f"Site {fallback_index + 1}"
    raw_slug = item.get("slug")
    slug = raw_slug if raw_slug else slugify(name)
    # Validate user-supplied slug; auto-derived slug also gets the same
    # treatment to keep bad names from silently becoming unusable URLs.
    validate_slug(slug, f"site '{name}'")

    raw_instances = item.get("instances") or []
    if len(raw_instances) > settings.max_pihole_instances:
        logging.getLogger(__name__).warning(
            "Site '%s' has %d instances; only the first %d will be used.",
            name, len(raw_instances), settings.max_pihole_instances,
        )
    instances = [_parse_instance(x) for x in raw_instances[:settings.max_pihole_instances]]

    # One vip_master per site. If multiple are flagged, keep the first and
    # demote the rest to plain (no vip_role) — they likely intended a single
    # cluster and listing two masters is almost certainly a YAML mistake.
    vip_masters = [i for i in instances if i.vip_role == "master"]
    if len(vip_masters) > 1:
        winner = vip_masters[0]
        for loser in vip_masters[1:]:
            logging.getLogger(__name__).warning(
                "Site '%s' has multiple vip_master instances; '%s' is the "
                "vip_master, '%s' demoted to plain (no vip_role).",
                name, winner.name, loser.name,
            )
            loser.vip_role = None

    # `default_site: true` is the friendly alias; `main: true` stays as
    # back-compat. Either (or both) flips the Main flag.
    is_main = bool(item.get("default_site", False)) or bool(item.get("main", False))

    return SiteConfig(
        name=name,
        slug=slug,
        main=is_main,
        instances=instances,
    )


def load_site_configs(path: str | None = None) -> list[SiteConfig]:
    """Load and parse pihole_instances.yml.

    Returns a list of SiteConfig. Supports both the new `sites:` top-level
    key (multi-site) and the legacy `instances:` top-level key (wrapped
    into a single implicit `Default` site flagged main).

    Main-flag resolution: if zero sites are flagged `main: true`, the first
    site in YAML order becomes Main. If multiple sites are flagged, the
    first flagged one wins and the rest are logged as demoted.
    """
    logger = logging.getLogger(__name__)
    config_path = path if path else os.getenv("PIHOLE_CONFIG_PATH", "pihole_instances.yml")
    p = Path(config_path)
    if not p.exists():
        return []
    # The YAML stores plaintext Pi-hole admin passwords. Warn if it has
    # any group/other read or write bits — operators commonly leave it
    # 644 (default umask) without realising the contents are secrets.
    # Defense-in-depth only: the container itself runs as a single user,
    # so this catches host-side mistakes during manual edits.
    try:
        file_mode = p.stat().st_mode
        permissive_bits = file_mode & 0o077
        if permissive_bits:
            logger.warning(
                "%s has permissive file mode %o — it contains plaintext "
                "Pi-hole admin passwords. Tighten with `chmod 600 %s` so "
                "only the owning user can read it.",
                config_path, stat.S_IMODE(file_mode), config_path,
            )
    except OSError:
        # Stat failure is non-fatal — falls through to the regular load.
        pass
    try:
        with open(p) as f:
            data = yaml.safe_load(f)
    except OSError as exc:
        # File present but unreadable (permissions, mid-bind-mount race on
        # first boot, etc.). Soft-fail — the next restart may resolve it
        # and we'd rather come up than wedge the container in a crash loop
        # waiting on a host-side issue.
        logger.warning(
            "Could not read %s: %s — starting with no sites.",
            config_path, exc,
        )
        return []
    except yaml.YAMLError as exc:
        # Parse failure is operator error, not infrastructure. Coming up
        # with `[]` means downstream sync_sites_and_instances bails before
        # touching the DB and the scheduler keeps polling the previous
        # last-known-good config — silently — so the operator has no
        # signal that their edit didn't take effect. Refuse to start so
        # the failure is immediately visible.
        mark = getattr(exc, "problem_mark", None)
        if mark is not None:
            location = f"line {mark.line + 1}, column {mark.column + 1}"
        else:
            location = "unknown location"
        logger.error(
            "Failed to parse %s at %s: %s — refusing to start so the "
            "failure is visible. Fix the YAML and restart.",
            config_path, location, exc,
        )
        raise RuntimeError(
            f"Could not parse {config_path} ({location}): {exc}"
        ) from exc
    if not data:
        return []

    # New-style multi-site YAML.
    if "sites" in data:
        raw = data["sites"] or []
        if len(raw) > settings.max_sites:
            logger.warning(
                "Config has %d sites; only the first %d will be used.",
                len(raw), settings.max_sites,
            )
        sites = [_parse_site(item, i) for i, item in enumerate(raw[:settings.max_sites])]

    # Legacy flat `instances:` YAML — wrap as an implicit Default site.
    elif "instances" in data:
        raw_instances = data["instances"] or []
        if len(raw_instances) > settings.max_pihole_instances:
            logger.warning(
                "Config has %d instances; only the first %d will be used.",
                len(raw_instances), settings.max_pihole_instances,
            )
        instances = [_parse_instance(x) for x in raw_instances[:settings.max_pihole_instances]]
        sites = [SiteConfig(name="Default", slug="default", main=True, instances=instances)]

    else:
        return []

    # Resolve Main designation.
    flagged = [s for s in sites if s.main]
    if len(flagged) > 1:
        winner = flagged[0]
        for loser in flagged[1:]:
            loser.main = False
            logger.warning(
                "Multiple sites flagged main: true; demoted '%s' — '%s' is Main.",
                loser.name, winner.name,
            )
    elif not flagged and sites:
        sites[0].main = True
        logger.info(
            "No site flagged main: true; using first site '%s' as Main.",
            sites[0].name,
        )

    # Slug uniqueness across sites.
    seen: set[str] = set()
    deduped: list[SiteConfig] = []
    for s in sites:
        if s.slug in seen:
            logger.warning(
                "Duplicate slug '%s' for site '%s' — skipping duplicate.",
                s.slug, s.name,
            )
            continue
        seen.add(s.slug)
        deduped.append(s)
    return deduped


# Legacy name kept so nothing downstream of config_loader re-imports it by
# accident. Returns the flat list of instances under Main, matching the
# old semantics. Not wired into the startup path — config_loader uses
# load_site_configs now.
def load_instance_configs(path: str | None = None) -> list[PiholeInstanceConfig]:
    for site in load_site_configs(path):
        if site.main:
            return list(site.instances)
    return []


settings = Settings()  # type: ignore[call-arg]  # required fields (database_url, secret_key) come from env/.env at runtime
