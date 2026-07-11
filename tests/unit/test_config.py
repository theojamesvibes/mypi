"""Unit tests for app/config.py — slug helpers, instance/site parsing,
and the YAML loader (including the wave-3 loud-fail and perms warning)."""
from __future__ import annotations

import logging
import os
import textwrap
from pathlib import Path

import pytest

from app import config

# ── slugify ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("name, expected", [
    ("WTR Site", "wtr-site"),
    ("MNAve", "mnave"),
    ("Multi   spaces!", "multi-spaces"),
    ("  ---trim---  ", "trim"),
    ("", ""),
    ("UPPER", "upper"),
    ("123 numeric", "123-numeric"),
])
def test_slugify(name, expected):
    assert config.slugify(name) == expected


def test_slugify_truncates_to_64_chars():
    long_name = "a" * 200
    result = config.slugify(long_name)
    assert len(result) == 64
    assert result == "a" * 64


# ── validate_slug ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("slug", [
    "wtr",
    "mnave",
    "site-1",
    "abc-123-def",
    "a",
])
def test_validate_slug_accepts_valid(slug):
    config.validate_slug(slug, source="test")  # no exception


@pytest.mark.parametrize("slug, reason", [
    ("", "empty"),
    ("a" * 65, "too long"),
    ("UPPER", "uppercase"),
    ("has space", "space"),
    ("has_underscore", "underscore"),
    ("trailing-", "trailing hyphen"),
    ("-leading", "leading hyphen"),
    ("double--hyphen", "consecutive hyphens"),
])
def test_validate_slug_rejects_invalid(slug, reason):
    with pytest.raises(ValueError):
        config.validate_slug(slug, source="test")


@pytest.mark.parametrize("slug", sorted(config.RESERVED_SLUGS))
def test_validate_slug_rejects_reserved(slug):
    with pytest.raises(ValueError, match="reserved"):
        config.validate_slug(slug, source="test")


# ── _parse_instance ──────────────────────────────────────────────────────────


def test_parse_instance_minimum_fields():
    inst = config._parse_instance({"name": "p1", "url": "http://192.168.1.1"})
    assert inst.name == "p1"
    assert inst.url == "http://192.168.1.1"
    assert inst.password == ""
    assert inst.master is False
    assert inst.vip_role is None


def test_parse_instance_strips_trailing_slash_from_url():
    inst = config._parse_instance({"name": "p", "url": "http://x/"})
    assert inst.url == "http://x"


def test_parse_instance_master_flag():
    inst = config._parse_instance({
        "name": "master", "url": "http://x", "master": True,
    })
    assert inst.master is True


def test_parse_instance_vip_master_role():
    inst = config._parse_instance({
        "name": "p", "url": "http://x", "vip_master": True,
    })
    assert inst.vip_role == "master"


def test_parse_instance_vip_replica_role():
    inst = config._parse_instance({
        "name": "p", "url": "http://x", "vip_replica": True,
    })
    assert inst.vip_role == "replica"


def test_parse_instance_both_vip_flags_resolves_to_none(caplog):
    """vip_master + vip_replica is a YAML mistake — neither role applies
    and the operator gets a warning."""
    with caplog.at_level(logging.WARNING):
        inst = config._parse_instance({
            "name": "confused", "url": "http://x",
            "vip_master": True, "vip_replica": True,
        })
    assert inst.vip_role is None
    assert any("vip_master and vip_replica" in r.message for r in caplog.records)


# ── _parse_site ──────────────────────────────────────────────────────────────


def test_parse_site_derives_slug_from_name():
    site = config._parse_site({"name": "WTR Test", "instances": []}, fallback_index=0)
    assert site.slug == "wtr-test"


def test_parse_site_honours_explicit_slug():
    site = config._parse_site({
        "name": "Anything", "slug": "custom", "instances": [],
    }, fallback_index=0)
    assert site.slug == "custom"


def test_parse_site_default_site_alias_sets_main():
    site = config._parse_site({
        "name": "S", "default_site": True, "instances": [],
    }, fallback_index=0)
    assert site.main is True


def test_parse_site_main_flag_sets_main():
    site = config._parse_site({
        "name": "S", "main": True, "instances": [],
    }, fallback_index=0)
    assert site.main is True


def test_parse_site_demotes_extra_vip_masters(caplog):
    """One vip_master per site — extras are demoted with a warning."""
    yaml_data = {
        "name": "Cluster",
        "instances": [
            {"name": "a", "url": "http://a", "vip_master": True},
            {"name": "b", "url": "http://b", "vip_master": True},
        ],
    }
    with caplog.at_level(logging.WARNING):
        site = config._parse_site(yaml_data, fallback_index=0)
    roles = [i.vip_role for i in site.instances]
    assert roles == ["master", None]  # winner first, loser demoted
    assert any("multiple vip_master" in r.message for r in caplog.records)


# ── load_site_configs — happy paths ──────────────────────────────────────────


def _write_yaml(tmp_path: Path, contents: str) -> Path:
    p = tmp_path / "pihole_instances.yml"
    p.write_text(textwrap.dedent(contents).lstrip())
    return p


def test_load_site_configs_returns_empty_for_missing_file(tmp_path: Path):
    missing = tmp_path / "nope.yml"
    assert config.load_site_configs(str(missing)) == []


def test_load_site_configs_returns_empty_for_blank_file(tmp_path: Path):
    p = _write_yaml(tmp_path, "")
    assert config.load_site_configs(str(p)) == []


def test_load_site_configs_parses_legacy_instances_format(tmp_path: Path):
    p = _write_yaml(tmp_path, """
        instances:
          - name: pihole1
            url: http://192.168.1.1
            password: secret
    """)
    sites = config.load_site_configs(str(p))
    assert len(sites) == 1
    assert sites[0].slug == "default"
    assert sites[0].main is True
    assert len(sites[0].instances) == 1
    assert sites[0].instances[0].name == "pihole1"


def test_load_site_configs_parses_sites_format(tmp_path: Path):
    p = _write_yaml(tmp_path, """
        sites:
          - name: Site A
            main: true
            instances:
              - name: a1
                url: http://a1
          - name: Site B
            instances:
              - name: b1
                url: http://b1
    """)
    sites = config.load_site_configs(str(p))
    assert len(sites) == 2
    assert sites[0].name == "Site A"
    assert sites[0].main is True
    assert sites[1].name == "Site B"
    assert sites[1].main is False


def test_load_site_configs_promotes_first_when_no_main_flagged(tmp_path: Path):
    p = _write_yaml(tmp_path, """
        sites:
          - name: First
            instances: []
          - name: Second
            instances: []
    """)
    sites = config.load_site_configs(str(p))
    assert sites[0].main is True
    assert sites[1].main is False


def test_load_site_configs_demotes_extra_main_flagged_sites(tmp_path: Path, caplog):
    p = _write_yaml(tmp_path, """
        sites:
          - name: First
            main: true
            instances: []
          - name: Second
            main: true
            instances: []
    """)
    with caplog.at_level(logging.WARNING):
        sites = config.load_site_configs(str(p))
    assert sites[0].main is True
    assert sites[1].main is False
    assert any("Multiple sites flagged main" in r.message for r in caplog.records)


def test_load_site_configs_skips_duplicate_slugs(tmp_path: Path, caplog):
    p = _write_yaml(tmp_path, """
        sites:
          - name: Same
            slug: dup
            main: true
            instances: []
          - name: Other
            slug: dup
            instances: []
    """)
    with caplog.at_level(logging.WARNING):
        sites = config.load_site_configs(str(p))
    assert len(sites) == 1
    assert sites[0].name == "Same"
    assert any("Duplicate slug" in r.message for r in caplog.records)


# ── load_site_configs — wave-3 hardening ─────────────────────────────────────


def test_load_site_configs_raises_on_yaml_parse_error(tmp_path: Path, caplog):
    """Closes #24 — refuse to start so the operator notices."""
    # Unclosed flow-sequence is a hard parse error — yaml.safe_load
    # raises ScannerError/ParserError at the unexpected EOF.
    p = tmp_path / "broken.yml"
    p.write_text("sites: [\n  - name: A\n")
    with caplog.at_level(logging.ERROR), pytest.raises(RuntimeError) as exc_info:
        config.load_site_configs(str(p))
    # Error message includes file path and line/column for grep-ability.
    msg = str(exc_info.value)
    assert "Could not parse" in msg
    assert "line" in msg and "column" in msg
    # The ERROR-level log must mention the file so operators can find it.
    assert any("Failed to parse" in r.message for r in caplog.records)


def test_load_site_configs_warns_on_permissive_perms(tmp_path: Path, caplog):
    p = _write_yaml(tmp_path, "instances: []")
    os.chmod(p, 0o644)  # group + other read bits set
    with caplog.at_level(logging.WARNING):
        config.load_site_configs(str(p))
    assert any("permissive file mode" in r.message for r in caplog.records)


def test_load_site_configs_no_warn_on_tight_perms(tmp_path: Path, caplog):
    p = _write_yaml(tmp_path, "instances: []")
    os.chmod(p, 0o600)
    with caplog.at_level(logging.WARNING):
        config.load_site_configs(str(p))
    assert not any("permissive file mode" in r.message for r in caplog.records)
