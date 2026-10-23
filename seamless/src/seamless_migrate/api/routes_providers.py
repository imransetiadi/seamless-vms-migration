"""``/providers`` routes."""

from __future__ import annotations

import asyncio
import logging
import random
import re
from collections import Counter
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Body, Depends, Request, Response
from pydantic import ValidationError

from ..domain.enums import PlanStatus, ProviderKind, ProviderRole, Role
from ..domain.models import (
    BLANK_NAME,
    REQUEST_BODY,
    ConversionHostConfig,
    Migration,
    Plan,
    Provider,
    ProviderCreate,
    is_blank,
    utcnow,
)
from ..events import emit
from ..orchestrator import RELEASES_VM, cleanup_pending
from ..security.auth import Principal
from ..security.secret_store import (
    SecretStoreError,
    conversion_key_secret_name,
    credentials_secret_name,
    secret_store,
)
from ..security.secrets import valid_secret_name
from ..store import ConflictError, NotFound
from .deps import (
    ApiError,
    refuse_unknown_fields,
    require_role,
    services,
    validation_message,
)
from .schemas import ConversionKeyRequest, EmptyRequest, ProviderCredentialsRequest

log = logging.getLogger(__name__)
router = APIRouter(tags=["providers"])
#: plans that can release their providers: a completed one, once none of its migrations holds its
#: VM, and a cancelled one — a failed plan can be started again (SDD §8, §12)
TERMINAL_PLAN_STATUSES = frozenset({PlanStatus.completed, PlanStatus.cancelled})
#: plans whose provider must not change under them (PATCH answers 409)
ACTIVE_PLAN_STATUSES = frozenset({PlanStatus.running, PlanStatus.paused})
#: PATCH /providers/{id} (SDD §12); id, kind, role and server-owned fields are immutable
PROVIDER_EDITABLE_FIELDS = frozenset(
    {
        "name",
        "endpoint",
        "cloud",
        "credentials_secret",
        "region",
        "verify_tls",
        "ca_cert_path",
        "conversion_host",
        "distribution",
        "release",
        "policy_profile",
        "gpu_profiles",
        "announces_tenant_networks",
    }
)
_SERVER_OWNED = {
    "status": "unknown",
    "status_message": None,
    "last_checked_at": None,
    "capabilities": {},
}
#: versioned writes of a secret's record before the route gives up with 503 (SDD §13.3): each
#: conflict is a check or another write that landed, so this many in a row is a hammered provider
RECORD_SECRET_ATTEMPTS = 50


async def _plans_using(request: Request, provider_id: str, statuses: frozenset[str]) -> list[str]:
    return [
        p.id
        for p in await services(request).db.list("plan", Plan)
        if p.status in statuses and provider_id in (p.source_provider_id, p.destination_provider_id)
    ]


def _credential_values(provider: Provider, body: ProviderCredentialsRequest) -> dict[str, str]:
    """The keys to store for ``provider`` (SDD §13.3), or a 422 naming what is missing."""
    values = body.values()
    if provider.kind == ProviderKind.vmware:
        allowed = {"username", "password", "datacenter"}
        required = ("username", "password")
    else:
        allowed = set(ProviderCredentialsRequest.model_fields) - {"datacenter"}
        app = bool(
            values.get("application_credential_id") or values.get("application_credential_secret")
        )
        required = (
            ("application_credential_id", "application_credential_secret")
            if app
            else ("username", "password", "project_name")
        )
    extra = sorted(set(values) - allowed)
    if extra:
        raise ApiError(
            422, "validation_error", f"not used by a {provider.kind} provider: {', '.join(extra)}"
        )
    missing = [k for k in required if not values.get(k)]
    if missing:
        raise ApiError(422, "validation_error", f"missing: {', '.join(missing)}")
    return values


@router.get("/providers", response_model=list[Provider])
async def list_providers(
    request: Request, _: Principal = Depends(require_role(Role.viewer))
) -> list[Provider]:
    return await services(request).db.list("provider", Provider)


def _check_secret_names(provider: Provider, fields: set[str]) -> None:
    """SDD §13.3: a secret named for a provider must be one ``security.secrets`` can read; only
    the fields the request sets are checked, so a provider stored before stays editable."""
    names = {}
    if "credentials_secret" in fields:
        names["credentials_secret"] = provider.credentials_secret
    if "conversion_host" in fields and provider.conversion_host is not None:
        names["conversion_host.ssh_key_secret"] = provider.conversion_host.ssh_key_secret
    bad = [
        field for field, name in names.items() if name is not None and not valid_secret_name(name)
    ]
    if bad:
        raise ApiError(
            422,
            "validation_error",
            f"{', '.join(bad)}: a secret name is 1-128 letters, digits, '.', '_' or '-', "
            "starting with a letter or digit",
        )
    # provider-* names are the ones the store writes for each provider: naming another provider's
    # would let a check send its credentials to this provider's endpoint (SDD §13.3)
    own = {
        "credentials_secret": credentials_secret_name(provider.id),
        "conversion_host.ssh_key_secret": conversion_key_secret_name(provider.id),
    }
    # compared as the readers fold the name: a case-insensitive filesystem opens provider-x for
    # Provider-x, the SEAMLESS_SECRET_ variables upper-case it and take any other character as _
    # (audit AP-04) - only the provider's own exact name passes
    taken = [
        field
        for field, name in names.items()
        if name is not None
        and re.sub(r"[^a-z0-9]", "-", name.lower()).startswith("provider-")
        and name != own[field]
    ]
    if taken:
        raise ApiError(
            422,
            "validation_error",
            f"{', '.join(taken)}: provider-* names belong to the secrets Seamless stores for each "
            "provider; this one is another provider's",
        )


def _secret_names(provider: Provider) -> dict[str, str]:
    """The secrets a provider reads, by name, for its audit events (never a value; SDD §13.3)."""
    names = {}
    if provider.credentials_secret:
        names["credentials_secret"] = provider.credentials_secret
    if provider.conversion_host is not None and provider.conversion_host.ssh_key_secret:
        names["ssh_key_secret"] = provider.conversion_host.ssh_key_secret
    return names


@router.post("/providers", response_model=Provider, status_code=201)
async def create_provider(
    body: ProviderCreate,
    request: Request,
    principal: Principal = Depends(require_role(Role.admin)),
) -> Provider:
    svc = services(request)
    await refuse_unknown_fields(request, ProviderCreate)
    _check_secret_names(body, {"credentials_secret", "conversion_host"})
    # the server-owned fields are ignored: the pin is the provider check's alone (SDD §10, §12)
    provider = body.model_copy(
        update={
            **_SERVER_OWNED,
            "credentials_updated_at": None,
            "conversion_key_updated_at": None,
            "conversion_host_pin": None,
        }
    )
    # the store names a provider's secrets provider-{id} and provider-{id}-ssh: an id that makes
    # them another provider's would overwrite (and on deletion remove) its key (SDD §13.3)
    async with svc.orchestrator.provider_lock():
        clash = next(
            (
                other.id
                for other in await svc.db.list("provider", Provider)
                if provider.id == f"{other.id}-ssh" or other.id == f"{provider.id}-ssh"
            ),
            None,
        )
        if clash is not None:
            raise ApiError(
                409,
                "conflict",
                f"provider id {provider.id!r} would share a secret with provider {clash!r}: "
                "Seamless stores a provider's secrets as provider-<id> and provider-<id>-ssh",
            )
        try:
            await svc.db.put("provider", provider, expected_version=0)
        except ConflictError:
            raise ApiError(409, "conflict", f"provider {provider.id!r} already exists") from None
    await emit(
        svc.store,
        svc.bus,
        "provider.created",
        f"provider {provider.id} created",
        actor=principal.name,
        data={
            "provider_id": provider.id,
            "kind": str(provider.kind),
            "role": str(provider.role),
            **_secret_names(provider),
        },
    )
    return provider


@router.get("/providers/{provider_id}", response_model=Provider)
async def get_provider(
    provider_id: str, request: Request, _: Principal = Depends(require_role(Role.viewer))
) -> Provider:
    return await services(request).db.get("provider", provider_id, Provider)


@router.patch("/providers/{provider_id}", response_model=Provider)
async def update_provider(
    provider_id: str,
    request: Request,
    body: dict[str, Any] = Body(...),
    principal: Principal = Depends(require_role(Role.admin)),
) -> Provider:
    svc = services(request)
    unknown = sorted(set(body) - PROVIDER_EDITABLE_FIELDS)
    if unknown:
        raise ApiError(422, "validation_error", f"fields cannot be changed: {', '.join(unknown)}")
    if isinstance(body.get("name"), str) and is_blank(body["name"]):
        # at the request boundary: a provider stored blank before keeps loading (RV20-11)
        raise ApiError(422, "validation_error", f"name: {BLANK_NAME}")
    # the check and the write hold the lock a plan start takes for its status write, so no start
    # lands between them and leaves a running plan on a changed provider (SDD §12, audit AP-07)
    async with svc.orchestrator.provider_lock():
        provider, version = await svc.db.get_versioned("provider", provider_id, Provider)
        users = await _plans_using(request, provider_id, ACTIVE_PLAN_STATUSES)
        if users:
            raise ApiError(
                409,
                "conflict",
                f"provider is used by running or paused plan(s): {', '.join(users)}",
            )
        try:
            updated = Provider.model_validate(
                {**provider.model_dump(mode="json"), **body, **_SERVER_OWNED},
                context=REQUEST_BODY,  # a nested field unknown is refused too (audit AP-01)
            )
        except ValidationError as exc:
            raise ApiError(
                422, "validation_error", validation_message(exc.errors(), "invalid provider")
            ) from None
        _check_secret_names(updated, set(body))
        await svc.db.put("provider", updated, expected_version=version)
    await emit(
        svc.store,
        svc.bus,
        "provider.updated",
        f"provider {provider_id} updated",
        actor=principal.name,
        data={"provider_id": provider_id, "fields": sorted(body), **_secret_names(updated)},
    )
    return updated


async def _store_secret(
    request: Request, name: str, values: dict[str, str], provider_id: str
) -> None:
    try:
        # a store that cannot be built (no ServiceAccount) is unavailable too (review RV52-X2)
        store = secret_store(services(request).settings)
        await asyncio.to_thread(store.write, name, values, {"seamless.io/provider": provider_id})
    except SecretStoreError as exc:
        raise ApiError(503, "unavailable", f"secret store: {exc}") from None


async def _record_secret(
    request: Request, provider_id: str, name: str, update: Callable[[Provider], dict[str, Any]]
) -> Provider:
    """The provider records a secret already in the store (SDD §13.3): a versioned write, retried
    when a check or another write changed the provider meanwhile (they touch other fields) — the
    update is derived from the fresh copy each time, so no concurrent change is lost — and so a
    written secret is recorded, unless the provider keeps changing for RECORD_SECRET_ATTEMPTS
    writes (503 naming the secret); a provider deleted meanwhile answers 404 and the secret
    goes again."""
    svc = services(request)
    # a conflict means another write landed, so each retry makes progress; giving up early would
    # leave a secret the store already holds unrecorded and unaudited (SDD §13.3)
    for attempt in range(RECORD_SECRET_ATTEMPTS):
        if attempt:
            await asyncio.sleep(min(0.5, 0.01 * 2**attempt) * random.uniform(0.5, 1.0))
        try:
            provider, version = await svc.db.get_versioned("provider", provider_id, Provider)
        except NotFound:
            try:
                store = secret_store(svc.settings)
                await asyncio.to_thread(store.delete, name)
            except SecretStoreError:
                log.warning("secret %s of a deleted provider could not be removed", name)
            raise
        updated = provider.model_copy(update={**update(provider), **_SERVER_OWNED})
        try:
            await svc.db.put("provider", updated, expected_version=version)
        except ConflictError:
            continue
        return updated
    # SDD §13.3: the store holds the value; the operator sends it again once the provider settles
    raise ApiError(
        503,
        "unavailable",
        f"secret {name} is stored, but provider {provider_id} changed {RECORD_SECRET_ATTEMPTS} "
        "times while it was being recorded; send it again",
    )


@router.put("/providers/{provider_id}/credentials", response_model=Provider)
async def set_provider_credentials(
    provider_id: str,
    body: ProviderCredentialsRequest,
    request: Request,
    principal: Principal = Depends(require_role(Role.admin)),
) -> Provider:
    """Write-only (SDD §12/§13.3): the values go to the secret store, never to the database."""
    svc = services(request)
    provider = await svc.db.get("provider", provider_id, Provider)
    values = _credential_values(provider, body)
    name = credentials_secret_name(provider_id)
    await _store_secret(request, name, values, provider_id)
    updated = await _record_secret(
        request,
        provider_id,
        name,
        lambda _: {"credentials_secret": name, "credentials_updated_at": utcnow()},
    )
    await emit(
        svc.store,
        svc.bus,
        "provider.credentials_updated",
        f"credentials of provider {provider_id} updated",
        actor=principal.name,
        data={"provider_id": provider_id, "secret": name, "keys": sorted(values)},
    )
    return updated


@router.put("/providers/{provider_id}/conversion-key", response_model=Provider)
async def set_conversion_key(
    provider_id: str,
    body: ConversionKeyRequest,
    request: Request,
    principal: Principal = Depends(require_role(Role.admin)),
) -> Provider:
    """Write-only SSH private key of an existing conversion host (SDD §12/§13.3)."""
    svc = services(request)
    await svc.db.get("provider", provider_id, Provider)  # 404 before anything is written
    key = body.private_key.strip()
    if "PRIVATE KEY-----" not in key or not key.startswith("-----BEGIN"):
        raise ApiError(422, "validation_error", "private_key must be an OpenSSH or PEM private key")
    name = conversion_key_secret_name(provider_id)
    await _store_secret(request, name, {"private_key": key + "\n"}, provider_id)
    updated = await _record_secret(
        request,
        provider_id,
        name,
        lambda fresh: {
            "conversion_host": (fresh.conversion_host or ConversionHostConfig()).model_copy(
                update={"ssh_key_secret": name}
            ),
            "conversion_key_updated_at": utcnow(),
        },
    )
    await emit(
        svc.store,
        svc.bus,
        "provider.credentials_updated",
        f"conversion-host key of provider {provider_id} updated",
        actor=principal.name,
        data={"provider_id": provider_id, "secret": name, "keys": ["private_key"]},
    )
    return updated


async def _plans_that_need(svc: Any, provider_id: str) -> list[str]:
    """The plans that still need the provider (SDD §12), each with why: one that has not completed
    (a failed one can start again), or a completed one with a migration that holds its VM - it can
    be rolled back until it is finalized, and its rollback needs both providers (§8.1), or a
    rolled-back one that kept its copied data, which a cancel cleans up - and a completed or
    cancelled one with a cancelled migration whose cleanup is pending (§5.1)."""
    plans = [
        p
        for p in await svc.db.list("plan", Plan)
        if provider_id in (p.source_provider_id, p.destination_provider_id)
    ]
    completed = [p.id for p in plans if p.status in TERMINAL_PLAN_STATUSES]
    holding: Counter[str] = Counter()
    keeping: Counter[str] = Counter()
    cleaning: Counter[str] = Counter()
    if completed:
        for m in await svc.db.list("migration", Migration, plan_id=completed):
            if m.phase not in RELEASES_VM:
                holding[m.plan_id] += 1
            elif await svc.orchestrator.keeps_copied_data(m):
                keeping[m.plan_id] += 1
            elif cleanup_pending(m):
                cleaning[m.plan_id] += 1
    users = []
    for p in plans:
        if p.status not in TERMINAL_PLAN_STATUSES:
            users.append(f'{p.id} ("{p.name}", {p.status})')
            continue
        why: list[str] = []
        if n := holding[p.id]:
            why.append(f"{n} VM{'' if n == 1 else 's'} not finalized")
        if n := keeping[p.id]:
            why.append(
                f"{n} rolled-back VM{' keeping its' if n == 1 else 's keeping their'} copied data"
            )
        if n := cleaning[p.id]:
            why.append(f"{n} cancelled VM{'' if n == 1 else 's'} still being cleaned up")
        if why:
            users.append(f'{p.id} ("{p.name}", {", ".join(why)})')
    return users


@router.delete("/providers/{provider_id}", status_code=204)
async def delete_provider(
    provider_id: str, request: Request, principal: Principal = Depends(require_role(Role.admin))
) -> Response:
    svc = services(request)
    # SDD §12: plan creations, edits and retries check their providers under the same lock
    async with svc.orchestrator.provider_lock():
        await svc.db.get("provider", provider_id, Provider)  # 404 when missing
        users = await _plans_that_need(svc, provider_id)
        if users:
            rest = len(users) - 10
            raise ApiError(
                409,
                "conflict",
                f"provider is used by plan(s) that still need it: {', '.join(users[:10])}"
                f"{f' and {rest} more' if rest > 0 else ''}; a plan needs its providers until it "
                "completes and its VMs are finalized",
            )
        await svc.db.delete("provider", provider_id)
    forget = getattr(svc.providers, "forget", None)
    if forget is not None:
        forget(provider_id)
    # the secrets the store manages for this provider go with it (SDD §13.3); each one is tried,
    # and the deletion is audited whatever the store answers, naming what it left (audit AA-03) -
    # a store that cannot be built leaves both (review RV52-X2)
    left: list[str] = []
    for name in (credentials_secret_name(provider_id), conversion_key_secret_name(provider_id)):
        try:
            await asyncio.to_thread(secret_store(svc.settings).delete, name)
        except SecretStoreError as exc:
            left.append(name)
            log.warning("could not delete secret %s of provider %s: %s", name, provider_id, exc)
    await emit(
        svc.store,
        svc.bus,
        "provider.deleted",
        f"provider {provider_id} deleted",
        actor=principal.name,
        data={"provider_id": provider_id, **({"secrets_left": left} if left else {})},
    )
    return Response(status_code=204)


@router.post("/providers/{provider_id}/check", response_model=Provider)
async def check_provider(
    provider_id: str,
    request: Request,
    body: EmptyRequest | None = None,
    principal: Principal = Depends(require_role(Role.operator)),
) -> Provider:
    return await services(request).orchestrator.check_provider(provider_id, principal.name)


@router.get("/providers/{provider_id}/inventory")
async def provider_inventory(
    provider_id: str, request: Request, _: Principal = Depends(require_role(Role.viewer))
) -> Any:
    svc = services(request)
    provider = await svc.db.get("provider", provider_id, Provider)
    impl = svc.providers.get(provider)
    if provider.role == ProviderRole.source:
        return [vm.model_dump(mode="json") for vm in await impl.list_vms()]
    return (await impl.inventory()).to_dict()
