"""Sync site + Pi-hole instance config from YAML into the database.

Runs once at container startup (from the app lifespan): reads
pihole_instances.yml and reconciles it into the DB. Changes to the YAML are
therefore only picked up on restart.
"""
from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import load_site_configs
from app.models.pihole import PiholeInstance
from app.models.site import Site, SiteSetting, SiteSlugHistory
from app.services.client_manager import close_client

logger = logging.getLogger(__name__)


async def sync_sites_and_instances(db: AsyncSession) -> None:
    """Upsert sites + Pi-hole instances from YAML into the database.

    Behavior:
      1. Upsert sites by slug — name/sort_order updated for existing rows.
      2. Upsert instances per site by (site_id, name).
      3. Orphan detection — sites and instances present in DB but not in
         YAML are marked is_active=FALSE (not deleted). Manual cleanup.
      4. Main-reassignment — if the target Main (from YAML) differs from
         the currently-active Main in the DB, copy the old Main's
         site_settings into the new Main wherever the new Main has NULL
         or missing values, then flip is_main atomically.
      5. Evict client_manager clients for deactivated instances so they
         don't hold open sessions against Pi-holes we're no longer tracking.
    """
    site_configs = load_site_configs()
    if not site_configs:
        logger.info("No sites found in config file.")
        return

    site_slugs = {sc.slug for sc in site_configs}

    # ---- 0. Main-rename detection.
    #         If the YAML's default_site's slug doesn't exist in the DB AND
    #         the DB's current Main has a slug that isn't referenced anywhere
    #         in the new YAML, treat it as a rename: update the existing
    #         Main's name+slug in place so the upsert-by-slug step below
    #         finds it. The old slug goes to site_slug_history so bookmarks
    #         (/dashboard/default etc.) keep working via 301.
    target_main = next((sc for sc in site_configs if sc.main), None)
    if target_main is not None:
        target_main_match = await db.execute(
            select(Site).where(Site.slug == target_main.slug)
        )
        target_main_in_db = target_main_match.scalar_one_or_none()

        if target_main_in_db is None:
            current_main_match = await db.execute(
                select(Site).where(
                    Site.is_main.is_(True), Site.is_active.is_(True),
                )
            )
            current_main = current_main_match.scalar_one_or_none()

            if (
                current_main is not None
                and current_main.slug not in site_slugs
            ):
                logger.info(
                    "Main-site rename detected in YAML: '%s' (slug=%s) → "
                    "'%s' (slug=%s). Preserving data in place.",
                    current_main.name, current_main.slug,
                    target_main.name, target_main.slug,
                )
                db.add(SiteSlugHistory(
                    old_slug=current_main.slug,
                    site_id=current_main.id,
                    retired_at=datetime.now(UTC),
                ))
                current_main.name = target_main.name
                current_main.slug = target_main.slug
                await db.flush()

    # ---- 1. Upsert sites.
    slug_to_site: dict[str, Site] = {}
    for order, sc in enumerate(site_configs):
        result = await db.execute(select(Site).where(Site.slug == sc.slug))
        site = result.scalar_one_or_none()
        if site is None:
            site = Site(
                name=sc.name,
                slug=sc.slug,
                is_main=False,  # main flag resolved in step 4
                is_active=True,
                sort_order=order,
            )
            db.add(site)
            await db.flush()
            logger.info("Added new site: %s (slug=%s)", sc.name, sc.slug)
        else:
            site.name = sc.name
            site.sort_order = order
            site.is_active = True
        slug_to_site[sc.slug] = site

    # ---- 2. Upsert instances.
    instance_keys_seen: set[tuple[uuid.UUID, str]] = set()
    for sc in site_configs:
        site = slug_to_site[sc.slug]
        for inst_cfg in sc.instances:
            inst_result = await db.execute(
                select(PiholeInstance).where(
                    PiholeInstance.site_id == site.id,
                    PiholeInstance.name == inst_cfg.name,
                )
            )
            instance = inst_result.scalar_one_or_none()
            if instance is None:
                instance = PiholeInstance(
                    site_id=site.id,
                    name=inst_cfg.name,
                    url=inst_cfg.url,
                    api_password=inst_cfg.password,
                    color=inst_cfg.color,
                    is_active=True,
                    is_master=inst_cfg.master,
                    vip_role=inst_cfg.vip_role,
                )
                db.add(instance)
                logger.info(
                    "Added new Pi-hole instance: %s under site '%s'",
                    inst_cfg.name, sc.name,
                )
            else:
                instance.url = inst_cfg.url
                instance.api_password = inst_cfg.password
                instance.color = inst_cfg.color
                instance.is_master = inst_cfg.master
                instance.vip_role = inst_cfg.vip_role
                instance.is_active = True
                logger.debug(
                    "Updated Pi-hole instance: %s under site '%s'",
                    inst_cfg.name, sc.name,
                )
            instance_keys_seen.add((site.id, inst_cfg.name))

    # Flush so the eviction step below sees committed site_ids.
    await db.flush()

    # ---- 3a. Orphan instances within still-active sites.
    #          Instances in DB under an active site, not in YAML → deactivate.
    deactivated_instance_keys: list[str] = []
    for sc in site_configs:
        site = slug_to_site[sc.slug]
        site_inst_result = await db.execute(
            select(PiholeInstance).where(PiholeInstance.site_id == site.id)
        )
        for inst in site_inst_result.scalars().all():
            if (site.id, inst.name) not in instance_keys_seen and inst.is_active:
                inst.is_active = False
                deactivated_instance_keys.append(str(inst.id))
                logger.info(
                    "Deactivated removed Pi-hole instance: %s (site '%s')",
                    inst.name, sc.name,
                )

    # ---- 3b. Orphan sites.
    #          Sites in DB not in YAML → deactivate. Their instances cascade-
    #          deactivate because the whole site is gone.
    # selectinload: Site.instances is touched below on sites that were NOT
    # loaded earlier in this sync (orphans, by definition), and a lazy load
    # inside an AsyncSession raises MissingGreenlet.
    result = await db.execute(
        select(Site)
        .where(Site.is_active.is_(True))
        .options(selectinload(Site.instances))
    )
    orphan_sites: list[Site] = []
    for site in result.scalars().all():
        if site.slug not in site_slugs:
            orphan_sites.append(site)

    for site in orphan_sites:
        site.is_active = False
        logger.warning(
            "Deactivated removed site: %s (slug=%s) — its %d instance(s) "
            "also deactivated. Clean up via /api/sites/inactive.",
            site.name, site.slug,
            sum(1 for i in site.instances if i.is_active),
        )
        for inst in site.instances:
            if inst.is_active:
                inst.is_active = False
                deactivated_instance_keys.append(str(inst.id))

    # ---- 4. Main reassignment.
    target_main_slug = next((sc.slug for sc in site_configs if sc.main), None)
    if target_main_slug is None:
        # load_site_configs guarantees at least one site.main=True when there
        # are sites, so this path only runs when site_configs is empty —
        # already handled above.
        pass
    else:
        new_main = slug_to_site[target_main_slug]

        # Current Main in DB.
        result = await db.execute(
            select(Site).where(Site.is_main.is_(True), Site.is_active.is_(True))
        )
        current_main = result.scalar_one_or_none()

        if current_main is None:
            # Fresh install or migration-created Default was just deactivated
            # as an orphan. Promote target directly.
            new_main.is_main = True
            logger.info("Designated site '%s' as Main.", new_main.name)
        elif current_main.id != new_main.id:
            logger.info(
                "Main reassignment: '%s' → '%s'. Materializing inherited "
                "settings into new Main.",
                current_main.name, new_main.name,
            )
            await _materialize_main_settings(db, current_main.id, new_main.id)
            current_main.is_main = False
            new_main.is_main = True
            await db.flush()

    await db.commit()

    # ---- 5. Evict clients for deactivated instances.
    for key in deactivated_instance_keys:
        try:
            await close_client(key, logout=True)
        except Exception as exc:
            logger.warning(
                "Failed to close client for deactivated instance %s: %s", key, exc,
            )

    # Per-site VIP cluster summary — logged at startup so the operator can
    # confirm at a glance that vip_master / vip_replica YAML flags were
    # picked up. Only sites with at least one VIP-flagged instance get a
    # line; non-VIP deployments stay quiet.
    for sc in site_configs:
        master = next((i.name for i in sc.instances if i.vip_role == "master"), None)
        replicas = [i.name for i in sc.instances if i.vip_role == "replica"]
        if master or replicas:
            logger.info(
                "VIP cluster in site '%s': vip_master=%s, vip_replicas=%s",
                sc.name,
                master or "(none)",
                replicas or "(none)",
            )

    # Every YAML site is active after this sync; orphans are DB-only rows
    # and never overlap with site_configs.
    active_site_count = len(site_configs)
    total_instances = sum(len(sc.instances) for sc in site_configs)
    logger.info(
        "Config sync complete. %d site(s) active, %d instance(s).",
        active_site_count, total_instances,
    )


async def _materialize_main_settings(
    db: AsyncSession, old_main_id: uuid.UUID, new_main_id: uuid.UUID,
) -> None:
    """Copy every (key, value) from old Main's site_settings into new Main,
    but only where new Main has NULL or no row for that key.

    Preserves inheritance semantics for non-Main sites: they continue to
    resolve NULL values via the new Main, which now holds the values they
    previously inherited from the old Main.
    """
    result = await db.execute(
        select(SiteSetting).where(SiteSetting.site_id == old_main_id)
    )
    old_rows = result.scalars().all()
    if not old_rows:
        return

    for row in old_rows:
        if row.value is None:
            # Old Main was itself inheriting (shouldn't happen, but skip).
            continue
        stmt = (
            pg_insert(SiteSetting)
            .values(site_id=new_main_id, key=row.key, value=row.value)
            .on_conflict_do_update(
                index_elements=["site_id", "key"],
                set_={"value": row.value},
                where=SiteSetting.value.is_(None),
            )
        )
        await db.execute(stmt)


# Back-compat alias so external callers (and the startup path prior to this
# phase) still work. Tests, CI, and any future rollback path can use the
# original name.
sync_instances = sync_sites_and_instances
