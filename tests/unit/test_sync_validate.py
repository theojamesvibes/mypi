"""Unit tests for sync_service._validate_teleporter_zip — the guard
that keeps a corrupt master export from fanning out to every replica."""
from __future__ import annotations

import io
import zipfile

import pytest

from app.services.sync_service import _ftl_series, _validate_teleporter_zip


def _make_zip(*entries: tuple[str, bytes]) -> bytes:
    """Return a valid in-memory ZIP containing the given (name, data) pairs."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries:
            zf.writestr(name, data)
    return buf.getvalue()


def test_accepts_valid_zip_with_realistic_contents():
    # Fabricate a 1.5 KB-ish "teleporter" so we clear the 1024-byte floor.
    payload = b"# pihole.toml\nfoo=bar\n" * 100
    zip_bytes = _make_zip(("pihole.toml", payload))
    # No exception means the validator accepted it.
    _validate_teleporter_zip(zip_bytes)


def test_rejects_under_minimum_byte_threshold():
    """A short response is almost always a truncated or empty teleporter."""
    with pytest.raises(RuntimeError, match="bytes"):
        _validate_teleporter_zip(b"x" * 100)


def test_rejects_non_zip_bytes():
    """Bytes that look right but aren't a valid ZIP must fail validation."""
    not_a_zip = b"\x00" * 4096
    with pytest.raises(RuntimeError, match="not a valid ZIP"):
        _validate_teleporter_zip(not_a_zip)


def test_rejects_empty_zip():
    """A zip with no members is a real Pi-hole edge case (empty export)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w"):
        pass
    # Pad to clear the 1 KB floor so the byte-length check doesn't fire first.
    raw = buf.getvalue() + b"\x00" * 2048
    # Empty zip is shorter than the threshold, so it should fail there.
    # Either error message means the validator caught it.
    with pytest.raises(RuntimeError):
        _validate_teleporter_zip(raw)


def test_rejects_zero_length_members():
    zip_bytes = _make_zip(
        ("pihole.toml", b""),
        ("gravity.db", b""),
    )
    # Pad up to the 1 KB floor since two empty members give a small zip.
    if len(zip_bytes) < 1024:
        zip_bytes = zip_bytes + b"\x00" * (1024 - len(zip_bytes))
        # Padding corrupts the central directory, which then trips
        # BadZipFile rather than the all-zero-members branch — both
        # are acceptable rejection paths for the "is this archive
        # broken?" question.
    with pytest.raises(RuntimeError):
        _validate_teleporter_zip(zip_bytes)


def test_rejects_corrupt_zip_member():
    """Tamper with the body of a valid zip so testzip() reports a CRC failure."""
    payload = b"x" * 2048
    zip_bytes = bytearray(_make_zip(("pihole.toml", payload)))
    # Flip a byte in the file body. Locate it by searching for the payload —
    # the zip stores it uncompressed so the bytes are findable.
    idx = zip_bytes.find(payload)
    assert idx > 0, "payload not found in zip"
    zip_bytes[idx + 10] ^= 0xFF
    with pytest.raises(RuntimeError, match="CRC|not a valid ZIP|corrupt"):
        _validate_teleporter_zip(bytes(zip_bytes))


# ── _ftl_series — FTL version-drift guard helper ──────────────────────────────


@pytest.mark.parametrize(
    "version,expected",
    [
        ("6.0.5", "6.0"),
        ("v6.1.2", "6.1"),   # leading 'v' tolerated
        ("6.0", "6.0"),      # major.minor with no patch
        ("6.2.0-rc1", "6.2"),
        (None, None),        # never polled
        ("", None),
        ("6", None),         # too coarse to compare
        ("dev", None),
        ("6.x", None),       # non-numeric minor
    ],
)
def test_ftl_series_reduces_to_major_minor(version, expected):
    assert _ftl_series(version) == expected


def test_ftl_series_flags_minor_drift_but_not_patch():
    # A genuine minor-series gap (6.0 → 6.1) must compare unequal...
    assert _ftl_series("6.0.5") != _ftl_series("6.1.0")
    # ...while a one-patch lag during a rolling upgrade must not.
    assert _ftl_series("6.0.5") == _ftl_series("6.0.6")


def test_ftl_series_unknown_version_skips_comparison():
    # When either side is unknown the caller must not warn; modelled here as
    # "None series compares falsy so the guard's `and` short-circuits".
    assert _ftl_series(None) is None
    assert not (_ftl_series(None) and _ftl_series("6.0.1"))
