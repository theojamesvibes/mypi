from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from app.api import auth as auth_router
from app.api import domains as domains_router
from app.api import instances as instances_router
from app.api import notifications as notifications_router
from app.api import queries as queries_router
from app.api import stats as stats_router
from app.api import sync as sync_router
from app.api import version as version_router
from app.auth import get_current_user, get_current_user_optional, hash_password, verify_password
from app.config import SESSION_COOKIE_NAME, settings
from app.database import AsyncSessionLocal, get_db
from app.models.user import User
from app.models.user import User
from app.services.collector import backfill_all_instances, cleanup_old_data, fetch_all_instance_versions, poll_queries, poll_stats, shutdown as collector_shutdown
from app.services.config_loader import sync_instances
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
    settings.validate_encryption_key_at_startup()
    version_check_service.initialize(APP_VERSION)
    await _bootstrap()
    await sync_service.load_schedule()
    await pushover_service.load_settings()
    await session_settings.load_settings()
    await version_check_service.load_settings()
    await pihole_version_check_service.load_settings()
    scheduler.add_job(poll_stats, "interval", seconds=settings.stats_poll_interval, id="poll_stats")
    scheduler.add_job(poll_queries, "interval", seconds=settings.queries_poll_interval, id="poll_queries")
    scheduler.add_job(cleanup_old_data, "cron", hour=3, minute=0, id="cleanup")
    scheduler.add_job(version_check_service.check_now, "interval", hours=1, id="version_check")
    scheduler.add_job(pihole_version_check_service.check_now, "interval", hours=1, id="pihole_version_check")
    scheduler.start()
    # Run initial checks in the background without blocking startup
    import asyncio as _asyncio
    _asyncio.create_task(version_check_service.check_now())
    _asyncio.create_task(pihole_version_check_service.check_now())
    _asyncio.create_task(fetch_all_instance_versions())
    _asyncio.create_task(backfill_all_instances())
    logger.info("Scheduler started (stats every %ds, queries every %ds).",
                settings.stats_poll_interval, settings.queries_poll_interval)
    yield
    scheduler.shutdown(wait=False)
    await collector_shutdown()


app = FastAPI(
    title="MyPi",
    description="Consolidated Pi-hole dashboard and API",
    version="1.0.0",
    lifespan=lifespan,
)

app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")
templates.env.globals["app_version"] = APP_VERSION

# API routers
app.include_router(auth_router.router)
app.include_router(instances_router.router)
app.include_router(stats_router.router)
app.include_router(queries_router.router)
app.include_router(domains_router.router)
app.include_router(sync_router.router)
app.include_router(notifications_router.router)
app.include_router(version_router.router)


# ── Web UI routes ─────────────────────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse, include_in_schema=False)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})


@app.post("/login", include_in_schema=False)
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
async def logout_web():
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
