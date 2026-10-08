"""Request/response schemas of the REST API (SDD §12) that are not domain models."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ..domain.enums import Role, Strategy
from ..domain.models import PlanCreate, ValidationItem, ValidationReport
from ..stats import Stats, ThroughputPoint


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


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
