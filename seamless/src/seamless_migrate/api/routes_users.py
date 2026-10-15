"""Admin-issued dashboard access alongside the read-only bootstrap token file."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..domain.enums import Role
from ..security.auth import (
    MAX_TOKEN_NAME,
    RESERVED_TOKEN_NAMES,
    DashboardUser,
    Principal,
    generate_token,
    hash_token,
)
from .deps import ApiError, require_role, services
from .schemas import Me

router = APIRouter(tags=["users"])


class UserCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(
        min_length=1, max_length=MAX_TOKEN_NAME, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.@-]*$"
    )
    role: Role = Role.viewer

    @field_validator("name")
    @classmethod
    def available_actor(cls, value: str) -> str:
        if value.casefold() in RESERVED_TOKEN_NAMES:
            raise ValueError("this name is reserved for the control plane")
        return value


class UserCreated(Me):
    token: str = Field(repr=False)


@router.get("/users", response_model=list[Me])
async def list_users(
    request: Request,
    response: Response,
    _: Principal = Depends(require_role(Role.admin)),
) -> list[Me]:
    svc = services(request)
    response.headers["Cache-Control"] = "no-store"
    entries = [*svc.tokens.entries, *await svc.db.list("user", DashboardUser)]
    return [Me(name=entry.name, role=entry.role) for entry in entries]


@router.post("/users", response_model=UserCreated, status_code=201)
async def create_user(
    body: UserCreate,
    request: Request,
    response: Response,
    principal: Principal = Depends(require_role(Role.admin)),
) -> UserCreated:
    svc = services(request)
    if svc.settings.auth_disabled:
        raise ApiError(409, "conflict", "enable authentication before adding dashboard users")
    if any(entry.name.strip().casefold() == body.name.casefold() for entry in svc.tokens.entries):
        raise ApiError(409, "conflict", "a user with this name already exists")
    token = generate_token()
    user = DashboardUser(
        id=body.name.casefold(),
        name=body.name,
        role=body.role,
        sha256=hash_token(token),
    )
    async with svc.bus.emit_lock:
        event = await asyncio.to_thread(svc.store.create_user, user, principal.name)
        svc.bus.publish(event, persist=True)
    response.headers["Cache-Control"] = "no-store"
    return UserCreated(name=user.name, role=user.role, token=token)
