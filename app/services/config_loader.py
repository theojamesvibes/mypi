"""Sync Pi-hole instance config from YAML into the database."""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import load_instance_configs
from app.models.pihole import PiholeInstance

logger = logging.getLogger(__name__)


async def sync_instances(db: AsyncSession) -> None:
    """Upsert Pi-hole instances from YAML config into the database."""
    configs = load_instance_configs()
    if not configs:
        logger.info("No Pi-hole instances found in config file.")
        return

    config_names = {c.name for c in configs}

    for cfg in configs:
        result = await db.execute(select(PiholeInstance).where(PiholeInstance.name == cfg.name))
        instance = result.scalar_one_or_none()
        if instance is None:
            instance = PiholeInstance(
                name=cfg.name,
                url=cfg.url,
                api_password=cfg.password,
                color=cfg.color,
                is_active=True,
            )
            db.add(instance)
            logger.info("Added new Pi-hole instance: %s", cfg.name)
        else:
            instance.url = cfg.url
            instance.api_password = cfg.password
            instance.color = cfg.color
            instance.is_active = True
            logger.debug("Updated Pi-hole instance: %s", cfg.name)

    # Deactivate instances that were removed from config
    result = await db.execute(select(PiholeInstance))
    all_instances = result.scalars().all()
    for inst in all_instances:
        if inst.name not in config_names:
            inst.is_active = False
            logger.info("Deactivated removed Pi-hole instance: %s", inst.name)

    await db.commit()
    logger.info("Pi-hole instance sync complete. %d instance(s) active.", len(configs))
