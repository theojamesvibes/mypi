"""Per-site VIP cluster state machine.

Tracks which node in a vip_master/vip_replica cluster is currently serving
traffic, fires a transfer alert when the active node changes, and fires a
group-stall alert when the whole cluster goes flat. State dicts live in
`state.py`; per-instance stall detection for VIP nodes is suppressed in
stats.py in favour of the cluster-level check here.
"""
from __future__ import annotations

import logging
import uuid

from app.models.pihole import PiholeInstance, StatsSnapshot
from app.services import pushover as pushover_service
from app.services.collector import state
from app.services.collector.state import (
    _STALL_THRESHOLD_POLLS,
    _VIP_TRANSFER_CONFIRM_POLLS,
    _site_poll_seq,
    _vip_active_node,
    _vip_advance_streak,
    _vip_group_stall_alerted,
    _vip_last_advance_seq,
)

logger = logging.getLogger(__name__)


async def _check_vip_state(
    site_id: uuid.UUID,
    site_name: str,
    poll_outcomes: list[tuple[PiholeInstance, StatsSnapshot, bool | None]],
    configured_vip_count: int,
) -> None:
    """Per-site VIP cluster detector.

    Runs after every site stats poll. Looks at the subset of instances in
    `poll_outcomes` whose `vip_role` is "master" or "replica" and:

      1. Maintains `_vip_last_advance_seq` for each VIP node.
      2. Identifies the "currently active" node (most-recently-advanced).
      3. Fires `notify_vip_transfer` when the active node changes, gated
         by `_VIP_TRANSFER_CONFIRM_POLLS` consecutive advancing polls on
         the candidate so a single transient doesn't bounce the active
         label.
      4. Fires `notify_vip_group_stalled` when every node in the cluster
         has gone flat for `_STALL_THRESHOLD_POLLS` polls (the whole VIP
         is dead). Recovery clears the alert; a future flat run can fire
         again.

    Bootstrap: a cluster that has *never* seen any node advance since
    process start can't distinguish "fresh install with no traffic yet"
    from "real outage." We require at least one observed advance from
    any node in the cluster before the group-stall alert can fire.
    Truly-dead instances are still caught by the existing offline check.
    """
    vip_outcomes = [
        (inst, snap, adv) for inst, snap, adv in poll_outcomes
        if inst.vip_role in ("master", "replica") and snap.status == "online"
    ]
    if not vip_outcomes:
        return

    seq = _site_poll_seq.get(site_id, 0) + 1
    _site_poll_seq[site_id] = seq

    # Update per-instance advance bookkeeping.
    any_advanced_this_poll = False
    ever_advanced = False
    for inst, _snap, advanced in vip_outcomes:
        key = str(inst.id)
        if advanced:
            _vip_last_advance_seq[key] = seq
            _vip_advance_streak[key] = _vip_advance_streak.get(key, 0) + 1
            any_advanced_this_poll = True
        elif advanced is False:
            _vip_advance_streak[key] = 0
        # advanced is None on bootstrap → don't update streak yet.
        if key in _vip_last_advance_seq:
            ever_advanced = True

    # Seed the active node on the very first poll where we can determine
    # one. Prefer the configured vip_master so a fresh start doesn't
    # mis-label whichever replica happens to advance first.
    if site_id not in _vip_active_node:
        configured_master = next(
            (inst for inst, _, _ in vip_outcomes if inst.vip_role == "master"),
            None,
        )
        _vip_active_node[site_id] = (
            configured_master.id if configured_master else None
        )

    # Transfer detection — pick the node with the highest streak that
    # also has the most recent advance. If the current active node also
    # advanced this poll, it stays put; only when *another* node has
    # been advancing for `_VIP_TRANSFER_CONFIRM_POLLS` polls in a row
    # while the current active has gone idle do we declare a transfer.
    current_active_id = _vip_active_node.get(site_id)
    current_active_inst = next(
        (inst for inst, _, _ in vip_outcomes if inst.id == current_active_id),
        None,
    )
    current_active_idle = (
        current_active_inst is None
        or _vip_advance_streak.get(str(current_active_inst.id), 0) == 0
    )

    if current_active_idle and any_advanced_this_poll:
        # Look for a confirmed challenger.
        challenger: PiholeInstance | None = None
        for inst, _snap, _adv in vip_outcomes:
            if inst.id == current_active_id:
                continue
            streak = _vip_advance_streak.get(str(inst.id), 0)
            # Prefer the configured vip_master if multiple nodes
            # qualify — closest match to "the cluster's normal state."
            if streak >= _VIP_TRANSFER_CONFIRM_POLLS and (
                challenger is None or inst.vip_role == "master"
            ):
                challenger = inst

        if challenger is not None and challenger.id != current_active_id:
            old_name = (
                current_active_inst.name if current_active_inst else "unknown"
            )
            logger.warning(
                "VIP transfer in site '%s': active node %s → %s "
                "(streak=%d).",
                site_name, old_name, challenger.name,
                _vip_advance_streak.get(str(challenger.id), 0),
            )
            _vip_active_node[site_id] = challenger.id
            state._spawn(pushover_service.notify_vip_transfer(
                old_name=old_name,
                new_name=challenger.name,
                site_name=site_name,
                site_id=site_id,
            ))

    # Group stall — every VIP node has been flat for >= threshold polls
    # AND the cluster has at least one historical advance to anchor
    # against (so a fresh install with no traffic doesn't fire).
    # Require positive observation of every configured VIP node this poll:
    # an offline/missing node returns no signal, and treating that as "flat"
    # combined with a quiet standby produced false alerts on transient
    # master TLS blips. Per-instance offline alerts cover real outages.
    all_observed_online = len(vip_outcomes) == configured_vip_count
    if ever_advanced and all_observed_online:
        all_flat = all(
            seq - _vip_last_advance_seq.get(str(inst.id), seq)
            >= _STALL_THRESHOLD_POLLS
            for inst, _snap, _adv in vip_outcomes
        )
        if all_flat and not _vip_group_stall_alerted.get(site_id):
            _vip_group_stall_alerted[site_id] = True
            names = ", ".join(inst.name for inst, _, _ in vip_outcomes)
            logger.error(
                "VIP cluster stalled in site '%s' — every node (%s) has "
                "been flat for >= %d polls. The whole VIP appears dead.",
                site_name, names, _STALL_THRESHOLD_POLLS,
            )
            state._spawn(pushover_service.notify_vip_group_stalled(
                names=names, site_name=site_name, site_id=site_id,
            ))
        elif any_advanced_this_poll and _vip_group_stall_alerted.get(site_id):
            _vip_group_stall_alerted[site_id] = False
            logger.info("VIP cluster in site '%s' recovered.", site_name)
            state._spawn(pushover_service.notify_vip_group_recovered(
                site_name=site_name, site_id=site_id,
            ))
