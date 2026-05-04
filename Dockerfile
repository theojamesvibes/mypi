FROM python:3.14-slim

# Non-root runtime user. Create up front so later COPY --chown works.
RUN groupadd --system --gid 1000 app \
 && useradd  --system --uid 1000 --gid app \
             --no-create-home --shell /usr/sbin/nologin app

WORKDIR /app

# curl is used by the HEALTHCHECK below; apt lists are purged to keep the image small.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps as root so site-packages ends up in /usr/local (shared,
# read-only at runtime). This is the standard Docker Python pattern.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=app:app alembic.ini .
COPY --chown=app:app alembic/ alembic/
COPY --chown=app:app app/ app/
COPY --chown=app:app VERSION .

USER app

EXPOSE 8080

# Hits the unauthenticated /api/health endpoint — cheap, no DB calls on failure path.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/api/health || exit 1

CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8080"]
