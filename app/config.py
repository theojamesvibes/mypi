from __future__ import annotations

import logging
import os
import re
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

_SLUG_STRIP_RE = re.compile(r"[^a-z0-9]+")
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
    initial_admin_password: str = "changeme"

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

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, v: str) -> str:
        if not v.startswith("postgresql"):
            raise ValueError("DATABASE_URL must be a PostgreSQL URL")
        return v


class PiholeInstanceConfig:
    def __init__(
        self,
        name: str,
        url: str,
        password: str,
        color: str,
        master: bool = False,
    ):
        self.name = name
        self.url = url.rstrip("/")
        self.password = password
        self.color = color
        self.master = master


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
    """Derive a URL slug from a site name. Lowercase, hyphen-delimited,
    alphanumeric. Returns empty string when nothing usable is left."""
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
    if not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", slug):
        raise ValueError(
            f"{source}: slug '{slug}' must be lowercase alphanumerics "
            "separated by single hyphens"
        )


def _parse_instance(item: dict) -> PiholeInstanceConfig:
    return PiholeInstanceConfig(
        name=item["name"],
        url=item["url"],
        password=item.get("password", ""),
        color=item.get("color", "#3c8dbc"),
        master=bool(item.get("master", False)),
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
    config_path = path or os.getenv("PIHOLE_CONFIG_PATH", "pihole_instances.yml")
    p = Path(config_path)
    if not p.exists():
        return []
    try:
        with open(p) as f:
            data = yaml.safe_load(f)
    except (OSError, yaml.YAMLError) as exc:
        logger.warning(
            "Could not load Pi-hole sites from %s: %s — starting with no sites.",
            config_path, exc,
        )
        return []
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


settings = Settings()
