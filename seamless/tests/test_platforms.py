"""Destination release series (SDD §10.1, §21.1–§21.3; QASuite S-DST): the series table, the range
a cloud's maximum microversions read as, and the support tier that follows."""

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from seamless_migrate.config import find_repo_root
from seamless_migrate.planning.platforms import (
    READ_ON,
    REVIEWED_MICROVERSIONS,
    SERIES,
    ReleaseRange,
    SupportTier,
    destination_release,
    maximum_microversion,
    release_range,
    support_tier,
)

WINDOW = ("2023.1", "2023.2", "2024.1", "2024.2", "2025.1", "2025.2", "2026.1", "2026.2")
VERSIONS = Path(__file__).parent / "fixtures" / "openstack_versions"


def _version(text):
    major, minor = text.split(".")
    return int(major), int(minor)


def test_every_series_of_the_window_has_its_phase_tier_and_compute_maximum():
    """The window runs from 2023.1 Antelope to 2026.2 Hibiscus, oldest first, one codename letter
    after another; each series' tier follows its upstream phase as SDD §21.1 and §21.2 rule it."""
    assert tuple(series.name for series in SERIES) == WINDOW
    assert [series.codename[0] for series in SERIES] == list("ABCDEFGH")
    for series in SERIES:
        expected = {
            ("Maintained", True): "tier1",
            ("Maintained", False): "tier1",
            ("Unmaintained", True): "tier2",
            ("Unmaintained", False): "tier3",
            ("End of Life", True): "tier3",
            ("End of Life", False): "tier3",
        }[(series.phase, series.slurp)]
        assert series.tier == expected, series
        assert re.fullmatch(r"2\.\d+", series.compute_max), series
    maxima = [_version(series.compute_max) for series in SERIES]
    assert maxima == sorted(maxima)
    # the floor is the oldest SLURP series not at its end of life (§21.1)
    assert SERIES[0].slurp and SERIES[0].phase != "End of Life"


@pytest.mark.parametrize(
    "compute_max, block_storage_max, series",
    [
        ("2.95", None, ("2023.1", "2023.2")),
        ("2.96", None, ("2024.1", "2024.2")),
        ("2.100", None, ("2025.1", "2025.2")),
        ("2.103", None, ("2026.1",)),
        ("2.104", None, ("2026.2",)),
        # between two series' maxima: the two series it falls between (a development snapshot)
        ("2.97", None, ("2024.2", "2025.1")),
        ("2.98", None, ("2024.2", "2025.1")),
        ("2.101", None, ("2025.2", "2026.1")),
        # block storage narrows the range, never widens it
        ("2.95", "3.70", ("2023.1", "2023.2")),
        ("2.98", "3.72", ("2025.1",)),
        ("2.98", "3.71", ("2024.2", "2025.1")),
        # a narrowing that would leave no series is not applied (Nova and Cinder of two series)
        ("2.96", "3.70", ("2024.1", "2024.2")),
        ("2.96", "not a version", ("2024.1", "2024.2")),
    ],
)
def test_a_compute_maximum_reads_as_the_series_range_it_fits(
    compute_max, block_storage_max, series
):
    assert release_range(compute_max, block_storage_max) == ReleaseRange(series)


@pytest.mark.parametrize("compute_max", ["2.105", "2.200", "2.1000"])
def test_a_maximum_above_the_newest_known_reads_as_outside_newer(compute_max):
    """Above 2026.2's 2.104 is a series no Seamless release validated (2027.1 Indri is in
    development): no series of the window, whatever a block-storage maximum says."""
    assert release_range(compute_max) == ReleaseRange((), "newer")
    assert release_range(compute_max, "3.71") == ReleaseRange((), "newer")


@pytest.mark.parametrize(
    "compute_max, outside",
    [
        ("2.94", "older"),
        ("2.93", "older"),
        # read as numbers: 2.9 is below 2.95, though "2.9" > "2.104" as text
        ("2.9", "older"),
        (None, "not_detected"),
        ("", "not_detected"),
        ("2", "not_detected"),
        ("latest", "not_detected"),
        (2.95, "not_detected"),
    ],
)
def test_a_maximum_below_the_window_or_unreadable_is_older_or_not_detected(compute_max, outside):
    """Below 2023.1's 2.95 is Zed or older, under the floor; a maximum that is not a microversion
    was not detected. Neither is a series of the window."""
    assert release_range(compute_max) == ReleaseRange((), outside)


@pytest.mark.parametrize(
    "compute_max, verdict",
    [
        ("2.95", SupportTier("tier3", "end_of_life")),  # 2023.1 (Tier 2) or 2023.2 (Tier 3)
        ("2.96", SupportTier("tier3", "end_of_life")),  # 2024.1 (Tier 2) or 2024.2 (Tier 3)
        ("2.98", SupportTier("tier3", "end_of_life")),  # 2024.2 (Tier 3) or 2025.1 (Tier 1)
        ("2.100", SupportTier("tier1")),
        ("2.103", SupportTier("tier1")),
        ("2.104", SupportTier("tier1")),
        ("2.105", SupportTier("outside", "newer_than_validated")),
        ("2.93", SupportTier("outside", "below_floor")),
        (None, SupportTier("outside", "not_detected")),
    ],
)
def test_without_a_declared_series_the_tier_is_the_least_favourable_of_the_range(
    compute_max, verdict
):
    """SDD §21.3: a range that straddles tiers never reads as the better one until its operator
    declares the series (review focus 1)."""
    assert support_tier(release_range(compute_max)) == verdict


@pytest.mark.parametrize(
    "compute_max, declared, verdict",
    [
        ("2.96", "2024.1", SupportTier("tier2")),
        ("2.96", "2024.2", SupportTier("tier3", "end_of_life")),
        ("2.95", "2023.1", SupportTier("tier2")),
        ("2.100", "2025.2", SupportTier("tier1")),
        ("2.98", "2025.1", SupportTier("tier1")),
    ],
)
def test_a_declared_series_inside_the_range_decides_the_tier(compute_max, declared, verdict):
    assert support_tier(release_range(compute_max), declared) == verdict


@pytest.mark.parametrize(
    "compute_max, declared, verdict",
    [
        ("2.96", "2026.2", SupportTier("tier3", "end_of_life", declared_outside_range=True)),
        # Nova never backports a microversion: 2024.1 never had 2.98
        ("2.98", "2024.1", SupportTier("tier3", "end_of_life", declared_outside_range=True)),
        ("2.104", "2025.1", SupportTier("tier1", declared_outside_range=True)),
        ("2.105", "2026.2", SupportTier("outside", "newer_than_validated", True)),
        ("2.93", "2023.1", SupportTier("outside", "below_floor", True)),
        # with nothing detected, any declared series is outside the (empty) range
        (None, "2026.2", SupportTier("outside", "not_detected", True)),
        ("2.104", "2022.2", SupportTier("tier1", declared_outside_range=True)),
    ],
)
def test_a_declared_series_outside_the_range_warns_and_the_range_decides(
    compute_max, declared, verdict
):
    assert support_tier(release_range(compute_max), declared) == verdict


@pytest.mark.parametrize(
    "compute_max, declared, settled",
    [
        ("2.104", None, "2026.2"),
        ("2.96", None, None),
        ("2.96", "2024.1", "2024.1"),
        ("2.96", "2026.2", None),  # declared outside the range: the range decides
        ("2.105", "2026.2", None),
        (None, "2026.2", None),
    ],
)
def test_a_destination_settles_on_the_declared_series_else_the_ranges_only_one(
    compute_max, declared, settled
):
    report = destination_release("openstack", declared, None, compute_max, None)
    assert report["release"] == settled
    assert report["release_range"] == list(release_range(compute_max).series)


@pytest.mark.parametrize(
    "kind, release, declared_profile, compute_max, profile",
    [
        ("openstack", None, None, "2.104", "secure_rbac"),
        ("openstack", None, None, "2.96", "secure_rbac"),  # 2024.1 and 2024.2 both enforce them
        ("openstack", None, None, "2.95", "unknown"),  # 2023.1 (legacy) or 2023.2 (secure-RBAC)
        ("openstack", "2023.1", None, "2.95", "legacy"),
        ("openstack", "2023.2", None, "2.95", "secure_rbac"),
        ("openstack", "2026.2", None, "2.95", "unknown"),  # declared outside: the range decides
        ("openstack", None, None, "2.93", "legacy"),  # Zed or older
        ("openstack", None, None, "2.105", "secure_rbac"),  # newer than the window
        ("openstack", None, None, None, "unknown"),  # nothing detected
        ("openstack", None, "legacy", "2.104", "legacy"),  # declared: the new defaults turned off
        ("openstack", None, "secure_rbac", "2.95", "secure_rbac"),
        ("rhoso", None, None, "2.95", "legacy"),  # D14
        ("rhoso", None, "secure_rbac", "2.95", "secure_rbac"),
    ],
)
def test_the_policy_profile_is_the_declared_one_else_by_kind_and_series(
    kind, release, declared_profile, compute_max, profile
):
    """SDD §10.1, §21.5: oslo.policy enforces the new defaults from 2023.2; a range that cannot
    tell reads unknown, never either profile."""
    report = destination_release(kind, release, declared_profile, compute_max, None)
    assert report["policy_profile"] == profile


def test_rhoso_is_validated_by_its_kind_never_by_detection():
    """SDD §10.1: RHOSO 18.0 is validated whatever its maxima read as, and a declared series never
    makes it warn; a community cloud with the same maxima reads its range's tier."""
    for compute_max, declared in (("2.95", None), ("2.95", "2026.2"), (None, None)):
        report = destination_release("rhoso", declared, None, compute_max, None)
        assert report["support_tier"] == {
            "tier": "validated",
            "reason": None,
            "declared_outside_range": False,
        }
    community = destination_release("openstack", None, None, "2.95", None)
    assert community["support_tier"] == {
        "tier": "tier3",
        "reason": "end_of_life",
        "declared_outside_range": False,
    }


def _sdd_section(heading):
    text = (find_repo_root() / "docs" / "SDD.md").read_text(encoding="utf-8")
    start = text.index(heading)
    return text[start : text.index("\n### ", start + len(heading))]


def test_the_sdd_support_table_matches_the_code_series_table():
    """SDD §21.2: §21.1's table and the code's series table change in the same commit, with the
    date they were read; §21.3's compute and block-storage maxima are the code's too."""
    table = _sdd_section("### 21.1 Support window and tiers")
    assert f"releases.openstack.org shows them on {READ_ON}:" in table
    rows = {}
    for line in table.splitlines():
        cells = [cell.strip() for cell in line.split("|")[1:-1]]
        if not cells or not re.match(r"20\d\d\.[12] ", cells[0]):
            continue
        name, codename = cells[0].split()
        phase = re.match(r"Maintained|Unmaintained|End of Life", cells[1]).group(0)
        tier = "tier" + re.search(r"Tier (\d)", cells[3]).group(1)
        rows[name] = (codename, phase, cells[2] == "yes", tier)
    assert rows == {s.name: (s.codename, s.phase, s.slurp, s.tier) for s in SERIES}

    detection = " ".join(_sdd_section("### 21.3 Release detection").split())
    compute = {}
    for maximum, first, second in re.findall(
        r"(2\.\d+) is (20\d\d\.[12])(?: or (20\d\d\.[12]))?", detection
    ):
        compute.update({name: maximum for name in (first, second) if name})
    assert compute == {s.name: s.compute_max for s in SERIES}
    block = {name: None for name in WINDOW}
    for maximum, first, second in re.findall(
        r"(3\.\d+) in (20\d\d\.[12]) and (20\d\d\.[12])", detection
    ):
        block.update({first: maximum, second: maximum})
    assert block == {s.name: s.block_storage_max for s in SERIES}
    assert f"`REVIEWED_MICROVERSIONS = {json.dumps(REVIEWED_MICROVERSIONS)}`" in _sdd_section(
        "### 10.1 Destination release series"
    )


@pytest.mark.parametrize("path", sorted(VERSIONS.glob("*.json")), ids=lambda p: p.stem)
def test_recorded_version_documents_map_to_their_series(path):
    """Each series' compute and block-storage version documents (synthetic until the lab records
    them, QASuite LAB-D) read as the range SDD §21.3 gives that series' maxima."""
    documents = json.loads(path.read_text(encoding="utf-8"))
    compute = maximum_microversion(documents["compute"])
    block_storage = maximum_microversion(documents["block_storage"])
    found = release_range(compute, block_storage)
    assert found == ReleaseRange(tuple(documents["expected_range"]))
    assert documents["series"] in found.series
    # the single-version document a versioned endpoint answers reads the same
    current = next(v for v in documents["compute"]["versions"] if v["status"] == "CURRENT")
    assert maximum_microversion({"version": current}) == compute


def test_every_series_of_the_window_has_its_recorded_version_documents():
    assert sorted(path.stem for path in VERSIONS.glob("*.json")) == list(WINDOW)


@pytest.mark.parametrize(
    "document, maximum",
    [
        (
            {
                "versions": [
                    {"status": "SUPPORTED", "version": "2.90"},
                    {"status": "CURRENT", "version": "2.60"},
                ]
            },
            "2.60",
        ),
        (
            {
                "versions": [
                    {"status": "SUPPORTED", "version": "2.9"},
                    {"status": "SUPPORTED", "version": "2.10"},
                ]
            },
            "2.10",
        ),
        ({"versions": [{"status": "SUPPORTED", "version": ""}]}, None),
        ({"version": {"status": "CURRENT", "version": "3.71"}}, "3.71"),
        ({}, None),
        (None, None),
        ("2.95", None),
    ],
    ids=[
        "the CURRENT entry",
        "else the highest",
        "none readable",
        "a versioned endpoint",
        "empty",
        "null",
        "not a document",
    ],
)
def test_a_version_documents_maximum_is_its_current_entry_else_its_highest(document, maximum):
    assert maximum_microversion(document) == maximum


#: the openstacksdk resources Seamless's calls use, with the service whose microversion they send
SDK_RESOURCES = (
    ("compute", "openstack.compute.v2.server", "Server"),
    ("compute", "openstack.compute.v2.flavor", "Flavor"),
    ("compute", "openstack.compute.v2.volume_attachment", "VolumeAttachment"),
    ("compute", "openstack.compute.v2.server_interface", "ServerInterface"),
    ("compute", "openstack.compute.v2.keypair", "Keypair"),
    ("compute", "openstack.compute.v2.quota_set", "QuotaSet"),
    ("block_storage", "openstack.block_storage.v3.volume", "Volume"),
    ("block_storage", "openstack.block_storage.v3.snapshot", "Snapshot"),
    ("block_storage", "openstack.block_storage.v3.attachment", "Attachment"),
    ("block_storage", "openstack.block_storage.v3.type", "Type"),
)


@pytest.mark.parametrize("series", SERIES, ids=lambda s: s.name)
def test_the_sdk_never_asks_more_than_its_caps_of_any_series_of_the_window(series):
    """SDD §21.7: openstacksdk asks a cloud for the lower of a resource's cap and the cloud's
    maximum - its own negotiation, run here against each series' maxima. Compute 2.101 (a volume
    attach answers 202), 2.102 (flavors lose two fields) and 2.103 (/os-volumes_boot is gone) must
    never reach Seamless's calls: a raised openstacksdk pin whose caps pass REVIEWED_MICROVERSIONS
    fails here until those versions are reviewed."""
    pytest.importorskip("openstack")
    import importlib

    from openstack import utils

    asked = {}
    for service, module, name in SDK_RESOURCES:
        resource = getattr(importlib.import_module(module), name)
        cap = getattr(resource, "_max_microversion", None)
        if cap is None:  # the SDK sends no microversion for it
            continue
        # a maximum §21.3 has not confirmed (Cinder's from 2025.1): the cloud takes what is asked
        maximum = series.compute_max if service == "compute" else series.block_storage_max or cap
        endpoint = SimpleNamespace(
            min_microversion="2.1" if service == "compute" else "3.0", max_microversion=maximum
        )
        adapter = SimpleNamespace(get_endpoint_data=lambda endpoint=endpoint: endpoint)
        version = utils.maximum_supported_microversion(adapter, cap)
        asked[name] = version
        assert version is not None, (name, cap, maximum)
        assert _version(version) <= _version(REVIEWED_MICROVERSIONS[service]), (name, cap, version)
        assert _version(version) <= _version(maximum), (name, cap, version)
    assert asked.get("Server"), asked
