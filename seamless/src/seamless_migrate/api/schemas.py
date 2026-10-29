"""Request/response schemas of the REST API (SDD §12) that are not domain models."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ..domain.enums import DecisionType, DerivedPlanStatus, Phase, Role, Strategy
from ..domain.models import (
    AdvisorNote,
    AdvisorPolicy,
    AdvisorRecommendation,
    Plan,
    PlanCreate,
    ValidationItem,
    ValidationReport,
)
from ..stats import Stats, ThroughputPoint


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EmptyRequest(_Body):
    """The body of a route whose request is ``{}``: an empty object or none, a field refused
    (SDD §12, audit AA-04)."""


_SECRET = 4096  # longest accepted credential value
_TEXT = 2000  # longest free text of a migration action: comment, reason, confirm (SDD §12)


class ProviderCredentialsRequest(_Body):
    """Write-only provider credentials (SDD §12 ``PUT /providers/{id}/credentials``).

    OpenStack/RHOSO: password auth (``username``, ``password``, ``project_name``, domains) or an
    application credential; VMware: ``username``, ``password``, optional ``datacenter``.
    """

    auth_url: str | None = Field(default=None, max_length=2048)
    username: str | None = Field(default=None, max_length=256)
    password: str | None = Field(default=None, max_length=_SECRET, repr=False)
    project_name: str | None = Field(default=None, max_length=256)
    user_domain_name: str | None = Field(default=None, max_length=256)
    project_domain_name: str | None = Field(default=None, max_length=256)
    application_credential_id: str | None = Field(default=None, max_length=256)
    application_credential_secret: str | None = Field(default=None, max_length=_SECRET, repr=False)
    interface: Literal["public", "internal", "admin"] | None = None
    datacenter: str | None = Field(default=None, max_length=256)

    def values(self) -> dict[str, str]:
        return {
            k: v.strip() if k not in ("password", "application_credential_secret") else v
            for k, v in self.model_dump(exclude_none=True).items()
            if str(v).strip()
        }


class ProjectCredentialsRequest(_Body):
    """A Keystone project's application credential (SDD §12 ``PUT /providers/{id}/projects/
    {project_id}/credentials``, §27.4): its id and its secret alone - no password, which signs in to
    every project of its user, and no ``auth_url``, which would send the secret elsewhere."""

    application_credential_id: str = Field(max_length=256)
    application_credential_secret: str = Field(max_length=_SECRET, repr=False)

    def values(self) -> dict[str, str]:
        """The keys to store: the id stripped, the secret as given, as the provider's own."""
        return {
            "application_credential_id": self.application_credential_id.strip(),
            "application_credential_secret": self.application_credential_secret,
        }


class ConversionKeyRequest(_Body):
    private_key: str = Field(min_length=64, max_length=16384, repr=False)


class CephKeyRequest(_Body):
    """The Ceph user's key as ``ceph auth get-key`` prints it (SDD §12, §28.4): write-only."""

    key: str = Field(min_length=16, max_length=512, repr=False)


class WavesAutoRequest(_Body):
    max_wave_size: int = Field(default=10, ge=1, le=1000)


class ApproveRequest(_Body):
    comment: str | None = Field(default=None, max_length=_TEXT)


class CutoverRequest(_Body):
    force_window: bool = False
    comment: str | None = Field(default=None, max_length=_TEXT)


class ForceCutoverRequest(_Body):
    """A cutover before convergence needs a reason (SDD §5.4, ADV-16)."""

    reason: str = Field(min_length=10, max_length=_TEXT)


class RollbackRequest(_Body):
    reason: str = Field(min_length=1, max_length=_TEXT)


class CancelRequest(_Body):
    reason: str | None = Field(default=None, max_length=_TEXT)


class FinalizeRequest(_Body):
    delete_source: bool = False
    confirm: str = Field(max_length=_TEXT)


class StrategyRequest(_Body):
    strategy: Strategy


class PlanOut(Plan):
    """A plan as every plan route returns it (SDD §4.2): its derived status beside the lifecycle,
    and how many of its migrations are in each phase (§8.1, §11)."""

    derived_status: DerivedPlanStatus
    migration_counts: dict[Phase, int] = Field(default_factory=dict)


class PlanFinalizeRequest(_Body):
    """Finalize a wave, or the whole plan, with one approval (SDD §8.1, ADV-19)."""

    wave_id: str | None = Field(default=None, max_length=_TEXT)  # unknown: 404 (SDD §12)
    #: the wave's name, or the plan's without wave_id
    confirm: str = Field(max_length=_TEXT)
    delete_source: bool = False


class FinalizeSkip(BaseModel):
    migration_id: str
    reason: str


class PlanFinalizeResult(BaseModel):
    finalized: list[str]
    skipped: list[FinalizeSkip]


class DemoPowerRequest(_Body):
    """Power a fake source VM on or off (demo mode only, SDD §8.1)."""

    state: Literal["on", "off"]


class SimilarIncidentsRequest(_Body):
    query: str = Field(min_length=1, max_length=2000)
    limit: int = Field(default=5, ge=1, le=20)


class Hit(BaseModel):
    title: str
    content: str
    score: float | None = None


class SimilarIncidentsResponse(BaseModel):
    hits: list[Hit]


class RecommendationPage(BaseModel):
    items: list[AdvisorRecommendation]
    total: int
    offset: int
    limit: int


class AdvisorNoteEntry(AdvisorNote):
    """An advisor note with the migration it belongs to (SDD §12 ``GET /advisor/notes``)."""

    migration_id: str
    plan_id: str
    vm_name: str


class AdvisorNoteList(BaseModel):
    items: list[AdvisorNoteEntry]


class OrchestratorHealth(BaseModel):
    running: bool
    last_tick_age_s: float | None
    ticks: int
    healthy: bool


class Health(BaseModel):
    status: Literal["ok", "degraded"]
    version: str
    demo: bool
    #: a demo's time compression: its downtime clock runs in model time (SDD §5.2, ADV-16)
    demo_speed: float | None = None
    #: authentication is off: Sign in offers to continue without a token only then (SDD §12, §16)
    auth_disabled: bool
    db: Literal["ok", "error"]
    orchestrator: OrchestratorHealth


class Me(BaseModel):
    name: str
    role: Role


class CurrentUser(Me):
    """GET /me: the signed-in user, and what the dashboard needs to know of the installation."""

    #: SEAMLESS_ANSIBLE_HOST_KEY_CHECKING, for the provider page's warning while it is off - for a
    #: signed-in viewer alone, never on the public /health (SDD §12, §16, plan task ST7)
    host_key_checking: bool


class JevStatus(BaseModel):
    mode: str
    available: bool
    last_error: str | None = None
    #: where the model runs, and whether the console excerpt may go there (SDD §13.4, ADV-06)
    deployment: Literal["on_prem", "remote"] = "remote"
    endpoint_host: str | None = None
    console_sent: bool = False


class MemoryStatus(BaseModel):
    enabled: bool
    available: bool
    last_error: str | None = None


class AdvisorStatus(BaseModel):
    jev: JevStatus
    memory: MemoryStatus


class DataSentField(BaseModel):
    key: str
    label: str


class DataSentItem(BaseModel):
    """What one decision type sends to Jev, from the serializers' field specs (SDD §13.4)."""

    decision_type: DecisionType
    tool: str
    fields: list[DataSentField]
    sent: bool
    note: str | None = None


class DataSentList(BaseModel):
    items: list[DataSentItem]


class ErrorBody(BaseModel):
    code: str
    message: str


class ErrorEnvelope(BaseModel):
    error: ErrorBody


__all__ = [
    "AdvisorStatus",
    "ApproveRequest",
    "CancelRequest",
    "CutoverRequest",
    "ErrorEnvelope",
    "FinalizeRequest",
    "Health",
    "Hit",
    "Me",
    "PlanCreate",
    "RollbackRequest",
    "SimilarIncidentsRequest",
    "SimilarIncidentsResponse",
    "Stats",
    "StrategyRequest",
    "ThroughputPoint",
    "ValidationItem",
    "ValidationReport",
    "WavesAutoRequest",
]


class PolicyList(BaseModel):
    """GET /advisor/policies: the stored policies and the built-in default (SDD §12, §14.6)."""

    items: list[AdvisorPolicy]
    default: AdvisorPolicy


class OverrideRequest(BaseModel):
    """POST /advisor/recommendations/{id}/override (SDD §12, §14.6)."""

    model_config = ConfigDict(extra="forbid")

    option: Strategy
    reason: str | None = Field(default=None, max_length=_TEXT)
