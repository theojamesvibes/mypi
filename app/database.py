from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

engine = create_async_engine(
    settings.database_url,
    pool_pre_ping=True,
    # Sized for an in-process app polling ~10 instances at 60s intervals
    # with intermittent UI traffic — peak in-flight sessions stay in
    # single digits. The previous 20+10 was holding 30% of Postgres's
    # default max_connections per worker for no observable benefit.
    pool_size=10,
    max_overflow=5,
    pool_recycle=3600,
)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncGenerator[AsyncSession]:
    async with AsyncSessionLocal() as session:
        yield session
