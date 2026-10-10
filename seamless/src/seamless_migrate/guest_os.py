"""Guest OS catalog (SDD §9.5): identification, lifecycle and virt-v2v support of a guest.

``identify(os_type)`` accepts what the providers report — OpenStack metadata and image properties
(``ubuntu 22.04``, ``rhel9``, ``windows``), libosinfo short ids (``win2k19``), VMware guest ids
(``rhel9_64Guest``, ``windows2019srvNext_64Guest``) and VMware Tools names (``Ubuntu 22.04.4 LTS``).
Lifecycle reflects vendor support as of :data:`CATALOG_DATE`; ``v2v`` follows Red Hat's virt-v2v
support matrix for RHEL 9 conversion hosts (VMware sources only — OpenStack guests are not
converted).
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict

CATALOG_DATE = "2026-10"

Family = Literal["linux", "windows", "unknown"]
Lifecycle = Literal["current", "legacy", "unknown"]
V2V = Literal["supported", "tech_preview", "unverified", "unsupported", "unknown"]


class GuestOS(BaseModel):
    model_config = ConfigDict(frozen=True)

    family: Family
    distro: str | None = None
    version: str | None = None
    label: str
    lifecycle: Lifecycle = "unknown"
    v2v: V2V = "unknown"


_UBUNTU_CODENAMES = {
    "trusty": "14.04",
    "xenial": "16.04",
    "bionic": "18.04",
    "focal": "20.04",
    "jammy": "22.04",
    "noble": "24.04",
    "plucky": "25.04",
    "questing": "25.10",
}
_DEBIAN_CODENAMES = {
    "wheezy": "7",
    "jessie": "8",
    "stretch": "9",
    "buster": "10",
    "bullseye": "11",
    "bookworm": "12",
    "trixie": "13",
    "forky": "14",
}
# order matters: the more specific names first
_LINUX: tuple[tuple[str, str], ...] = (
    ("centos-stream", r"centos[\s_-]*stream"),
    ("rocky", r"rocky"),
    ("almalinux", r"alma"),
    ("oracle", r"oracle|\boel\b"),
    ("centos", r"centos"),
    ("rhel", r"rhel|red\s*hat"),
    ("ubuntu", r"ubuntu"),
    ("debian", r"debian"),
    ("sles", r"sles|suse\s+linux\s+enterprise"),
    ("opensuse", r"opensuse"),
    ("fedora", r"fedora"),
)
_LABELS = {
    "rhel": "RHEL",
    "centos": "CentOS",
    "centos-stream": "CentOS Stream",
    "rocky": "Rocky Linux",
    "almalinux": "AlmaLinux",
    "oracle": "Oracle Linux",
    "ubuntu": "Ubuntu",
    "debian": "Debian",
    "sles": "SLES",
    "opensuse": "openSUSE",
    "fedora": "Fedora",
    "windows-server": "Windows Server",
    "windows": "Windows",
}
# VMware guest ids (underscores and case removed) whose version is not in the id itself
_VMWARE_WINDOWS = {
    "windows2022srvnext64guest": ("windows-server", "2025"),
    "windows2019srvnext64guest": ("windows-server", "2022"),
    "windows2019srv64guest": ("windows-server", "2019"),
    "windows9server64guest": ("windows-server", "2016"),
    "windows8server64guest": ("windows-server", "2012"),
    "windows7server64guest": ("windows-server", "2008 R2"),
    "windows1164guest": ("windows", "11"),
    "windows964guest": ("windows", "10"),
    "windows9guest": ("windows", "10"),
    "windows864guest": ("windows", "8"),
    "windows764guest": ("windows", "7"),
    "windows7guest": ("windows", "7"),
}
_SERVER_YEAR = re.compile(r"(2003|2008|2012|2016|2019|2022|2025)")
_LIBOSINFO = re.compile(r"^win2k(\d{1,2})(r2)?$")
_CLIENT = re.compile(r"windows[\s_-]*(11|10|8\.1|8|7)\b")
_NUMBER = re.compile(r"(\d+(?:\.\d+)?)")


def _major(version: str | None) -> int | None:
    if not version:
        return None
    match = re.match(r"\d+", version)
    return int(match.group()) if match else None


def _windows(low: str, compact: str) -> tuple[str | None, str | None]:
    if compact in _VMWARE_WINDOWS:
        return _VMWARE_WINDOWS[compact]
    if compact.startswith("winlonghorn"):
        return "windows-server", "2008"
    if compact.startswith("winnet"):
        return "windows-server", "2003"
    lib = _LIBOSINFO.match(compact)
    if lib:
        short = int(lib.group(1))
        year = 2000 + short
        return "windows-server", f"{year}{' R2' if lib.group(2) else ''}"
    year = _SERVER_YEAR.search(low)
    if year:
        r2 = re.search(r"r2", low[year.end() :]) is not None
        return "windows-server", f"{year.group()}{' R2' if r2 else ''}"
    client = _CLIENT.search(low)
    if client:
        return "windows", client.group(1)
    return None, None


def _linux(low: str) -> tuple[str | None, str | None]:
    text = re.sub(r"_?64guest$|guest$", "", low)
    for distro, pattern in _LINUX:
        match = re.search(pattern, text)
        if not match:
            continue
        number = _NUMBER.search(text[match.end() :])
        version = number.group(1) if number else None
        if distro == "debian" and version:
            version = version.split(".")[0]
        if version is None:
            names = _UBUNTU_CODENAMES if distro == "ubuntu" else _DEBIAN_CODENAMES
            version = next((v for k, v in names.items() if k in text), None)
        if distro == "ubuntu" and version and "." in version:
            major, minor = version.split(".")[:2]
            version = f"{major}.{minor}"
        return distro, version
    for names, distro in ((_UBUNTU_CODENAMES, "ubuntu"), (_DEBIAN_CODENAMES, "debian")):
        for name, version in names.items():
            if re.search(rf"\b{name}\b", text):
                return distro, version
    return None, None


def _lifecycle(distro: str | None, version: str | None) -> Lifecycle:
    major = _major(version)
    if distro in {"rhel", "oracle"}:
        return "unknown" if major is None else ("legacy" if major <= 7 else "current")
    if distro == "centos":
        return "legacy"  # every CentOS Linux release is end of life
    if distro == "centos-stream":
        return "unknown" if major is None else ("legacy" if major <= 8 else "current")
    if distro in {"rocky", "almalinux"}:
        return "unknown" if major is None else "current"
    if distro == "ubuntu":
        if not version or "." not in version or major is None:
            return "unknown"
        minor = int(version.split(".")[1])
        lts = minor == 4 and major % 2 == 0  # LTS: April releases of even years
        if (major, minor) >= (26, 4) or (lts and major >= 22):
            return "current"
        return "legacy"
    if distro == "debian":
        return "unknown" if major is None else ("legacy" if major <= 11 else "current")
    if distro == "sles":
        return "unknown" if major is None else ("legacy" if major <= 12 else "current")
    if distro == "windows-server":
        return "unknown" if major is None else ("legacy" if major <= 2012 else "current")
    if distro == "windows":
        return "unknown" if major is None else ("current" if major >= 11 else "legacy")
    return "unknown"


def _v2v(distro: str | None, version: str | None) -> V2V:
    major = _major(version)
    if distro == "rhel":
        if major is None:
            return "unverified"
        return "supported" if major >= 7 else ("unverified" if major == 6 else "unsupported")
    if distro == "centos":
        return "unsupported" if major is not None and major <= 5 else "unverified"
    if distro in {"centos-stream", "rocky", "almalinux", "oracle", "sles", "opensuse", "fedora"}:
        return "unverified"
    if distro in {"ubuntu", "debian"}:
        return "tech_preview"
    if distro == "windows-server":
        return "unknown" if major is None else ("supported" if major >= 2016 else "unsupported")
    if distro == "windows":
        return "unknown" if major is None else ("supported" if major >= 10 else "unsupported")
    return "unknown"


def identify(os_type: str | None) -> GuestOS:
    """The guest OS described by a provider's ``os_type`` string (SDD §9.5)."""
    raw = (os_type or "").strip()
    if not raw:
        return GuestOS(family="unknown", label="Unknown OS")
    low = raw.lower()
    compact = re.sub(r"[^a-z0-9]", "", low)
    if low.startswith("win") or "windows" in low or "microsoft" in low:
        distro, version = _windows(low, compact)
        family: Family = "windows"
    else:
        distro, version = _linux(low)
        family = "linux" if distro or "linux" in low else "unknown"
    if family == "unknown":
        return GuestOS(family="unknown", label=raw)
    if distro is None:
        return GuestOS(family=family, label="Windows" if family == "windows" else "Linux")
    label = _LABELS[distro] + (f" {version}" if version else "")
    return GuestOS(
        family=family,
        distro=distro,
        version=version,
        label=label,
        lifecycle=_lifecycle(distro, version),
        v2v=_v2v(distro, version),
    )
