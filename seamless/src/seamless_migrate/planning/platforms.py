"""Destination release series (SDD §10.1, §21.1–§21.3): the community OpenStack series a destination
cloud may run, read from its services' maximum microversions, and the support tier that follows.

Pure: no I/O. The provider check reads the version documents (§21.3, T3); the pre-flight warns by
the tier (T4). Behaviour follows capabilities, never the series name (§21.4): the tier decides only
what a Seamless release validated and what the pre-flight warns.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

#: the date the upstream phases below were read on releases.openstack.org (SDD §21.1)
READ_ON = "2026-10-16"


@dataclass(frozen=True)
class Series:
    """One community series of the window: its upstream phase and SLURP status decide its tier
    (§21.2); its services' maximum microversions identify it (§21.3)."""

    name: str
    codename: str
    phase: str  # "Maintained" | "Unmaintained" | "End of Life", as releases.openstack.org shows it
    slurp: bool
    tier: str  # "tier1" | "tier2" | "tier3"
    compute_max: str
    block_storage_max: str | None  # null where §21.3 has not confirmed it


#: the window's community series, oldest first (SDD §21.1, §21.3); §21.1's table changes with it
SERIES: tuple[Series, ...] = (
    Series("2023.1", "Antelope", "Unmaintained", True, "tier2", "2.95", "3.70"),
    Series("2023.2", "Bobcat", "End of Life", False, "tier3", "2.95", "3.70"),
    Series("2024.1", "Caracal", "Unmaintained", True, "tier2", "2.96", "3.71"),
    Series("2024.2", "Dalmatian", "End of Life", False, "tier3", "2.96", "3.71"),
    Series("2025.1", "Epoxy", "Maintained", True, "tier1", "2.100", None),
    Series("2025.2", "Flamingo", "Maintained", False, "tier1", "2.100", None),
    Series("2026.1", "Gazpacho", "Maintained", True, "tier1", "2.103", None),
    Series("2026.2", "Hibiscus", "Maintained", False, "tier1", "2.104", None),
)

#: the newest microversions whose behaviour Seamless's calls were checked against (SDD §21.7)
REVIEWED_MICROVERSIONS = {"compute": "2.100", "block_storage": "3.71"}

#: the tiers of the window, the most favourable first
TIERS = ("tier1", "tier2", "tier3")

#: the oldest series whose oslo.policy enforces the new defaults (SDD §21.5)
SECURE_RBAC_FROM = "2023.2"

_REASONS = {"older": "below_floor", "newer": "newer_than_validated"}


@dataclass(frozen=True)
class ReleaseRange:
    """The series a cloud may run, oldest first; with none, why: ``older`` (below the window),
    ``newer`` (above it) or ``not_detected``."""

    series: tuple[str, ...]
    outside: str | None = None


@dataclass(frozen=True)
class SupportTier:
    """A destination's tier, why it is ``tier3`` or ``outside``, and whether a declared series
    fell outside the detected range (the pre-flight warns of it)."""

    tier: str
    reason: str | None = None
    declared_outside_range: bool = False


def parse_microversion(value: Any) -> tuple[int, int] | None:
    """A microversion such as ``"2.100"`` as numbers - so 2.9 is below 2.95 - or None."""
    match = re.fullmatch(r"\s*(\d+)\.(\d+)\s*", value) if isinstance(value, str) else None
    return (int(match.group(1)), int(match.group(2))) if match else None


def maximum_microversion(document: Any) -> str | None:
    """The maximum microversion a service's version document gives: its ``version`` object, or in
    the root document the ``versions`` entry whose status is CURRENT, else the highest one; None
    when it gives none (SDD §10.1)."""
    if not isinstance(document, dict):
        return None
    if isinstance(document.get("version"), dict):
        entries = [document["version"]]
    elif isinstance(document.get("versions"), list):
        entries = [entry for entry in document["versions"] if isinstance(entry, dict)]
    else:
        return None
    readable = [
        (version, entry)
        for entry in entries
        if (version := parse_microversion(entry.get("version"))) is not None
    ]
    current = [item for item in readable if str(item[1].get("status", "")).upper() == "CURRENT"]
    if not readable:
        return None
    major, minor = max(current or readable, key=lambda item: item[0])[0]
    return f"{major}.{minor}"


def release_range(compute_max: Any, block_storage_max: Any = None) -> ReleaseRange:
    """The series a cloud with these maximum microversions may run (SDD §10.1, §21.3).

    A compute maximum equal to series' maxima is those series; one between two series' maxima the
    two series it falls between (Nova never backports a microversion, so only a development
    snapshot shows one); one outside the window's maxima, or none readable, is no series. A
    block-storage maximum keeps the series it fits (``_cinder_fits``): it narrows the range, never
    widens it, and a narrowing to nothing is not applied."""
    compute = parse_microversion(compute_max)
    if compute is None:
        return ReleaseRange((), "not_detected")
    maxima = [(parse_microversion(series.compute_max), series) for series in SERIES]
    if compute < maxima[0][0]:
        return ReleaseRange((), "older")
    if compute > maxima[-1][0]:
        return ReleaseRange((), "newer")
    found = [series for maximum, series in maxima if maximum == compute]
    if not found:
        below = [series for maximum, series in maxima if maximum < compute][-1]
        above = next(series for maximum, series in maxima if maximum > compute)
        found = [below, above]
    block_storage = parse_microversion(block_storage_max)
    if block_storage is not None:
        narrowed = [series for series in found if _cinder_fits(series, block_storage)]
        found = narrowed or found
    return ReleaseRange(tuple(series.name for series in found))


def _cinder_fits(series: Series, block_storage: tuple[int, int]) -> bool:
    """Whether a cloud whose Cinder maximum is ``block_storage`` may run ``series`` (SDD §10.1):
    the series' confirmed maximum equals it; an unconfirmed one is at least the greatest confirmed
    maximum of the older series, as Cinder's microversions only grow (review RV18-05)."""
    if series.block_storage_max is not None:
        return parse_microversion(series.block_storage_max) == block_storage
    older = [
        parse_microversion(older.block_storage_max)
        for older in SERIES[: SERIES.index(series)]
        if older.block_storage_max is not None
    ]
    floor = max((maximum for maximum in older if maximum is not None), default=None)
    return floor is None or block_storage >= floor


def _series_number(name: str) -> tuple[int, int]:
    year, half = name.split(".")
    return int(year), int(half)


def _declared_decides(found: ReleaseRange, declared: str) -> bool:
    """Whether a declared series decides the tier and is the settled one (SDD §10.1): the range
    holds it, or the range is empty and does not contradict it - any series when nothing was
    detected, one older than the window when the range is ``older``, one newer than it when
    ``newer``: where the APIs cannot tell the series, the declaration does (review RV18-06)."""
    if declared in found.series:
        return True
    if found.series:
        return False
    if found.outside == "older":
        return _series_number(declared) < _series_number(SERIES[0].name)
    if found.outside == "newer":
        return _series_number(declared) > _series_number(SERIES[-1].name)
    return True


def _profile_by_series(settled: str | None, found: ReleaseRange) -> str:
    """The policy profile a community cloud's series implies: the settled series, else every
    series of the range, must agree (SDD §10.1, §21.5); ``unknown`` when they do not."""
    series = (settled,) if settled is not None else found.series
    if not series:
        return {"newer": "secure_rbac", "older": "legacy"}.get(found.outside or "", "unknown")
    secure = {_series_number(name) >= _series_number(SECURE_RBAC_FROM) for name in series}
    if secure == {True}:
        return "secure_rbac"
    return "legacy" if secure == {False} else "unknown"


def destination_release(
    kind: str,
    release: str | None,
    policy_profile: str | None,
    compute_max: Any,
    block_storage_max: Any,
) -> dict[str, Any]:
    """What a destination's check reports about its series (SDD §10, §10.1): the detected range,
    the series it settles on, its support tier and its policy profile. ``release`` and
    ``policy_profile`` are what its provider declares (§4.2)."""
    found = release_range(compute_max, block_storage_max)
    if release is not None and _declared_decides(found, release):
        settled: str | None = release
    else:
        settled = found.series[0] if len(found.series) == 1 else None
    if kind == "rhoso":
        # validated by its kind; legacy until the lab decides D14
        tier, default_profile = SupportTier("validated"), "legacy"
    else:
        tier, default_profile = support_tier(found, release), _profile_by_series(settled, found)
    return {
        "release_range": list(found.series),
        "release": settled,
        "support_tier": {
            "tier": tier.tier,
            "reason": tier.reason,
            "declared_outside_range": tier.declared_outside_range,
        },
        "policy_profile": policy_profile or default_profile,
    }


def support_tier(found: ReleaseRange, declared: str | None = None) -> SupportTier:
    """The tier of a cloud whose detected range is ``found`` (SDD §10.1, §21.3).

    A declared series decides when the range holds it or, empty, does not contradict it
    (``_declared_decides``): its own tier, or ``outside`` with the reason its place gives when it
    is not a series of the window. Otherwise the range does - the least favourable tier of its
    series, or ``outside`` when it has none - and a declared series, which the range then
    contradicts, sets ``declared_outside_range``. RHOSO 18.0 is ``validated`` by its kind, never by
    this."""
    tiers = {series.name: series.tier for series in SERIES}
    if declared is not None and _declared_decides(found, declared):
        if declared not in tiers:
            older = _series_number(declared) < _series_number(SERIES[0].name)
            return SupportTier("outside", "below_floor" if older else "newer_than_validated")
        tier = tiers[declared]
        return SupportTier(tier, "end_of_life" if tier == "tier3" else None)
    outside_range = declared is not None
    if not found.series:
        reason = _REASONS.get(found.outside or "", "not_detected")
        return SupportTier("outside", reason, outside_range)
    tier = max((tiers[name] for name in found.series), key=TIERS.index)
    return SupportTier(tier, "end_of_life" if tier == "tier3" else None, outside_range)
