from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import Cookie, Depends, FastAPI, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from cryptography.fernet import Fernet

from app.api import auth as auth_router
from app.api import domains as domains_router
from app.api import health as health_router
from app.api import instances as instances_router
from app.api import notifications as notifications_router
from app.api import poll_settings as poll_settings_router
from app.api import queries as queries_router
from app.api import stats as stats_router
from app.api import sync as sync_router
from app.api import version as version_router
from app.auth import _decode_token_claims, get_current_user, get_current_user_optional, hash_password, verify_password
from app.config import SESSION_COOKIE_NAME, settings
from app.database import AsyncSessionLocal, get_db
from app.limiter import limiter
from app.models.settings import AppSetting
from app.models.user import RevokedToken, User
from app.services.collector import backfill_all_instances, cleanup_old_data, fetch_all_instance_versions, poll_queries, poll_stats, shutdown as collector_shutdown
from app.services.config_loader import sync_instances
from app.services import poll_settings as poll_settings_service
from app.services import pushover as pushover_service
from app.services import session_settings
from app.services import sync_service
from app.services import pihole_version_check as pihole_version_check_service
from app.services import version_check as version_check_service

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

_version_file = Path(__file__).parent.parent / "VERSION"
APP_VERSION = _version_file.read_text().strip() if _version_file.exists() else "dev"

scheduler = AsyncIOScheduler()

# Long-lived set of fire-and-forget background tasks. The event loop only
# holds *weak* references to tasks created with `asyncio.create_task(...)`,
# so without keeping a strong reference here the task can be garbage-
# collected mid-run. Every helper that spawns a one-shot task should call
# `_track_task(...)` instead of `asyncio.create_task(...)` directly.
_background_tasks: set = set()


def _track_task(coro) -> None:
    import asyncio as _asyncio
    task = _asyncio.create_task(coro)
    _background_tasks.add(task)

    def _done(t: _asyncio.Task) -> None:
        _background_tasks.discard(t)
        if not t.cancelled():
            exc = t.exception()
            if exc is not None:
                logger.exception("Background task failed", exc_info=exc)

    task.add_done_callback(_done)


_ENCRYPTION_KEY_SETTING = "encryption_key"


async def _ensure_encryption_key() -> None:
    """Resolve the Fernet encryption key used for Pi-hole password storage.

    Priority:
    1. ENCRYPTION_KEY env var / .env — explicit operator configuration.
    2. app_settings table — key was auto-generated on a previous startup.
    3. Neither — generate a new key, persist it to app_settings, and warn.

    Setting settings.encryption_key here (before any ORM read/write touches
    the encrypted column) ensures _get_fernet() in models/pihole.py picks up
    the correct key on its first lazy call.
    """
    import app.models.pihole as pihole_models

    if settings.encryption_key:
        # Validate the explicitly-provided key before anything touches the DB.
        try:
            Fernet(settings.encryption_key.encode())
        except Exception:
            raise RuntimeError(
                "ENCRYPTION_KEY in .env is not a valid Fernet key. "
                "Generate a new one with: "
                "python3 -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
            )
        return  # explicit key is valid — nothing more to do

    # No key in environment — check the database.
    async with AsyncSessionLocal() as db:
        row = await db.get(AppSetting, _ENCRYPTION_KEY_SETTING)
        if row and row.value:
            settings.encryption_key = row.value
            pihole_models._fernet = None  # force _get_fernet() to re-initialise
            logger.info("Loaded encryption key from database.")
            return

        # No key anywhere — generate one and persist it.
        new_key = Fernet.generate_key().decode()
        stmt = (
            pg_insert(AppSetting)
            .values(key=_ENCRYPTION_KEY_SETTING, value=new_key)
            .on_conflict_do_update(index_elements=["key"], set_={"value": new_key})
        )
        await db.execute(stmt)
        await db.commit()
        settings.encryption_key = new_key
        pihole_models._fernet = None
        logger.warning(
            "ENCRYPTION_KEY was not set — a key has been auto-generated and saved to the "
            "database. Add it to your .env for portability: ENCRYPTION_KEY=%s", new_key
        )


async def _bootstrap() -> None:
    """Sync instances and create initial admin user if needed."""
    async with AsyncSessionLocal() as db:
        await sync_instances(db)

        result = await db.execute(select(User).limit(1))
        if result.scalar_one_or_none() is None:
            admin = User(
                username=settings.initial_admin_user,
                hashed_password=hash_password(settings.initial_admin_password),
                password_change_required=True,
            )
            db.add(admin)
            await db.commit()
            logger.info("Created initial admin user: %s (password change required on first login)", settings.initial_admin_user)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("MyPi version %s starting up", APP_VERSION)
    await _ensure_encryption_key()
    version_check_service.initialize(APP_VERSION)
    await _bootstrap()
    await sync_service.load_schedule()
    await pushover_service.load_settings()
    await session_settings.load_settings()
    await version_check_service.load_settings()
    await pihole_version_check_service.load_settings()
    await poll_settings_service.load_settings()
    poll_settings_service.set_reschedule_callback(
        lambda s: scheduler.reschedule_job("poll_queries", trigger="interval", seconds=s)
    )
    scheduler.add_job(poll_stats, "interval", seconds=settings.stats_poll_interval, id="poll_stats")
    scheduler.add_job(poll_queries, "interval", seconds=poll_settings_service.get_interval_seconds(), id="poll_queries")
    scheduler.add_job(cleanup_old_data, "cron", hour=3, minute=0, id="cleanup")
    scheduler.add_job(version_check_service.check_now, "interval", hours=1, id="version_check")
    scheduler.add_job(pihole_version_check_service.check_now, "interval", hours=1, id="pihole_version_check")
    scheduler.start()
    # Run initial checks in the background without blocking startup. Using
    # `_track_task` (not bare `create_task`) keeps a strong ref so the tasks
    # aren't GC'd and logs exceptions instead of swallowing them.
    _track_task(version_check_service.check_now())
    _track_task(pihole_version_check_service.check_now())
    _track_task(fetch_all_instance_versions())
    _track_task(backfill_all_instances())
    logger.info("Scheduler started (stats every %ds, queries every %ds).",
                settings.stats_poll_interval, poll_settings_service.get_interval_seconds())
    yield
    scheduler.shutdown(wait=False)
    await collector_shutdown()


app = FastAPI(
    title="MyPi",
    description="Consolidated Pi-hole dashboard and API",
    version=APP_VERSION,
    lifespan=lifespan,
    docs_url=None,
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


# ── Security headers ──────────────────────────────────────────────────────────
#
# MyPi is a self-hosted dashboard — there's no legitimate reason for another
# origin to frame it, load its content-type-sniffing, or exfiltrate its
# referrer. Add a small set of defensive headers by default.
# HSTS is opt-in (settings.secure_cookies) so we don't break local HTTP setups.
@app.middleware("http")
async def _security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    if settings.secure_cookies:
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )
    return response

app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")
templates.env.globals["app_version"] = APP_VERSION

# API routers
app.include_router(auth_router.router)
app.include_router(health_router.router)
app.include_router(instances_router.router)
app.include_router(stats_router.router)
app.include_router(queries_router.router)
app.include_router(domains_router.router)
app.include_router(sync_router.router)
app.include_router(notifications_router.router)
app.include_router(poll_settings_router.router)
app.include_router(version_router.router)


# ── API docs ──────────────────────────────────────────────────────────────────

@app.get("/docs", include_in_schema=False)
async def swagger_ui(request: Request):
    # Custom template so the docs page follows the user's MyPi theme
    # (light/dark/system) via localStorage['mypi-theme'] — same logic as base.html.
    return templates.TemplateResponse("docs.html", {"request": request})


# ── Web UI routes ─────────────────────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse, include_in_schema=False)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})


@app.post("/login", include_in_schema=False)
@limiter.limit("10/minute")
async def login_form(request: Request, response: Response, db=Depends(get_db)):
    form = await request.form()
    username = form.get("username", "")
    password = form.get("password", "")

    from app.auth import create_access_token, verify_password
    result = await db.execute(select(User).where(User.username == username, User.is_active.is_(True)))
    user = result.scalar_one_or_none()

    if user is None or not verify_password(password, user.hashed_password):
        return templates.TemplateResponse(
            "login.html", {"request": request, "error": "Invalid username or password"}, status_code=401
        )

    expire_minutes = session_settings.effective_minutes(session_settings.get_timeout_minutes())
    token = create_access_token(user.username, expire_minutes=expire_minutes)
    dest = "/change-password" if user.password_change_required else "/"
    redirect = RedirectResponse(url=dest, status_code=303)
    redirect.set_cookie(SESSION_COOKIE_NAME, token, httponly=True, secure=settings.secure_cookies, samesite="lax", max_age=expire_minutes * 60)
    return redirect


@app.get("/logout", include_in_schema=False)
async def logout_web(
    session_token: str | None = Cookie(default=None),
    db=Depends(get_db),
):
    if session_token:
        claims = _decode_token_claims(session_token)
        if claims and claims.get("jti"):
            from datetime import datetime, timezone
            jti = claims["jti"]
            exp = claims.get("exp")
            expires_at = (
                datetime.fromtimestamp(exp, tz=timezone.utc)
                if exp
                else datetime.now(timezone.utc)
            )
            stmt = pg_insert(RevokedToken).values(jti=jti, expires_at=expires_at).on_conflict_do_nothing()
            await db.execute(stmt)
            await db.commit()

    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def dashboard(request: Request, current_user=Depends(get_current_user_optional)):
    if current_user is None:
        return RedirectResponse(url="/login", status_code=303)
    if current_user.password_change_required:
        return RedirectResponse(url="/change-password", status_code=303)
    return templates.TemplateResponse("dashboard.html", {"request": request, "user": current_user})


@app.get("/queries", response_class=HTMLResponse, include_in_schema=False)
async def queries_page(request: Request, current_user=Depends(get_current_user_optional)):
    if current_user is None:
        return RedirectResponse(url="/login", status_code=303)
    if current_user.password_change_required:
        return RedirectResponse(url="/change-password", status_code=303)
    return templates.TemplateResponse("queries.html", {"request": request, "user": current_user})


@app.get("/settings", response_class=HTMLResponse, include_in_schema=False)
async def settings_page(request: Request, current_user=Depends(get_current_user_optional)):
    if current_user is None:
        return RedirectResponse(url="/login", status_code=303)
    if current_user.password_change_required:
        return RedirectResponse(url="/change-password", status_code=303)
    return templates.TemplateResponse("settings.html", {"request": request, "user": current_user})


@app.get("/change-password", response_class=HTMLResponse, include_in_schema=False)
async def change_password_page(request: Request, current_user=Depends(get_current_user_optional)):
    if current_user is None:
        return RedirectResponse(url="/login", status_code=303)
    return templates.TemplateResponse("change_password.html", {"request": request, "user": current_user})


@app.post("/change-password", include_in_schema=False)
async def change_password_form(request: Request, db=Depends(get_db), current_user=Depends(get_current_user_optional)):
    if current_user is None:
        return RedirectResponse(url="/login", status_code=303)

    form = await request.form()
    current_pw = form.get("current_password", "")
    new_pw = form.get("new_password", "")
    confirm_pw = form.get("confirm_password", "")

    def _error(msg: str):
        return templates.TemplateResponse(
            "change_password.html", {"request": request, "user": current_user, "error": msg}, status_code=422
        )

    if not verify_password(current_pw, current_user.hashed_password):
        return _error("Current password is incorrect.")
    if len(new_pw) < 8:
        return _error("New password must be at least 8 characters.")
    if new_pw != confirm_pw:
        return _error("Passwords do not match.")

    async with AsyncSessionLocal() as session:
        user = await session.get(User, current_user.id)
        user.hashed_password = hash_password(new_pw)
        user.password_change_required = False
        await session.commit()

    return RedirectResponse(url="/", status_code=303)
