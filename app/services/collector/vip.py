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
    _VIP_DOMINANCE_SHARE,
    _VIP_TRANSFER_CONFIRM_POLLS,
    _site_poll_seq,
    _vip_active_node,
    _vip_group_stall_alerted,
    _vip_last_advance_seq,
    _vip_lead_streak,
    _vip_prev_count,
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

    # Per-poll bookkeeping.
    #   - `_vip_last_advance_seq` / `any_advanced` / `ever_advanced` drive the
    #     group-stall check below (did the *whole* cluster go flat?), keyed off
    #     the boolean `advanced` signal.
    #   - `deltas[instance_id]` is each node's query *volume* this poll — the
    #     signal that distinguishes the VIP holder (serves the bulk of the
    #     cluster's traffic) from a standby (residual direct queries only).
    #     A boolean "did it advance at all?" can't: where a standby's real IP
    #     is a client's secondary resolver it advances every poll too, so both
    #     nodes look permanently active and the old streak-of-advances logic
    #     flapped the active label on any single incumbent blip.
    any_advanced_this_poll = False
    ever_advanced = False
    deltas: dict[uuid.UUID, int] = {}
    for inst, snap, advanced in vip_outcomes:
        key = str(inst.id)
        if advanced:
            _vip_last_advance_seq[key] = seq
            any_advanced_this_poll = True
        # advanced is None on bootstrap → don't touch stall bookkeeping yet.
        if key in _vip_last_advance_seq:
            ever_advanced = True

        new_count = snap.dns_queries_today or 0
        prev_count = _vip_prev_count.get(key)
        _vip_prev_count[key] = new_count
        if prev_count is None or new_count < prev_count:
            # Bootstrap poll, or a counter rollover (midnight / FTL restart):
            # no meaningful volume delta to attribute this poll.
            deltas[inst.id] = 0
        else:
            deltas[inst.id] = new_count - prev_count

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

    current_active_id = _vip_active_node.get(site_id)

    # Active-node / transfer detection by query-volume dominance. Find the
    # node carrying the dominant share of this poll's cluster traffic; a
    # transfer is confirmed only once a *different* node has held that share
    # for `_VIP_TRANSFER_CONFIRM_POLLS` consecutive polls. A single idle poll
    # on the incumbent momentarily hands the lead to a chattering standby, but
    # the streak resets the moment the incumbent serves the majority again, so
    # it never reaches the confirm gate — killing the phantom flapping.
    total_delta = sum(deltas.values())
    dominant_id: uuid.UUID | None = None
    if total_delta > 0:
        leader_id, leader_delta = max(deltas.items(), key=lambda kv: kv[1])
        if leader_delta / total_delta >= _VIP_DOMINANCE_SHARE:
            dominant_id = leader_id

    if dominant_id is not None:
        # One node clearly owns this poll's traffic: advance its lead streak
        # and reset every other node's (dominance must be *consecutive*).
        for inst, _snap, _adv in vip_outcomes:
            key = str(inst.id)
            if inst.id == dominant_id:
                _vip_lead_streak[key] = _vip_lead_streak.get(key, 0) + 1
            else:
                _vip_lead_streak[key] = 0
    else:
        # Ambiguous poll (no majority, or the whole cluster went flat). Don't
        # let it build a challenger — reset every non-incumbent's streak.
        for inst, _snap, _adv in vip_outcomes:
            if inst.id != current_active_id:
                _vip_lead_streak[str(inst.id)] = 0

    if (
        dominant_id is not None
        and dominant_id != current_active_id
        and _vip_lead_streak.get(str(dominant_id), 0) >= _VIP_TRANSFER_CONFIRM_POLLS
    ):
        challenger = next(
            inst for inst, _, _ in vip_outcomes if inst.id == dominant_id
        )
        current_active_inst = next(
            (inst for inst, _, _ in vip_outcomes if inst.id == current_active_id),
            None,
        )
        old_name = (
            current_active_inst.name if current_active_inst else "unknown"
        )
        logger.warning(
            "VIP transfer in site '%s': active node %s → %s "
            "(lead streak=%d, share=%.0f%%).",
            site_name, old_name, challenger.name,
            _vip_lead_streak.get(str(dominant_id), 0),
            100 * deltas[dominant_id] / total_delta,
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
