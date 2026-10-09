"""Pydantic v2 domain models (SDD §4.2).

JSON field names are exactly the attribute names. Timestamps are timezone-aware UTC and
serialize as ISO-8601 strings ending in ``Z``; naive inputs are interpreted as UTC.
"""

from __future__ import annotations

import math
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
    computed_field,
    field_validator,
    model_validator,
)

from ..guest_os import GuestOS, identify
from .enums import Phase, PlanStatus, ProviderKind, ProviderRole, Severity, Strategy, SyncPassKind

GIB = 2**30
#: Fraction of the provisioned size assumed used when a disk does not report usage (SDD §4.2).
USED_FALLBACK_RATIO = 0.6


def utcnow() -> datetime:
    return datetime.now(UTC)


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


UTCDateTime = Annotated[datetime, AfterValidator(_to_utc)]


def new_plan_id() -> str:
    return f"plan-{secrets.token_hex(4)}"


def new_migration_id() -> str:
    return f"mig-{secrets.token_hex(5)}"


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


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


PowerState = Literal["running", "stopped", "paused", "error", "transitioning", "unknown"]


class VMRef(_Model):
    """A source VM as inventoried (SDD §4.2). Tenant-controlled strings (name, tags, …) are
    stored in JSON documents: PostgreSQL JSONB rejects NUL characters, so they are stripped."""

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

    @field_validator("name", "project", "flavor", "os_type", "host", mode="before")
    @classmethod
    def _strip_nul(cls, value: Any) -> Any:
        return value.replace("\x00", "") if isinstance(value, str) else value

    @field_validator("tags", "flavor_extra_specs", mode="before")
    @classmethod
    def _strip_nul_map(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                str(k).replace("\x00", ""): (v.replace("\x00", "") if isinstance(v, str) else v)
                for k, v in value.items()
            }
        return value

    change_rate_bps: float | None = None

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


class HandoverConfig(_Model):
    enabled: bool = False
    #: source volume_type -> RHOSO cinder host "hostgroup@backend#pool"
    backend_map: dict[str, str] = Field(default_factory=dict)


class CutoverWindow(_Model):
    start: UTCDateTime
    end: UTCDateTime

    def contains(self, moment: datetime) -> bool:
        return self.start <= _to_utc(moment) <= self.end


DEFAULT_CONSOLE_PATTERNS = ["login:", "Cloud-init v\\. .* finished", "Reached target .*Multi-User"]


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


class PlanSpec(_Model):
    """Editable plan fields; this is the ``PlanCreate`` wire schema (SDD §12)."""

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
    prestage_resources: list[str] = Field(default_factory=lambda: list(DEFAULT_PRESTAGE))


PlanCreate = PlanSpec


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
    window = spec.cutover_window
    if window is not None and window.end <= window.start:
        problems.append("cutover_window: end must be after start")
    # the JSON parser accepts the Infinity literal, which gt=0 lets through (SDD §9.1)
    if not math.isfinite(spec.link_bps):
        problems.append(f"link_bps must be a finite positive number (got {spec.link_bps})")
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


AdvisorKind = Literal["strategy", "classification", "verification", "similar_incidents", "screen"]


class AdvisorNote(_Model):
    kind: AdvisorKind
    source: Literal["jev", "rules", "memory"]
    summary: str
    confidence: float | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    created_at: UTCDateTime = Field(default_factory=utcnow)


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
    #: cutover window bypass granted with the request (SDD §5.4 rule 2); persisted
    force_window: bool = False
    advisor_notes: list[AdvisorNote] = Field(default_factory=list)
    review_required: bool = False
    review_reason: str | None = None
    destination_server_id: str | None = None
    error: str | None = None
    attempts: int = 0
    created_at: UTCDateTime = Field(default_factory=utcnow)
    updated_at: UTCDateTime = Field(default_factory=utcnow)

    def estimate_for(self, strategy: Strategy) -> Estimate | None:
        for est in self.estimates:
            if est.strategy == strategy:
                return est
        return None


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
