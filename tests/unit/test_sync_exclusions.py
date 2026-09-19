"""Unit tests for the per-key config exclusion helpers in sync_service.

These are the functions that decide which slices of a replica's
`pihole.toml` survive a teleporter import. Getting them wrong is silent
and destructive — a dropped key means a replica's local DNS is quietly
replaced by the master's — so they're pinned here without needing a DB
or an HTTP mock.
"""
from __future__ import annotations

from app.services.sync_service import (
    MAX_CONFIG_EXCLUSIONS,
    _merge_into,
    _nest,
    _pluck,
    build_preserve_payload,
    normalise_exclusions,
)

# ── normalise_exclusions ─────────────────────────────────────────────────────


def test_normalise_keeps_order_and_dedupes():
    assert normalise_exclusions(
        ["dns.hosts", "dns.upstreams", "dns.hosts"]
    ) == ["dns.hosts", "dns.upstreams"]


def test_normalise_trims_whitespace_and_stray_dots():
    assert normalise_exclusions(["  dns.hosts  ", ".dhcp."]) == ["dns.hosts", "dhcp"]


def test_normalise_drops_malformed_keys_but_keeps_the_rest():
    """A typo in one key must not cost the user the keys they got right."""
    out = normalise_exclusions(["dns.hosts", "dns..hosts", "dns hosts", "", "dhcp"])
    assert out == ["dns.hosts", "dhcp"]


def test_normalise_rejects_path_traversal_and_injection_shapes():
    assert normalise_exclusions(["../../etc/passwd", "dns.hosts; rm -rf /"]) == []


def test_normalise_handles_none_and_non_lists():
    assert normalise_exclusions(None) == []
    assert normalise_exclusions("dns.hosts") == []
    assert normalise_exclusions([1, None, {"dns": "hosts"}]) == []


def test_normalise_caps_the_list_length():
    out = normalise_exclusions([f"dns.key{i}" for i in range(MAX_CONFIG_EXCLUSIONS + 25)])
    assert len(out) == MAX_CONFIG_EXCLUSIONS


# ── _pluck / _nest / _merge_into ─────────────────────────────────────────────


def test_pluck_reads_nested_values():
    tree = {"dns": {"hosts": ["10.0.0.1 nas"], "upstreams": ["1.1.1.1"]}}
    assert _pluck(tree, "dns.hosts") == (True, ["10.0.0.1 nas"])
    assert _pluck(tree, "dns") == (True, tree["dns"])


def test_pluck_reports_missing_paths_rather_than_raising():
    tree = {"dns": {"hosts": []}}
    assert _pluck(tree, "dns.cnameRecords") == (False, None)
    assert _pluck(tree, "dhcp.active") == (False, None)
    # Walking *through* a non-dict must not explode.
    assert _pluck({"dns": {"hosts": ["a"]}}, "dns.hosts.0") == (False, None)


def test_pluck_distinguishes_missing_from_falsy():
    """An empty list is a real value: a replica with no local DNS records
    must still have that emptiness preserved, not treated as 'not set'."""
    found, value = _pluck({"dns": {"hosts": []}}, "dns.hosts")
    assert found is True
    assert value == []


def test_nest_is_the_inverse_of_pluck():
    nested = _nest("dns.hosts", ["10.0.0.1 nas"])
    assert nested == {"dns": {"hosts": ["10.0.0.1 nas"]}}
    assert _pluck(nested, "dns.hosts") == (True, ["10.0.0.1 nas"])


def test_nest_handles_a_single_segment():
    assert _nest("dhcp", {"active": True}) == {"dhcp": {"active": True}}


def test_merge_into_keeps_siblings_under_one_section():
    dst = {"dns": {"hosts": ["a"]}}
    _merge_into(dst, {"dns": {"upstreams": ["1.1.1.1"]}})
    assert dst == {"dns": {"hosts": ["a"], "upstreams": ["1.1.1.1"]}}


# ── build_preserve_payload ───────────────────────────────────────────────────


CONFIG = {
    "dns": {
        "hosts": ["10.0.0.5 nas.lan"],
        "cnameRecords": ["www.lan,nas.lan"],
        "upstreams": ["192.168.1.1"],
    },
    "dhcp": {"active": True, "start": "10.0.0.100"},
    "misc": {"nice": -10},
}


def test_build_payload_merges_sibling_keys_into_one_patch():
    payload, found = build_preserve_payload(CONFIG, ["dns.hosts", "dns.upstreams"])
    assert payload == {
        "dns": {"hosts": ["10.0.0.5 nas.lan"], "upstreams": ["192.168.1.1"]}
    }
    assert found == ["dns.hosts", "dns.upstreams"]


def test_build_payload_carries_a_whole_section():
    payload, found = build_preserve_payload(CONFIG, ["dhcp"])
    assert payload == {"dhcp": {"active": True, "start": "10.0.0.100"}}
    assert found == ["dhcp"]


def test_build_payload_skips_keys_absent_from_this_pihole():
    """A key this Pi-hole version doesn't have has nothing to preserve —
    skip it rather than failing the whole sync over it."""
    payload, found = build_preserve_payload(CONFIG, ["dns.hosts", "dns.notAThing"])
    assert payload == {"dns": {"hosts": ["10.0.0.5 nas.lan"]}}
    assert found == ["dns.hosts"]


def test_build_payload_is_empty_when_nothing_matches():
    assert build_preserve_payload(CONFIG, ["nope.at.all"]) == ({}, [])
    assert build_preserve_payload(CONFIG, []) == ({}, [])


def test_build_payload_is_independent_of_the_source_config():
    """The payload is a snapshot: editing it must not reach back into the
    replica's parsed config, and overlapping paths must not either."""
    source = {"dhcp": {"active": True, "start": "10.0.0.100"}}
    payload, found = build_preserve_payload(source, ["dhcp", "dhcp.active"])
    assert found == ["dhcp", "dhcp.active"]
    payload["dhcp"]["start"] = "mutated"
    assert source["dhcp"]["start"] == "10.0.0.100"
