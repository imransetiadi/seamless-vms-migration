"""Request/response schemas of the REST API (SDD §12) that are not domain models."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ..domain.enums import Role, Strategy
from ..domain.models import PlanCreate, ValidationItem, ValidationReport
from ..stats import Stats, ThroughputPoint


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


_SECRET = 4096  # longest accepted credential value


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


class ConversionKeyRequest(_Body):
    private_key: str = Field(min_length=64, max_length=16384, repr=False)


class WavesAutoRequest(_Body):
    max_wave_size: int = Field(default=10, ge=1, le=1000)


class ApproveRequest(_Body):
    comment: str | None = None


class CutoverRequest(_Body):
    force_window: bool = False
    comment: str | None = None


class RollbackRequest(_Body):
    reason: str = Field(min_length=1)


class CancelRequest(_Body):
    reason: str | None = None


class FinalizeRequest(_Body):
    delete_source: bool = False
    confirm: str


class StrategyRequest(_Body):
    strategy: Strategy


class SimilarIncidentsRequest(_Body):
    query: str = Field(min_length=1, max_length=2000)
    limit: int = Field(default=5, ge=1, le=20)


class Hit(BaseModel):
    title: str
    content: str
    score: float | None = None


class SimilarIncidentsResponse(BaseModel):
    hits: list[Hit]


class OrchestratorHealth(BaseModel):
    running: bool
    last_tick_age_s: float | None
    ticks: int
    healthy: bool


class Health(BaseModel):
    status: Literal["ok", "degraded"]
    version: str
    demo: bool
    db: Literal["ok", "error"]
    orchestrator: OrchestratorHealth


class Me(BaseModel):
    name: str
    role: Role


class JevStatus(BaseModel):
    mode: str
    available: bool
    last_error: str | None = None


class MemoryStatus(BaseModel):
    enabled: bool
    available: bool
    last_error: str | None = None


class AdvisorStatus(BaseModel):
    jev: JevStatus
    memory: MemoryStatus


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
