"""Pydantic v2 domain models (SDD §4.2).

JSON field names are exactly the attribute names. Timestamps are timezone-aware UTC and
serialize as ISO-8601 strings ending in ``Z``; naive inputs are interpreted as UTC.
"""

from __future__ import annotations

import bisect
import ipaddress
import math
import re
import secrets
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationInfo,
    computed_field,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError

from ..guest_os import GuestOS, identify
from .enums import (
    DecisionStatus,
    DecisionType,
    Phase,
    PlanStatus,
    ProviderKind,
    ProviderRole,
    ReasonCode,
    Severity,
    Strategy,
    SyncPassKind,
)
from .text import replace_unstorable

GIB = 2**30
#: Fraction of the provisioned size assumed used when a disk does not report usage (SDD §4.2).
USED_FALLBACK_RATIO = 0.6


def utcnow() -> datetime:
    return datetime.now(UTC)


def to_utc(value: datetime) -> datetime:
    """``value`` in UTC, one without an offset read as UTC; ValueError past datetime's range."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    try:
        return value.astimezone(UTC)
    except OverflowError:
        # its offset carries it past datetime's range: a 422 naming the field, never a 500
        # (SDD §4, audit AA-01)
        raise ValueError("a time outside 0001-01-01..9999-12-31 UTC") from None


UTCDateTime = Annotated[datetime, AfterValidator(to_utc)]


def new_plan_id() -> str:
    return f"plan-{secrets.token_hex(4)}"


def new_migration_id() -> str:
    return f"mig-{secrets.token_hex(5)}"


def new_recommendation_id() -> str:
    return f"rec-{secrets.token_hex(6)}"


def new_policy_id() -> str:
    return f"pol-{secrets.token_hex(6)}"


#: the validation context of a request body (SDD §12): its unknown fields are refused at every
#: depth, where a stored document keeps loading with the fields a later version added (§11)
REQUEST_BODY: dict[str, bool] = {"request_body": True}
#: the validation context of a document the store reads (SDD §11): a NIC's network an earlier
#: version stored with a NUL or a lone surrogate reads with U+FFFD there, and only there (§4.2)
STORED_DOCUMENT: dict[str, bool] = {"stored_document": True}


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore")

    @model_validator(mode="before")
    @classmethod
    def _no_unknown_field_in_a_request(cls, data: Any, info: ValidationInfo) -> Any:
        """A request body's field the object does not take is refused at every depth (SDD §12,
        audit AP-01); a stored document, read without the request context, keeps loading with a
        later version's fields (§11)."""
        if isinstance(data, dict) and (info.context or {}).get("request_body"):
            unknown = sorted(str(key) for key in data if key not in cls.model_fields)
            if unknown:
                raise PydanticCustomError(
                    "unknown_field",
                    "not a field of this object: {fields}",
                    {"fields": ", ".join(unknown)},
                )
        return data


class ConversionHostConfig(_Model):
    manage: bool = True
    name: str | None = None
    flavor: str | None = None
    external_network: str | None = None
    image: str | None = None
    ssh_user: str = "cloud-user"
    address: str | None = None
    #: CIDR allowed to SSH to the conversion host (Security.md SEC-03)
    ssh_allowed_cidr: str | None = None
    #: secret holding the private key for an existing conversion host (required for VMware)
    ssh_key_secret: str | None = None


ProviderStatus = Literal["unknown", "ok", "degraded", "error"]


#: Presets and display only (SDD §4.2); ``kind`` decides the code path.
Distribution = Literal["openstack_community", "kolla", "rhosp", "rhoso", "vmware"]
DISTRIBUTION_KIND: dict[str, ProviderKind] = {
    "openstack_community": ProviderKind.openstack,
    "kolla": ProviderKind.openstack,
    "rhosp": ProviderKind.openstack,
    "rhoso": ProviderKind.rhoso,
    "vmware": ProviderKind.vmware,
}


#: a GPU flavor's name, and the NVIDIA vGPU profile its provider declares it gives (SDD §4.2)
FlavorName = Annotated[str, StringConstraints(min_length=1, max_length=255)]
ProfileName = Annotated[str, StringConstraints(min_length=1, max_length=64)]


class Provider(_Model):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,62}$")
    name: str
    kind: ProviderKind
    role: ProviderRole
    endpoint: str
    cloud: str | None = None
    credentials_secret: str | None = None
    region: str | None = None
    verify_tls: bool = True
    ca_cert_path: str | None = None
    conversion_host: ConversionHostConfig | None = None
    capabilities: dict[str, Any] = Field(default_factory=dict)
    status: ProviderStatus = "unknown"
    status_message: str | None = None
    last_checked_at: UTCDateTime | None = None
    distribution: Distribution | None = None
    #: the community series its operator declares, e.g. "2026.2" (SDD §4.2, §10.1, §21.3)
    release: str | None = Field(default=None, pattern=r"^20[0-9]{2}\.[12]$")
    #: the policy defaults its operator declares; None = by kind and series (SDD §4.2, §21.5)
    policy_profile: Literal["secure_rbac", "legacy"] | None = None
    #: the NVIDIA vGPU profile each of its GPU flavors gives, as its operator declares it: no API
    #: tells a flavor's framebuffer (SDD §4.2, §22.3)
    gpu_profiles: dict[FlavorName, ProfileName] = Field(default_factory=dict)
    #: its operator declares that the cloud's OVN BGP agent announces tenant networks
    #: (expose_tenant_networks), which no API shows: NET_PREFIX_SPLIT (SDD §4.2, §9.3, §23.6)
    announces_tenant_networks: bool = False
    #: server-owned: when PUT …/credentials / …/conversion-key last wrote the secret store
    credentials_updated_at: UTCDateTime | None = None
    conversion_key_updated_at: UTCDateTime | None = None

    @model_validator(mode="after")
    def _distribution_matches_kind(self) -> Provider:
        if self.distribution is not None and DISTRIBUTION_KIND[self.distribution] != self.kind:
            raise ValueError(
                f"distribution {self.distribution} belongs to kind "
                f"{DISTRIBUTION_KIND[self.distribution]}, not {self.kind}"
            )
        return self

    @model_validator(mode="after")
    def _openstack_declarations(self) -> Provider:
        """A series, a policy profile, vGPU profiles and announced tenant networks are OpenStack's:
        a vmware provider declares none - vCenter reports each VM's own vGPU profile (SDD §4.2,
        §22.3, §23.6)."""
        if self.kind == ProviderKind.vmware:
            for name in ("release", "policy_profile"):
                if getattr(self, name) is not None:
                    raise ValueError(f"{name}: an OpenStack {name} cannot be declared for vmware")
            if self.gpu_profiles:
                raise ValueError(
                    "gpu_profiles: vGPU profiles cannot be declared for vmware: vCenter reports "
                    "each VM's own"
                )
            if self.announces_tenant_networks:
                raise ValueError(
                    "announces_tenant_networks: an OpenStack BGP declaration cannot be made for "
                    "vmware"
                )
        return self

    @property
    def label(self) -> str:
        """The name a text gives the provider (SDD §1 naming): its name, else its id - a name stored
        blank before the API refused one (review RV20-11) never reads "in : ..."."""
        return self.id if is_blank(self.name) else self.name


class ProviderCreate(Provider):
    """The ``POST /providers`` body (SDD §12): a field ``Provider`` does not have is refused
    (422), never dropped; stored providers keep loading with later fields (``_Model``, §11)."""

    model_config = ConfigDict(extra="forbid")

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, name: str) -> str:
        """A blank name is refused here, at the request boundary: a provider stored blank before
        keeps loading (SDD §4.2, review RV20-11)."""
        if is_blank(name):
            raise PydanticCustomError("blank", BLANK_NAME)
        return name


#: why POST and PATCH /providers refuse a name (SDD §4.2, review RV20-11)
BLANK_NAME = "must not be blank"

#: the code points a blank name holds only - Unicode 15.1's separators (Z), controls (Cc) and
#: format characters (Cf) - pinned, as the dashboard's BLANK_RANGES, so every runtime answers
#: alike whatever its own Unicode database (SDD §4.2, review RV22-06); tests/fixtures/
#: blank_ranges.json holds both to the same list
BLANK_RANGES: tuple[tuple[int, int], ...] = (
    (0x0000, 0x0020),
    (0x007F, 0x00A0),
    (0x00AD, 0x00AD),
    (0x0600, 0x0605),
    (0x061C, 0x061C),
    (0x06DD, 0x06DD),
    (0x070F, 0x070F),
    (0x0890, 0x0891),
    (0x08E2, 0x08E2),
    (0x1680, 0x1680),
    (0x180E, 0x180E),
    (0x2000, 0x200F),
    (0x2028, 0x202F),
    (0x205F, 0x2064),
    (0x2066, 0x206F),
    (0x3000, 0x3000),
    (0xFEFF, 0xFEFF),
    (0xFFF9, 0xFFFB),
    (0x110BD, 0x110BD),
    (0x110CD, 0x110CD),
    (0x13430, 0x1343F),
    (0x1BCA0, 0x1BCA3),
    (0x1D173, 0x1D17A),
    (0xE0001, 0xE0001),
    (0xE0020, 0xE007F),
)
_BLANK_STARTS = tuple(first for first, _ in BLANK_RANGES)


def is_blank(name: str) -> bool:
    """No character outside BLANK_RANGES - empty, spaces, U+200B, U+FEFF -: the rule the dashboard
    shares, read from the pinned list alone (SDD §4.2, reviews RV21-08, RV22-06)."""
    for ch in name:
        cp = ord(ch)
        at = bisect.bisect_right(_BLANK_STARTS, cp) - 1
        if at < 0 or cp > BLANK_RANGES[at][1]:
            return False
    return True


DiskKind = Literal["volume", "ephemeral", "image_root", "vmdk"]


class Disk(_Model):
    id: str
    name: str | None = None
    size_gb: int
    used_gb: float | None = None
    bootable: bool = False
    volume_type: str | None = None
    device: str | None = None
    kind: DiskKind = "volume"
    multiattach: bool = False
    encrypted: bool = False
    independent: bool = False
    # Cinder "host@backend#pool" of a volume (admin only); decides the handover reference (§7.3.1)
    pool: str | None = None


class Nic(_Model):
    network: str
    mac: str | None = None
    fixed_ips: list[str] = Field(default_factory=list)
    vnic_type: str = "normal"
    mtu: int | None = None
    #: OpenStack: the addresses of the port's allowed address pairs (an address or a CIDR, as
    #: Neutron writes it), which the import does not carry (SDD §4.2, owner decision 11)
    allowed_address_pairs: list[str] = Field(default_factory=list)
    #: OpenStack: the addresses of the floating IPs associated with the port (SDD §4.2, §9.3)
    floating_ips: list[str] = Field(default_factory=list)
    #: OpenStack: the port's DNS name and its domain - the port's, else its network's - under
    #: Neutron's DNS integration; None when it has none (SDD §4.2, §9.3)
    dns_name: str | None = None
    dns_domain: str | None = None
    #: VMware: what the migration kit maps the NIC by - its distributed port group's key, else its
    #: device summary (a standard port group's name); None for OpenStack (SDD §4, §7.2)
    network_key: str | None = None

    @field_validator("network", mode="before")
    @classmethod
    def _stored_network(cls, value: Any, info: ValidationInfo) -> Any:
        """A network v0.1.0-rc.2 stored with a NUL or a lone surrogate, read with U+FFFD as the
        store reads it, so its migration is written again; from the source the name stays as
        given, and the store's write refuses it (SDD §4.2, §11, review RV52-S1)."""
        if isinstance(value, str) and (info.context or {}).get("stored_document"):
            return _storable(value)
        return value


PowerState = Literal["running", "stopped", "paused", "error", "transitioning", "unknown"]


#: a PCI vendor or device id: four lower-case hex digits (SDD §4.2, §22.2)
PCI_ID = r"^[0-9a-f]{4}$"


class GpuDevice(_Model):
    """A GPU of a source VM, or a GPU request of a flavor (SDD §4.2, §22.2). Its profile, alias
    and traits are free text, written U+FFFD where they hold what the store cannot keep (§11)."""

    kind: Literal["vgpu", "passthrough"]
    #: the vGPU profile: VMware's as vCenter reports it, an OpenStack request's as its flavor's
    #: provider declares it (Provider.gpu_profiles)
    profile: str | None = None
    #: VMware passthrough: the device's PCI vendor and device ids
    vendor_id: str | None = Field(default=None, pattern=PCI_ID)
    device_id: str | None = Field(default=None, pattern=PCI_ID)
    #: identical devices of one VM are one entry
    count: int = Field(default=1, ge=1)
    #: OpenStack: the request group's resource class with its required and forbidden traits, and
    #: a passthrough request's PCI alias
    resource_class: str | None = None
    traits: list[str] = Field(default_factory=list)
    forbidden_traits: list[str] = Field(default_factory=list)
    #: OpenStack vGPU: a numbered request group's (resources<suffix>:), whose traits Placement
    #: checks on the GPU's own provider; the unnumbered group's it checks across the GPU's provider
    #: and the compute node's (SDD §22.3, PR #62 review)
    same_provider: bool = False
    alias: str | None = None

    @field_validator("profile", "alias", mode="before")
    @classmethod
    def _storable_text(cls, value: Any) -> Any:
        return replace_unstorable(value) if isinstance(value, str) else value

    @field_validator("traits", "forbidden_traits", mode="before")
    @classmethod
    def _storable_names(cls, value: Any) -> Any:
        if not isinstance(value, list):
            return value
        return [replace_unstorable(v) if isinstance(v, str) else v for v in value]


class VMRef(_Model):
    """A source VM as inventoried (SDD §4.2). The names a cutover reads - its own, its project's,
    its flavor's, its NICs' networks, its disks' volume types - are kept as the source gives them:
    one holding a NUL, which PostgreSQL cannot store, fails the store's write naming the field,
    never a name the source does not have. Its free text no export filter or mapping reads - tags
    and flavor extra specs, keys and values, os_type, host - is written U+FFFD as the model is
    built, so one tenant's metadata value never fails a plan's validation (SDD §11, RV49-02)."""

    source_id: str
    name: str
    project: str | None = None
    flavor: str | None = None
    vcpus: int
    ram_mb: int
    disks: list[Disk] = Field(default_factory=list)
    nics: list[Nic] = Field(default_factory=list)
    power_state: PowerState = "unknown"
    os_type: str | None = None
    host: str | None = None
    tags: dict[str, str] = Field(default_factory=dict)
    flavor_extra_specs: dict[str, str] = Field(default_factory=dict)
    cbt_enabled: bool | None = None
    snapshot_count: int = 0
    tools_ok: bool | None = None

    @field_validator("os_type", "host", mode="before")
    @classmethod
    def _storable_text(cls, value: Any) -> Any:
        return replace_unstorable(value) if isinstance(value, str) else value

    @field_validator("tags", "flavor_extra_specs", mode="before")
    @classmethod
    def _storable_map(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        return {
            (replace_unstorable(k) if isinstance(k, str) else k): (
                replace_unstorable(v) if isinstance(v, str) else v
            )
            for k, v in value.items()
        }

    change_rate_bps: float | None = None
    #: the task Nova runs on the server whatever its status (OS-EXT-STS:task_state), apart from
    #: power_state; None for none and for VMware (SDD §4.2, review PV-05)
    task_state: str | None = None
    #: the Nova keypair the server boots with (OpenStack); VMware: None (SDD §4.1)
    key_name: str | None = None
    #: its GPUs, as its source reports them; [] for a VM recorded before (SDD §4.2, §22.2)
    gpus: list[GpuDevice] = Field(default_factory=list)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def disk_bytes(self) -> int:
        return sum(d.size_gb for d in self.disks) * GIB

    @computed_field  # type: ignore[prop-decorator]
    @property
    def used_bytes(self) -> int:
        if all(d.used_gb is not None for d in self.disks):
            return int(sum(float(d.used_gb or 0.0) for d in self.disks) * GIB)
        return int(self.disk_bytes * USED_FALLBACK_RATIO)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def guest_os(self) -> GuestOS:
        """Family, version, lifecycle and conversion support of the guest (SDD §9.5)."""
        return identify(self.os_type)

    def root_disk(self) -> Disk | None:
        """The boot disk: first bootable disk, else the first disk."""
        for disk in self.disks:
            if disk.bootable:
                return disk
        return self.disks[0] if self.disks else None


class Mappings(_Model):
    networks: dict[str, str] = Field(default_factory=dict)
    flavors: dict[str, str] = Field(default_factory=dict)
    volume_types: dict[str, str] = Field(default_factory=dict)
    projects: dict[str, str] = Field(default_factory=dict)
    #: a VMware GPU key to a destination GPU flavor; never resolved automatically (SDD §4.2, §22.4)
    gpus: dict[str, str] = Field(default_factory=dict)


class HandoverConfig(_Model):
    enabled: bool = False
    #: source volume_type -> RHOSO cinder host "hostgroup@backend#pool"
    backend_map: dict[str, str] = Field(default_factory=dict)


class CutoverWindow(_Model):
    start: UTCDateTime
    end: UTCDateTime

    def contains(self, moment: datetime) -> bool:
        return self.start <= to_utc(moment) <= self.end


DEFAULT_CONSOLE_PATTERNS = ["login:", "Cloud-init v\\. .* finished", "Reached target .*Multi-User"]


ProbeKind = Literal["tcp", "http", "https"]


#: a NIC override's key: six hex pairs, lower case (SDD §4.2, §12)
_MAC = re.compile(r"[0-9a-f]{2}(?::[0-9a-f]{2}){5}")


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


class NicOverride(_Model):
    """A VM NIC's network and addresses at RHOSO (SDD §4.2): None keeps where the plan maps its
    network, or its own addresses."""

    network: str | None = None
    fixed_ips: list[str] | None = None

    @field_validator("fixed_ips")
    @classmethod
    def _canonical(cls, value: list[str] | None) -> list[str] | None:
        """Valid addresses in their canonical form ("fd00:0::0005" is "fd00::5"); the others stay
        as given, for invalid_plan_settings to name."""
        if value is None:
            return None
        return [str(ipaddress.ip_address(a)) if _is_ip(a) else a for a in value]


def _storable(text: str) -> str:
    """``text`` with each NUL and lone surrogate read as U+FFFD; ASCII text with no NUL - nearly
    every string - is looked at by C code alone (SDD §11, review RV52-S1, PR #58 review)."""
    if "\x00" not in text and text.isascii():
        return text
    return replace_unstorable(text)


def _as_written(value: Any) -> Any:
    """``value`` as this version writes what an earlier version stored on SQLite from an outside
    service - Jev, agentmemory: each NaN or infinity in it as None, each NUL and lone surrogate of
    its strings, keys and values, as U+FFFD - in one walk (SDD §11, reviews RV51-06, RV52-S1)."""
    if isinstance(value, str):  # first and inline: most of what is walked
        return value if "\x00" not in value and value.isascii() else replace_unstorable(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {_as_written(key): _as_written(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_as_written(item) for item in value]
    return value


def _storable_text_field(value: Any) -> Any:
    """A field's text, or the strings of its list or mapping, as ``_as_written`` reads them."""
    return _as_written(value) if isinstance(value, str | dict | list) else value


def _storable_keys(value: Any) -> Any:
    """A mapping's keys read as ``_as_written`` reads them, its values as given."""
    if not isinstance(value, dict):
        return value
    return {(_storable(k) if isinstance(k, str) else k): v for k, v in value.items()}


class AppProbe(_Model):
    """An application probe (SDD §7.5, ADV-18): a TCP connect, or an HTTP(S) GET of ``path``."""

    kind: ProbeKind
    port: int
    #: http(s) only
    path: str = "/"
    #: http(s) only: the status that passes; None = any 2xx or 3xx
    expect_status: int | None = None


class ProbeResult(_Model):
    """What one application probe found (SDD §7.5): unreachable is not failed (ADV-18)."""

    target: str
    kind: ProbeKind
    result: Literal["passed", "failed", "unreachable"]
    #: the time to the service's answer; None when it did not answer
    latency_ms: float | None = None
    at: UTCDateTime
    detail: str = ""

    _storable = field_validator("detail", mode="before")(_storable_text_field)


class VerificationRecord(_Model):
    """The last verification of a migration: its checks and probe results (SDD §7.5)."""

    at: UTCDateTime
    passed: bool
    checks: list[dict[str, Any]] = Field(default_factory=list)
    probes: list[ProbeResult] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    _storable = field_validator("checks", "warnings", mode="before")(_storable_text_field)


class VerificationConfig(_Model):
    tcp_ports: list[int] = Field(default_factory=list)
    # Windows guests probe these instead and skip the console check (SDD §7.5)
    windows_tcp_ports: list[int] = Field(default_factory=list)
    probe_address: Literal["fixed", "floating"] = "fixed"
    console_success_patterns: list[str] = Field(
        default_factory=lambda: list(DEFAULT_CONSOLE_PATTERNS)
    )
    timeout_s: int = 600
    auto_rollback: bool = True
    use_advisor: bool = True
    #: the application probes of every VM; Plan.probe_overrides replaces them per VM (ADV-18)
    app_probes: list[AppProbe] = Field(default_factory=list)


class Wave(_Model):
    id: str
    name: str
    order: int
    vm_ids: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    max_parallel: int = 5


DEFAULT_PRESTAGE = [
    "networks",
    "subnets",
    "routers",
    "router_interfaces",
    "security_groups",
    "security_group_rules",
]
#: What a plan may pre-stage (SDD §4.2): the collection's export_<r>/import_<r> playbook pairs,
#: never the workloads themselves; the defaults first, then the others in their creation order
PRESTAGE_RESOURCES = (
    *DEFAULT_PRESTAGE,
    "flavors",
    "keypairs",
    "images",
    "projects",
    "users",
    "users_keypairs",
    "user_project_role_assignments",
    "detached_volumes",
)


#: where a migration runs a plan's hooks (SDD §4.2, §23.3)
HookPoint = Literal["pre_stop", "post_create", "post_verify", "on_rollback", "on_finalize"]
#: a hook's name, the catalog's key - a provider id's form (SDD §4.2)
HOOK_NAME = r"^[a-z0-9][a-z0-9-]{1,62}$"
#: a DNS target's zone and record, an Octavia pool's id (SDD §4.2): 1-255 characters
_Text255 = Annotated[str, StringConstraints(min_length=1, max_length=255)]


def _secret_name(field: str, name: str | None) -> str | None:
    """A secret's name as SDD §13.3 reads it, else a ValueError naming ``field``."""
    from ..security.secrets import valid_secret_name  # the security package reads the config

    if name is not None and not valid_secret_name(name):
        raise ValueError(
            f"{field}: a secret name is 1-128 letters, digits, '.', '_' or '-', starting with a "
            "letter or digit"
        )
    return name


class DnsTarget(_Model):
    """Where a VM's new address goes in DNS (SDD §4.2, §23.5): a Designate zone's record, reached
    with the destination provider's credential, or an RFC 2136 server's, signed with a TSIG key
    held as a secret (owner decision 6)."""

    kind: Literal["designate", "rfc2136"]
    zone: _Text255
    record: _Text255
    server: _Text255 | None = None
    tsig_secret: str | None = None

    @model_validator(mode="after")
    def _kind_fields(self) -> DnsTarget:
        if self.kind == "designate":
            for name in ("server", "tsig_secret"):
                if getattr(self, name) is not None:
                    raise ValueError(
                        f"{name}: a designate target takes none - Seamless reaches Designate with "
                        "the plan's own provider credential"
                    )
        else:
            for name in ("server", "tsig_secret"):
                if getattr(self, name) is None:
                    raise ValueError(f"{name}: an rfc2136 target needs it")
            _secret_name("tsig_secret", self.tsig_secret)
        return self


class NetworkCutover(_Model):
    """A plan's network cut-over settings (SDD §4.2, §23.3-§23.7, plan task N3): the catalog hooks
    it runs per point, the floating IP move, a DNS target per VM and the Octavia pool mapping."""

    #: per point, the names of catalog hooks in their run order, each once per point
    hooks: dict[HookPoint, list[Annotated[str, StringConstraints(min_length=1)]]] = Field(
        default_factory=dict
    )
    #: "keep_address": the move that keeps a floating IP's address (SDD §23.4, owner decision 4)
    floating_ips: Literal["auto", "keep_address"] = "auto"
    #: per VM (source id): the record its new address goes to (SDD §23.5)
    dns_targets: dict[str, DnsTarget] = Field(default_factory=dict)
    #: a source Octavia pool (its id) to a destination pool (its id) (SDD §23.7)
    lb_pools: dict[_Text255, _Text255] = Field(default_factory=dict)

    @field_validator("hooks")
    @classmethod
    def _once_per_point(cls, hooks: dict[str, list[str]]) -> dict[str, list[str]]:
        for point, names in hooks.items():
            twice = sorted(name for name, n in Counter(names).items() if n > 1)
            if twice:
                raise ValueError(f"{point}: names {', '.join(twice)} more than once")
        return hooks

    def hook_names(self) -> set[str]:
        """Every hook the settings name, at any point."""
        return {name for names in self.hooks.values() for name in names}


class Hook(_Model):
    """A hook of the catalog an admin keeps (SDD §4.2, §23.3): a playbook the AnsibleExecutor runs,
    or a webhook POSTed the context document with a bearer token; plans name it per point."""

    name: str = Field(pattern=HOOK_NAME)
    kind: Literal["playbook", "webhook"]
    description: str | None = None
    #: playbook: its absolute path, a .yml or .yaml file
    playbook: str | None = None
    #: webhook: an https:// URL without user information
    url: str | None = None
    #: webhook: the secret (SDD §13.3) whose key `token` is its bearer token
    secret: str | None = None
    timeout_s: int = Field(default=300, ge=1, le=1800)
    #: the hook a rollback runs to undo this one: another hook of the catalog
    revert: str | None = None
    #: server-owned
    created_at: UTCDateTime = Field(default_factory=utcnow)
    updated_at: UTCDateTime = Field(default_factory=utcnow)

    @property
    def id(self) -> str:
        """The store's key: the hook's name (SDD §11)."""
        return self.name

    @model_validator(mode="after")
    def _kind_fields(self) -> Hook:
        own = {"playbook": ("playbook",), "webhook": ("url", "secret")}[self.kind]
        other = {"playbook": ("url", "secret"), "webhook": ("playbook",)}[self.kind]
        for name in own:
            if getattr(self, name) is None:
                raise ValueError(f"{name}: a {self.kind} hook needs it")
        for name in other:
            if getattr(self, name) is not None:
                raise ValueError(f"{name}: a {self.kind} hook takes none")
        if self.playbook is not None:
            if not self.playbook.startswith("/"):
                raise ValueError("playbook: an absolute path")
            if not self.playbook.lower().endswith((".yml", ".yaml")):
                raise ValueError("playbook: a .yml or .yaml file")
        if self.url is not None:
            _https_url(self.url)
        _secret_name("secret", self.secret)
        if self.revert == self.name:
            raise ValueError("revert: a hook cannot revert itself")
        return self


def _https_url(url: str) -> None:
    """A webhook's URL (SDD §4.2, §23.9): https:// with a host, and no user information - a
    credential belongs in its secret."""
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        host, parts = None, None
    if parts is None or parts.scheme.lower() != "https" or not host:
        raise ValueError("url: an https:// URL with a host")
    if parts.username is not None or parts.password is not None:
        raise ValueError("url: no user information - put the credential in the hook's secret")


#: the pre-flight codes a plan may acknowledge for a VM (SDD §4.2, §9.3): a blocker it then reports
#: as a warning that says so - an autoscaling group's member (owner decision 9)
ACKNOWLEDGEABLE_FINDINGS = frozenset({"SRC_VM_AUTOSCALED"})


class PlanSpec(_Model):
    """Editable plan fields; ``PlanCreate``, the wire schema of SDD §12, refuses the rest."""

    name: str
    description: str | None = None
    source_provider_id: str
    destination_provider_id: str
    vm_ids: list[str]
    mappings: Mappings = Field(default_factory=Mappings)
    default_strategy: Strategy | Literal["auto"] = "auto"
    strategy_overrides: dict[str, Strategy] = Field(default_factory=dict)
    selection_policy: Literal["min_downtime", "simplest_meeting_slo"] = "min_downtime"
    downtime_slo_s: int = Field(default=600, ge=1)
    require_approval: bool = True
    #: maker-checker (SDD §5.4, ADV-17): the cutover's requester never counts as its approver
    require_distinct_approver: bool = True
    auto_cutover: bool = False
    cutover_window: CutoverWindow | None = None
    #: at least a minute: a shorter interval would run delta passes back to back
    keep_warm_interval_s: int = Field(default=900, ge=60)
    convergence_threshold_bytes: int = Field(default=1073741824, ge=0)
    max_sync_passes: int = Field(default=5, ge=1)
    link_bps: float = Field(default=131072000.0, gt=0)
    #: overrides of ``EstimatorParams`` fields for this plan (SDD §9.1)
    estimator_overrides: dict[str, float] = Field(default_factory=dict)
    handover: HandoverConfig = Field(default_factory=HandoverConfig)
    verification: VerificationConfig = Field(default_factory=VerificationConfig)
    #: per VM (source id): its application probes instead of verification.app_probes; [] = none
    probe_overrides: dict[str, list[AppProbe]] = Field(default_factory=dict)
    #: hours a migration may wait in completed before it is flagged for finalize (SDD §8.1, ADV-19);
    #: TODO(ADV-19): confirm 72 h — the owner's open question 14
    finalize_warning_h: int = Field(default=72, ge=1)
    prestage_resources: list[str] = Field(default_factory=lambda: list(DEFAULT_PRESTAGE))
    #: "neutron": OpenStack cold/warm recreate every port with its MAC, all its fixed IPs and its
    #: security groups (SDD §6.6, §7.2, §9.2); "nova": one fixed IP per port and a new MAC
    port_creation_mode: Literal["nova", "neutron"] = "nova"
    #: per VM (source id), per NIC MAC (lower case): its network and addresses at RHOSO; OpenStack
    #: sources (SDD §4.2, §7.2, §7.3, §9.2, §9.3)
    nic_overrides: dict[str, dict[str, NicOverride]] = Field(default_factory=dict)
    #: per VM (source id): the pre-flight codes the plan acknowledges for it, each one of
    #: ACKNOWLEDGEABLE_FINDINGS (SDD §4.2, §9.3, owner decision 9)
    acknowledged_findings: dict[str, list[str]] = Field(default_factory=dict)
    #: the hooks per point, the floating IP move, DNS targets and Octavia pool mappings (SDD §4.2,
    #: §23.3-§23.7, plan task N3)
    network_cutover: NetworkCutover = Field(default_factory=NetworkCutover)

    def nic_override(self, vm_id: str, mac: str | None) -> NicOverride | None:
        """The plan's override of a VM's NIC, by its MAC (SDD §4.2)."""
        if not mac:
            return None
        return (self.nic_overrides.get(vm_id) or {}).get(mac.lower())

    def nic_target(self, vm_id: str, nic: Nic) -> tuple[str, list[str]]:
        """Where a VM's NIC goes at RHOSO (SDD §9.2, §9.3): its override's network, else the
        plan's mapping of its network; its override's addresses, else its own."""
        entry = self.nic_override(vm_id, nic.mac)
        network = (entry.network if entry is not None else None) or self.mappings.networks.get(
            nic.network, nic.network
        )
        if entry is not None and entry.fixed_ips is not None:
            return network, list(entry.fixed_ips)
        return network, list(nic.fixed_ips)

    @field_validator("nic_overrides")
    @classmethod
    def _lower_case_macs(
        cls, value: dict[str, dict[str, NicOverride]]
    ) -> dict[str, dict[str, NicOverride]]:
        return {
            vm: {mac.lower(): entry for mac, entry in nics.items()} for vm, nics in value.items()
        }


class PlanCreate(PlanSpec):
    """The ``POST /plans`` body (SDD §12): a field ``PlanSpec`` does not have is refused (422),
    never dropped — a misspelt guard would otherwise yield a plan without it; stored plans keep
    loading with fields a later version added (``_Model``, §11)."""

    model_config = ConfigDict(extra="forbid")


#: SDD §12: what one probe list may hold, and the longest http(s) path
MAX_PROBES_PER_VM = 10
MAX_PROBE_PATH = 200


def _nic_override_problems(overrides: dict[str, dict[str, NicOverride]]) -> list[str]:
    """What makes a NIC override unusable before any cloud is asked (SDD §12)."""
    problems: list[str] = []
    for vm_id, nics in overrides.items():
        for mac in sorted(nics):
            entry, where = nics[mac], f"nic_overrides[{vm_id}][{mac}]"
            if not _MAC.fullmatch(mac):
                problems.append(f"{where}: not a MAC address")
            if entry.network is None and entry.fixed_ips is None:
                problems.append(f"{where}: give a network, addresses or both")
            if entry.network is not None and not entry.network.strip():
                problems.append(f"{where}: network must not be empty")
            if entry.fixed_ips is None:
                continue
            if not entry.fixed_ips:
                problems.append(
                    f"{where}: fixed_ips must list at least one address "
                    "(leave it out to keep the NIC's own)"
                )
            bad = [a for a in entry.fixed_ips if not _is_ip(a)]
            if bad:
                problems.append(f"{where}: not an IP address: {', '.join(bad)}")
            twice = sorted({a for a in entry.fixed_ips if entry.fixed_ips.count(a) > 1})
            if twice:
                problems.append(f"{where}: {', '.join(twice)} given twice")
    return problems


def _probe_problems(where: str, probes: Sequence[AppProbe]) -> list[str]:
    problems = [
        f"{where}: port {p.port} outside 1-65535" for p in probes if not 1 <= p.port <= 65535
    ]
    problems += [
        f"{where}: path {p.path!r} must start with '/' (at most {MAX_PROBE_PATH} characters)"
        for p in probes
        if p.kind != "tcp" and (not p.path.startswith("/") or len(p.path) > MAX_PROBE_PATH)
    ]
    problems += [
        f"{where}: expect_status {p.expect_status} outside 100-599"
        for p in probes
        if p.expect_status is not None and not 100 <= p.expect_status <= 599
    ]
    if len(probes) > MAX_PROBES_PER_VM:
        problems.append(f"{where}: {len(probes)} probes, at most {MAX_PROBES_PER_VM}")
    return problems


#: the estimator's bounds (SDD §9.1, reviews RV51-07, RV51-22): a plan's sync passes at most, a rate
#: it divides by at least (B/s), a duration it adds at most (s) - so every estimate is finite
MAX_SYNC_PASSES = 100
MIN_RATE_BPS = 1.0
MAX_DURATION_S = 10**9


def repeated_vm_ids(vm_ids: Sequence[str]) -> list[str]:
    """VM ids a plan lists more than once: one VM, one migration (SDD §12)."""
    return sorted(vm_id for vm_id, count in Counter(vm_ids).items() if count > 1)


def invalid_plan_settings(spec: PlanSpec) -> list[str]:
    """Settings that would fail every verification or never open the cutover gate (SDD §12).

    Checked on create/patch and at validation; the model itself stays permissive so that plans
    stored before the check keep loading.
    """
    problems: list[str] = []
    verification = spec.verification
    for name in ("tcp_ports", "windows_tcp_ports"):
        bad = [port for port in getattr(verification, name) if not 1 <= port <= 65535]
        if bad:
            problems.append(f"verification.{name} {bad} outside 1-65535")
    if verification.timeout_s < 0:  # 0 checks once without polling
        problems.append(
            f"verification.timeout_s must not be negative (got {verification.timeout_s})"
        )
    # application probes (SDD §7.5, §12, ADV-18)
    problems += _probe_problems("verification.app_probes", verification.app_probes)
    for vm_id, probes in spec.probe_overrides.items():
        problems += _probe_problems(f"probe_overrides[{vm_id}]", probes)
    problems += _nic_override_problems(spec.nic_overrides)
    # a blocker the plan may take as acknowledged: an autoscaling group's member (SDD §4.2, §9.3)
    for vm_id, codes in spec.acknowledged_findings.items():
        refused = [code for code in codes if code not in ACKNOWLEDGEABLE_FINDINGS]
        if refused:
            problems.append(
                f"acknowledged_findings[{vm_id}]: {', '.join(refused)} cannot be acknowledged "
                f"(only {', '.join(sorted(ACKNOWLEDGEABLE_FINDINGS))} can)"
            )
    unknown = [r for r in spec.prestage_resources if r not in PRESTAGE_RESOURCES]
    if unknown:
        problems.append(f"prestage_resources: cannot pre-stage {', '.join(map(repr, unknown))}")
    window = spec.cutover_window
    if window is not None and window.end <= window.start:
        problems.append("cutover_window: end must be after start")
    # the JSON parser accepts the Infinity literal, which gt=0 lets through, and a rate a hair
    # above zero makes every estimate infinite (SDD §9.1, review RV51-22)
    if not math.isfinite(spec.link_bps):
        problems.append(f"link_bps must be a finite positive number (got {spec.link_bps})")
    elif spec.link_bps < MIN_RATE_BPS:
        problems.append(f"link_bps must be at least 1 B/s (got {spec.link_bps})")
    # the model runs a pass per allowed pass, in the event loop (SDD §9.1, review RV51-07)
    if spec.max_sync_passes > MAX_SYNC_PASSES:
        problems.append(
            f"max_sync_passes must be at most {MAX_SYNC_PASSES} (got {spec.max_sync_passes})"
        )
    return problems


#: Fields a client may send in ``PlanCreate`` / ``PATCH /plans/{id}``.
PLAN_EDITABLE_FIELDS = frozenset(PlanSpec.model_fields)


class Plan(PlanSpec):
    id: str = Field(default_factory=new_plan_id)
    waves: list[Wave] = Field(default_factory=list)
    status: PlanStatus = PlanStatus.draft
    created_at: UTCDateTime = Field(default_factory=utcnow)
    updated_at: UTCDateTime = Field(default_factory=utcnow)

    def wave_of(self, vm_id: str) -> Wave | None:
        for wave in self.waves:
            if vm_id in wave.vm_ids:
                return wave
        return None


class SyncPass(_Model):
    number: int = Field(ge=1)
    kind: SyncPassKind
    started_at: UTCDateTime
    ended_at: UTCDateTime | None = None
    bytes_scanned: int = 0
    bytes_changed: int = 0
    bytes_transferred: int = 0
    duration_s: float | None = None


class Finding(_Model):
    code: str
    severity: Severity
    message: str
    remediation: str | None = None
    #: empty = applies to every strategy
    strategies: list[Strategy] = Field(default_factory=list)

    def applies_to(self, strategy: Strategy) -> bool:
        return not self.strategies or strategy in self.strategies


class Estimate(_Model):
    strategy: Strategy
    eligible: bool = True
    reasons: list[str] = Field(default_factory=list)
    precopy_s: float
    passes: int
    downtime_s: float
    total_s: float
    final_delta_bytes: int
    meets_slo: bool
    #: shutdown_s, copy_s (final sync or full copy), conversion_s, create_s, boot_s, and hooks_s
    #: when the plan runs post_create hooks: they add up to downtime_s (SDD §4, §9.1, ADV-16, N10)
    downtime_parts: dict[str, float] = Field(default_factory=dict)
    #: the VM's DNS record's TTL, apart from the downtime: clients may reach the old address that
    #: long after the swap (SDD §8.4, §9.1, plan task N10)
    dns_tail_s: float | None = None


AdvisorKind = Literal["strategy", "classification", "verification", "similar_incidents", "screen"]


class AdvisorNote(_Model):
    kind: AdvisorKind
    source: Literal["jev", "rules", "memory"]
    summary: str
    confidence: float | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    created_at: UTCDateTime = Field(default_factory=utcnow)

    _storable = field_validator("summary", mode="before")(_storable_text_field)

    @model_validator(mode="before")
    @classmethod
    def _no_number_that_is_none(cls, data: Any) -> Any:
        """A confidence, a hit's score or a claim's confidence v0.1.0-rc.2 stored as NaN or an
        infinity reads as None, and a NUL or a lone surrogate in its data as U+FFFD, so the
        migration holding the note is written again (SDD §11, reviews RV51-06, RV52-S1)."""
        if isinstance(data, dict) and ("confidence" in data or "data" in data):
            data = {**data}
            if "confidence" in data:
                data["confidence"] = _as_written(data["confidence"])
            if isinstance(data.get("data"), dict):
                data["data"] = _as_written(data["data"])
        return data


class AdvisorActor(_Model):
    """Who took a decision: ``policy:<id>``, ``user:<name>`` or ``system:fallback`` (SDD §14.4)."""

    kind: Literal["policy", "user", "system"]
    id: str


class AdvisorModelInfo(_Model):
    name: Literal["jev", "rules"]
    version: str
    deployment: Literal["on_prem", "remote", "unknown"] = "unknown"
    endpoint_host: str | None = None

    _storable = field_validator("version", mode="before")(_storable_text_field)


class RecommendedOption(_Model):
    option: str | None = None
    #: 0..1 - the only number compared with a threshold (SDD §14.4)
    confidence: float | None = None
    #: raw probabilities per option, shown only in Details
    distribution: dict[str, float] = Field(default_factory=dict)
    #: why the answer could not be applied when it was recorded (§14.4 rule 2), else None: no
    #: approve takes it (SDD §4.2, §14.6, audit AD-03)
    unusable: str | None = None

    _storable = field_validator("option", "unusable", mode="before")(_storable_text_field)
    _storable_distribution = field_validator("distribution", mode="before")(_storable_keys)

    @model_validator(mode="before")
    @classmethod
    def _no_number_that_is_none(cls, data: Any) -> Any:
        """A confidence v0.1.0-rc.2 stored as NaN or an infinity reads as None, such a probability
        as absent (SDD §11, review RV51-06)."""
        if isinstance(data, dict):
            data = {**data}
            if "confidence" in data:
                data["confidence"] = _as_written(data["confidence"])
            if isinstance(data.get("distribution"), dict):
                data["distribution"] = {
                    key: value
                    for key, value in data["distribution"].items()
                    if not (isinstance(value, float) and not math.isfinite(value))
                }
        return data


class Decision(_Model):
    final_option: str | None = None
    status: DecisionStatus
    reason_code: ReasonCode
    reason_text: str | None = None
    actor: AdvisorActor
    policy_id: str | None = None
    threshold_used: float | None = None
    decided_at: UTCDateTime = Field(default_factory=utcnow)
    #: the deciding policy's rules for a person's approve or override, as they were then (SDD
    #: §14.6); None: the fallback, or a decision stored before - the policy in force applies
    require_distinct_approver: bool | None = None
    require_override_reason: bool | None = None

    _storable = field_validator("reason_text", mode="before")(_storable_text_field)


class AdvisorSubject(_Model):
    kind: Literal["vm", "plan", "wave"]
    id: str
    name: str


EvidenceSourceKind = Literal["inventory", "preflight", "estimate", "plan", "provider", "run"]
EvidenceEffect = Literal["supports", "opposes", "context"]


class EvidenceSource(_Model):
    """Where an evidence item comes from: the VM, the migration, the plan or the provider."""

    kind: EvidenceSourceKind
    ref_id: str = Field(min_length=1)


class EvidenceItem(_Model):
    """One fact a recommendation rests on, built by code, never by the model (SDD §14.4, ADV-02)."""

    key: str
    label: str
    value: str | int | float | bool | None = None
    display: str
    #: measured against the recommendation's ``evidence_option``
    effect: EvidenceEffect
    #: relevance, 0..100: the card shows the heaviest
    weight: int = Field(ge=0, le=100)
    source: EvidenceSource


class SimilarIncident(_Model):
    """A past incident agentmemory knew when the recommendation was recorded (SDD §14.3)."""

    title: str
    content: str = Field(max_length=1000)
    score: float | None = None

    _storable = field_validator("title", "content", mode="before")(_storable_text_field)

    @model_validator(mode="before")
    @classmethod
    def _no_number_that_is_none(cls, data: Any) -> Any:
        """A score v0.1.0-rc.2 stored as NaN or an infinity reads as no score (SDD §11, §14.3,
        review RV51-06)."""
        if isinstance(data, dict) and "score" in data:
            data = {**data, "score": _as_written(data["score"])}
        return data


class AdvisorReason(_Model):
    """A reason Jev gave that names one of the recommendation's evidence items."""

    key: str
    text: str

    _storable = field_validator("text", mode="before")(_storable_text_field)


class AdvisorRecommendation(_Model):
    """A recommendation and the decision taken on it, stored apart (document kind
    ``recommendation``, SDD §4.2, §14.4). The engine reads only the decision."""

    id: str = Field(default_factory=new_recommendation_id)
    created_at: UTCDateTime = Field(default_factory=utcnow)
    decision_type: DecisionType
    subject: AdvisorSubject
    plan_id: str | None = None
    migration_id: str | None = None
    #: the plan's source provider
    provider_id: str | None = None
    model: AdvisorModelInfo
    recommendation: RecommendedOption
    decision: Decision
    #: the redacted input the recommendation rests on (document kind ``advisor_snapshot``, §14.5)
    input_snapshot_sha256: str | None = None
    #: the user whose plan validation recorded it: never the one who decides it (SDD §14.6)
    requested_by: str | None = None
    legacy: bool = False
    legacy_text: str | None = None
    #: the option every evidence effect is measured against (SDD §14.4, ADV-02)
    evidence_option: str | None = None
    #: what validation knew when it was recorded, heaviest first
    evidence: list[EvidenceItem] = Field(default_factory=list)
    #: at most 3, searched once when it was recorded
    similar_incidents: list[SimilarIncident] = Field(default_factory=list)
    #: Jev's reasons that name one of the evidence keys
    reasons: list[AdvisorReason] = Field(default_factory=list)

    _storable = field_validator("legacy_text", mode="before")(_storable_text_field)

    @model_validator(mode="before")
    @classmethod
    def _unusable_stored_before_the_field(cls, data: Any) -> Any:
        """A recommendation stored before ``unusable`` (v0.1.0-rc.2) whose decision fell back
        reads as ``unusable`` the why that decision recorded: no approve takes an answer that
        could not be applied (SDD §4.2, §14.6, review RV50-07)."""
        if not isinstance(data, dict):
            return data
        option, decision = data.get("recommendation"), data.get("decision")
        if (
            isinstance(option, dict)
            and option.get("unusable") is None
            and isinstance(decision, dict)
            and decision.get("status") == DecisionStatus.fallback_rules
        ):
            why = decision.get("reason_text")
            if not isinstance(why, str) or not why:
                why = "the advisor's answer could not be applied"
            data = {**data, "recommendation": {**option, "unusable": why}}
        return data


class AdvisorSnapshot(_Model):
    """A redacted advisor input, stored once under the SHA-256 of its canonical JSON (SDD §14.5)."""

    sha256: str
    created_at: UTCDateTime = Field(default_factory=utcnow)
    payload: dict[str, Any]

    @property
    def id(self) -> str:
        return self.sha256


class PolicyScope(_Model):
    """Where a policy applies: plans or source providers; neither = everywhere (SDD §14.6)."""

    provider_ids: list[str] = Field(default_factory=list)
    plan_ids: list[str] = Field(default_factory=list)


class AdvisorPolicySpec(_Model):
    """A policy as it is written (POST/PUT /advisor/policies, SDD §12, §14.6)."""

    name: str = Field(min_length=1, max_length=200)
    scope: PolicyScope = Field(default_factory=PolicyScope)
    #: none = every decision type
    decision_types: list[DecisionType] = Field(default_factory=list)
    mode: Literal["auto_apply_above_threshold", "always_require_approval"]
    threshold: float = Field(ge=0, le=1)
    #: the evidence items that support or oppose (SDD §14.4): a new policy asks for 3 (ADV-02); the
    #: built-in default asks for none
    min_evidence_for_auto_apply: int = Field(default=3, ge=0, le=100)
    require_override_reason: bool = True
    #: maker-checker (ADV-17): the decision's requester neither approves nor overrides it
    require_distinct_approver: bool = True

    @model_validator(mode="after")
    def _one_scope_level(self) -> AdvisorPolicySpec:
        if self.scope.provider_ids and self.scope.plan_ids:
            raise ValueError("a policy scopes either providers or plans, not both")
        return self


class AdvisorPolicy(AdvisorPolicySpec):
    """A stored advisor policy (document kind ``advisor_policy``, SDD §4.2, §14.6)."""

    id: str = Field(default_factory=new_policy_id)
    created_at: UTCDateTime = Field(default_factory=utcnow)
    updated_at: UTCDateTime = Field(default_factory=utcnow)
    updated_by: str


class ForceCutover(_Model):
    """A cutover before convergence, asked for with a reason (SDD §5.4, ADV-16)."""

    actor: str
    at: UTCDateTime
    reason: str
    #: the last pass's bytes_changed, and the estimated final downtime, when it was asked for
    delta_bytes: int
    estimated_downtime_s: float


class GpuWait(_Model):
    """A cutover that waits for GPU capacity, its source running (SDD §4.2, §5.4 rule 5, §22.7):
    what the last capacity read found short - or that it failed -, since when."""

    #: when the destination answered the read that began the wait
    since: UTCDateTime
    #: when the destination answered the last read
    checked_at: UTCDateTime
    #: what that read found short, by pool_name: a pool, or a class alone across its pools; ""
    #: while no read of the wait could list the GPU flavor's requests
    pool: str
    #: the units the cutover needs there: its GPU flavor's, plus those of the destination's other
    #: migrations in cutover with no server yet; for a request group of n > 1 units, the n one
    #: provider must have free
    needed: int
    #: the units the read found free there (the most on one provider, for such a group); None
    #: when the read failed
    free: int | None = None
    #: why, in words, naming the destination by its label; a failure by its HTTP status,
    #: Placement error code or kind, never a message's text (§22.12)
    reason: str

    _storable = field_validator("pool", "reason", mode="before")(_storable_text_field)


class Approval(_Model):
    actor: str
    at: UTCDateTime = Field(default_factory=utcnow)
    comment: str | None = None


class PhaseChange(_Model):
    from_phase: Phase | None = None
    to_phase: Phase
    at: UTCDateTime = Field(default_factory=utcnow)
    reason: str
    actor: str

    _storable = field_validator("reason", mode="before")(_storable_text_field)


class FipMove(_Model):
    """One floating IP of a migration's keep-address move (SDD §4.2, §8.3): its address, the NIC's
    first address at the destination whose port receives it, the source port it was on - saved
    before the disassociate -, and how far the move went."""

    address: str
    fixed_ip: str
    source_port: str | None = None
    state: Literal["releasing", "released", "moved", "returned", "finalized"] = "releasing"


class DnsSwap(_Model):
    """A migration's DNS swap (SDD §4.2, §8.4): the record's fully qualified name and type, the
    VM's address at the source and the one taking its place, and how far the swap went."""

    name: str
    rdtype: Literal["A", "AAAA"]
    old: str
    new: str
    state: Literal["swapping", "swapped", "unchanged", "conflict", "reverted"] = "swapping"


class LbMove(_Model):
    """A membership the Octavia member helper moves (SDD §4.2, §8.5): the mapped source pool and the
    VM's member there, that member's address and port, the destination pool and the member created
    there, and how far the move went."""

    source_pool: str
    source_member: str
    address: str
    port: int
    pool: str
    member: str | None = None
    state: Literal["draining", "drained", "joining", "joined", "restored"] = "draining"


class Migration(_Model):
    id: str = Field(default_factory=new_migration_id)
    plan_id: str
    wave_id: str | None = None
    vm: VMRef
    strategy: Strategy
    phase: Phase = Phase.pending
    phase_history: list[PhaseChange] = Field(default_factory=list)
    progress_pct: float = 0
    bytes_total: int = 0
    bytes_transferred: int = 0
    sync_passes: list[SyncPass] = Field(default_factory=list)
    #: bytes of the passes dropped from sync_passes (SDD §5.4); bytes_transferred still counts them
    sync_bytes_dropped: int = 0
    estimate: Estimate | None = None
    estimates: list[Estimate] = Field(default_factory=list)
    #: per-stream scan throughput measured by the last warm pass (SDD §9.1 calibration)
    observed_scan_bps: float | None = None
    #: mappings matched automatically by pre-flight (e.g. the smallest fitting flavor)
    resolved_mappings: Mappings = Field(default_factory=Mappings)
    findings: list[Finding] = Field(default_factory=list)
    checkpoint: str | None = None
    downtime_started_at: UTCDateTime | None = None
    downtime_ended_at: UTCDateTime | None = None
    actual_downtime_s: float | None = None
    approvals: list[Approval] = Field(default_factory=list)
    cutover_requested: bool = False
    #: who asked for the cutover first, and when (SDD §5.4): never the approver that counts
    cutover_requested_by: str | None = None
    cutover_requested_at: UTCDateTime | None = None
    #: cutover window bypass granted with the request (SDD §5.4 rule 2); persisted
    force_window: bool = False
    advisor_notes: list[AdvisorNote] = Field(default_factory=list)
    #: the recommendation whose decision waits for approval: no start meanwhile (SDD §5.4, §14.6)
    advisor_hold: str | None = None
    #: the recommendation its last validation stored: what a decision acts on while no hold names
    #: one - written in that validation's transaction, so a clock step back never makes the one
    #: it replaced the newest (SDD §14.6, review RV51-08); None: none stored since the field
    last_recommendation: str | None = None
    #: the convergence rule that ended the passes (SDD §5.3, ADV-16)
    converged_by: Literal["threshold", "slo", "max_passes", "force"] | None = None
    force_cutover: ForceCutover | None = None
    #: the model seconds a simulated cutover spent with the source stopped (demo, SDD §5.2)
    downtime_model_s: float | None = None
    #: invariant violations found when it completed (SDD §5.5)
    data_issues: list[str] = Field(default_factory=list)
    #: what the last warm pass met that the cutover refuses before the source stops (SDD §7.2):
    #: each pass replaces it, a successful cutover empties it
    cutover_refusals: list[str] = Field(default_factory=list)
    #: the last verification: its checks and application probe results (SDD §7.5, ADV-18)
    verification: VerificationRecord | None = None
    #: the rules' probe suggestion at validation (SDD §9.6): runs only once added to the plan
    suggested_probes: list[AppProbe] = Field(default_factory=list)
    #: completed, not finalized: when its source VM was seen running again (SDD §8.1, ADV-19)
    source_running_at: UTCDateTime | None = None
    #: in ready, awaiting_cutover or a keep-warm pass: its cutover waits for GPU capacity (SDD §5.4
    #: rule 5, §22.7); leaving those phases drops it in the same write (domain.fsm)
    gpu_wait: GpuWait | None = None
    review_required: bool = False
    review_reason: str | None = None

    _storable = field_validator("review_reason", "error", mode="before")(_storable_text_field)
    destination_server_id: str | None = None
    error: str | None = None
    attempts: int = 0
    #: each retried attempt's strategy, oldest first: the evidence names them (SDD §14.4)
    attempt_strategies: list[Strategy] = Field(default_factory=list)
    #: the reverts a rollback owes: the revert of each hook that ran with success at pre_stop,
    #: post_create or post_verify since the last rollback, in run order (SDD §4.2, §8.2)
    hook_reverts: list[str] = Field(default_factory=list)
    #: the journal of its floating IPs' keep-address move (SDD §4.2, §8.3)
    fip_moves: list[FipMove] = Field(default_factory=list)
    #: the journal of its DNS target's swap (SDD §4.2, §8.4)
    dns_swap: DnsSwap | None = None
    #: the journal of its Octavia memberships' move by the member helper (SDD §4.2, §8.5)
    lb_moves: list[LbMove] = Field(default_factory=list)
    created_at: UTCDateTime = Field(default_factory=utcnow)
    updated_at: UTCDateTime = Field(default_factory=utcnow)

    def estimate_for(self, strategy: Strategy) -> Estimate | None:
        for est in self.estimates:
            if est.strategy == strategy:
                return est
        return None


class SummaryVM(_Model):
    """The VM of a migration summary: which one, by id and name, and its project (SDD §4)."""

    source_id: str
    name: str
    project: str | None


class MigrationSummary(_Model):
    """A migration's fields that the Overview's and the plan page's lists read, as the Migration has
    them (SDD §4, ``GET /migrations?view=summary``): no passes, all the estimates, notes, history or
    VM details. ``phase_since`` is the last PhaseChange's time, when the phase it is in began, else
    the creation time; ``preflight_ran`` whether the history reaches ready or blocked;
    ``eligible_strategies`` the estimates' eligible strategies, in their order."""

    id: str
    plan_id: str
    wave_id: str | None
    vm: SummaryVM
    strategy: Strategy
    phase: Phase
    phase_since: UTCDateTime
    preflight_ran: bool
    eligible_strategies: list[Strategy]
    progress_pct: float
    estimate: Estimate | None
    actual_downtime_s: float | None
    downtime_started_at: UTCDateTime | None
    downtime_ended_at: UTCDateTime | None
    approvals: list[Approval]
    cutover_requested: bool
    cutover_requested_by: str | None
    force_cutover: ForceCutover | None
    force_window: bool
    advisor_hold: str | None
    review_required: bool
    review_reason: str | None
    error: str | None
    source_running_at: UTCDateTime | None
    findings: list[Finding]
    suggested_probes: list[AppProbe]
    cutover_refusals: list[str]
    gpu_wait: GpuWait | None
    created_at: UTCDateTime
    updated_at: UTCDateTime

    @classmethod
    def of(cls, m: Migration) -> MigrationSummary:
        return cls(
            id=m.id,
            plan_id=m.plan_id,
            wave_id=m.wave_id,
            vm=SummaryVM(source_id=m.vm.source_id, name=m.vm.name, project=m.vm.project),
            strategy=m.strategy,
            phase=m.phase,
            phase_since=m.phase_history[-1].at if m.phase_history else m.created_at,
            preflight_ran=any(c.to_phase in (Phase.ready, Phase.blocked) for c in m.phase_history),
            eligible_strategies=[e.strategy for e in m.estimates if e.eligible],
            progress_pct=m.progress_pct,
            estimate=m.estimate,
            actual_downtime_s=m.actual_downtime_s,
            downtime_started_at=m.downtime_started_at,
            downtime_ended_at=m.downtime_ended_at,
            approvals=m.approvals,
            cutover_requested=m.cutover_requested,
            cutover_requested_by=m.cutover_requested_by,
            force_cutover=m.force_cutover,
            force_window=m.force_window,
            advisor_hold=m.advisor_hold,
            review_required=m.review_required,
            review_reason=m.review_reason,
            error=m.error,
            source_running_at=m.source_running_at,
            findings=m.findings,
            suggested_probes=m.suggested_probes,
            cutover_refusals=m.cutover_refusals,
            gpu_wait=m.gpu_wait,
            created_at=m.created_at,
            updated_at=m.updated_at,
        )


#: SDD §5.4: the latest passes sync_passes keeps after the first plan.max_sync_passes
SYNC_PASSES_LATEST = 20


def next_pass_number(m: Migration) -> int:
    """The number of the migration's next pass; it keeps counting after passes are dropped."""
    return m.sync_passes[-1].number + 1 if m.sync_passes else 1


def keep_sync_history(m: Migration, keep_first: int, keep_latest: int = SYNC_PASSES_LATEST) -> None:
    """Drop the oldest passes between the first ``keep_first`` and the latest ``keep_latest``
    (SDD §5.4): a long wait for the cutover runs a keep-warm pass every interval, which must not
    grow the migration without bound. The dropped passes' bytes move to ``sync_bytes_dropped``."""
    excess = len(m.sync_passes) - keep_first - keep_latest
    if excess <= 0:
        return
    dropped = m.sync_passes[keep_first : keep_first + excess]
    m.sync_bytes_dropped += sum(p.bytes_transferred for p in dropped)
    del m.sync_passes[keep_first : keep_first + excess]


class Event(_Model):
    seq: int = 0
    ts: UTCDateTime = Field(default_factory=utcnow)
    kind: str
    plan_id: str | None = None
    migration_id: str | None = None
    actor: str = "system"
    message: str = ""
    data: dict[str, Any] = Field(default_factory=dict)


class ValidationItem(_Model):
    migration_id: str
    vm_name: str
    strategy: Strategy
    phase: Phase
    findings: list[Finding] = Field(default_factory=list)
    estimates: list[Estimate] = Field(default_factory=list)


class ValidationReport(_Model):
    """Answer of ``POST /plans/{id}/validate`` (SDD §12)."""

    plan_id: str
    ok: bool
    migrations: list[ValidationItem] = Field(default_factory=list)
