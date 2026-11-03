"""Unit tests for the per-key config exclusion helpers in sync_service.

These are the functions that decide which slices of a replica's
`pihole.toml` survive a teleporter import. Getting them wrong is silent
and destructive — a dropped key means a replica's local DNS is quietly
replaced by the master's — so they're pinned here without needing a DB
or an HTTP mock.
"""
from __future__ import annotations

import io
import tomllib
import zipfile

import pytest

from app.services.sync_service import (
    MAX_CONFIG_EXCLUSIONS,
    _merge_into,
    _nest,
    _pluck,
    build_preserve_payload,
    normalise_exclusions,
    strip_keys_from_teleporter,
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


# ── strip_keys_from_teleporter ───────────────────────────────────────────────
#
# The shape FTL actually writes: comments, indented tables, multi-line
# arrays with "### CHANGED" trailers, nested tables, and a webserver.port
# that FTL treats as host-only on import.

_FTL_TOML = """# This is Pi-hole's main configuration file.
# Last updated by FTL v6.7.1

[dns]
  upstreams = [
    "127.0.0.1#5335"
  ] ### CHANGED, default = []
  CNAMEdeep = true
  interface = "eth0" ### CHANGED, default = ""
  hosts = [
    "192.168.1.5 nas.lan",
    "192.168.1.6 \\"quoted\\".lan"
  ] ### CHANGED, default = []
  listeningMode = "LOCAL"
  port = 53
  cnameRecords = []

  [dns.rateLimit]
    count = 1000
    interval = 60

[dhcp]
  active = false
  start = ""
  leaseTime = "24h"

[webserver]
  domain = "pi.hole"
  port = "8080o,8443os,[::]:8080o,[::]:8443os" ### CHANGED, default = "80o,443os,[::]:80o,[::]:443os"
  threads = 50

  [webserver.api]
    max_sessions = 16
    app_sudo = true

[misc]
  nice = -10
"""

_GRAVITY_BYTES = b"SQLite format 3\0" + b"\0" * 3000


def _archive(toml: str | bytes = _FTL_TOML, member: str = "etc/pihole/pihole.toml") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(member, toml)
        zf.writestr("etc/pihole/gravity.db", _GRAVITY_BYTES)
        zf.writestr("etc/pihole/dhcp.leases", b"")
        zf.comment = b"Pi-hole Teleporter archive"
    return buf.getvalue()


def _toml_in(archive: bytes) -> dict:
    with zipfile.ZipFile(io.BytesIO(archive)) as zf:
        return tomllib.loads(zf.read("etc/pihole/pihole.toml").decode("utf-8"))


def test_strip_removes_only_the_pinned_keys():
    out, stripped = strip_keys_from_teleporter(
        _archive(), ["dns.upstreams", "webserver.port", "dns.rateLimit.count"]
    )
    assert stripped == ["dns.upstreams", "webserver.port", "dns.rateLimit.count"]
    tree = _toml_in(out)
    assert "upstreams" not in tree["dns"]
    assert "port" not in tree["webserver"]
    assert "count" not in tree["dns"]["rateLimit"]
    # Siblings at every level survive, with their values intact.
    assert tree["dns"]["port"] == 53
    assert tree["dns"]["interface"] == "eth0"
    assert tree["dns"]["hosts"] == ["192.168.1.5 nas.lan", '192.168.1.6 "quoted".lan']
    assert tree["dns"]["rateLimit"]["interval"] == 60
    assert tree["webserver"]["domain"] == "pi.hole"
    assert tree["webserver"]["api"] == {"max_sessions": 16, "app_sudo": True}
    assert tree["dhcp"] == {"active": False, "start": "", "leaseTime": "24h"}
    assert tree["misc"]["nice"] == -10


def test_strip_removes_a_whole_section():
    out, stripped = strip_keys_from_teleporter(_archive(), ["dhcp"])
    assert stripped == ["dhcp"]
    tree = _toml_in(out)
    assert "dhcp" not in tree
    assert tree["dns"]["upstreams"] == ["127.0.0.1#5335"]


def test_strip_reports_only_keys_the_archive_carries():
    """A pinned key this master doesn't have is nothing to cut — and not an
    error, exactly as build_preserve_payload treats it on the replica."""
    out, stripped = strip_keys_from_teleporter(_archive(), ["nope.key", "dns.interface", "dns.nope"])
    assert stripped == ["dns.interface"]
    assert "interface" not in _toml_in(out)["dns"]


def test_strip_returns_the_original_bytes_when_nothing_matches():
    data = _archive()
    out, stripped = strip_keys_from_teleporter(data, ["nope.key"])
    assert stripped == []
    assert out is data


def test_strip_copies_every_other_member_through_unchanged():
    out, _ = strip_keys_from_teleporter(_archive(), ["dns.hosts"])
    with zipfile.ZipFile(io.BytesIO(out)) as zf:
        assert zf.testzip() is None
        assert zf.namelist() == [
            "etc/pihole/pihole.toml", "etc/pihole/gravity.db", "etc/pihole/dhcp.leases",
        ]
        assert zf.read("etc/pihole/gravity.db") == _GRAVITY_BYTES
        assert zf.read("etc/pihole/dhcp.leases") == b""
        assert zf.getinfo("etc/pihole/gravity.db").compress_type == zipfile.ZIP_DEFLATED
        assert zf.comment == b"Pi-hole Teleporter archive"


def test_strip_raises_when_the_archive_has_no_pihole_toml():
    """The caller falls back to sending the archive as exported — so this
    must raise rather than quietly return an archive that still carries the
    pinned keys while claiming it does not."""
    with pytest.raises(RuntimeError, match="etc/pihole/pihole.toml"):
        strip_keys_from_teleporter(_archive(member="pihole.toml"), ["dns.hosts"])


def test_strip_raises_on_toml_it_cannot_parse():
    with pytest.raises(tomllib.TOMLDecodeError):
        strip_keys_from_teleporter(_archive(toml="not = toml = at all"), ["dns.hosts"])
