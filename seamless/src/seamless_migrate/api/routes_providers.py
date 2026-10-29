"""``/providers`` routes."""

from __future__ import annotations

import asyncio
import base64
import logging
import random
import re
from collections import Counter
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Body, Depends, Query, Request, Response
from pydantic import ValidationError

from ..credentials import inventory_project
from ..domain.enums import PlanStatus, ProviderKind, ProviderRole, Role
from ..domain.models import (
    BLANK_NAME,
    PROJECT_ID_RULE,
    REQUEST_BODY,
    ConversionHostConfig,
    Migration,
    Plan,
    ProjectCredential,
    Provider,
    ProviderCreate,
    is_blank,
    is_notify_secret_name,
    is_project_id,
    utcnow,
)
from ..events import emit
from ..orchestrator import RELEASES_VM, cleanup_pending
from ..providers.base import ProviderError
from ..security.auth import Principal
from ..security.secret_store import (
    SecretStoreError,
    ceph_key_secret_name,
    conversion_key_secret_name,
    credentials_secret_name,
    project_secret_name,
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
from .schemas import (
    CephKeyRequest,
    ConversionKeyRequest,
    EmptyRequest,
    ProjectCredentialsRequest,
    ProviderCredentialsRequest,
)

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
        "ceph",
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
    # notify-* names are the notification channels' (SDD §13.3, §24.3): a check would send a
    # channel's secret to this provider's endpoint, and writing the channel's would replace it
    channels = [field for field, name in names.items() if is_notify_secret_name(name)]
    if channels:
        raise ApiError(
            422,
            "validation_error",
            f"{', '.join(channels)}: notify-* names belong to the notification channels' secrets",
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
    # the server-owned fields are ignored: the pin is the provider check's alone, the project
    # credentials their routes' (SDD §10, §12, §27.4)
    provider = body.model_copy(
        update={
            **_SERVER_OWNED,
            "credentials_updated_at": None,
            "conversion_key_updated_at": None,
            "conversion_host_pin": None,
            "project_credentials": {},
        }
    )
    provider = _with_ceph_key_time(provider, None)
    # the store names a provider's secrets provider-{id}, provider-{id}-ssh, provider-{id}.ceph and
    # provider-{id}.project-<h>: an id that makes them another provider's would overwrite (and on
    # deletion remove) its key (SDD §13.3; the SEAMLESS_SECRET_ variables read `.` and `-` alike,
    # audit AP-04)
    async with svc.orchestrator.provider_lock():
        clash = next(
            (
                other.id
                for other in await svc.db.list("provider", Provider)
                if _secret_suffixed(provider.id, other.id)
                or _secret_suffixed(other.id, provider.id)
            ),
            None,
        )
        if clash is not None:
            raise ApiError(
                409,
                "conflict",
                f"provider id {provider.id!r} would share a secret with provider {clash!r}: "
                "Seamless stores a provider's secrets as provider-<id>, provider-<id>-ssh, "
                "provider-<id>.ceph and provider-<id>.project-<16 hex digits>",
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


def _secret_suffixed(longer: str, shorter: str) -> bool:
    """Whether provider id ``longer`` is ``shorter`` with an end that makes its credentials secret
    one of ``shorter``'s as the SEAMLESS_SECRET_ variables read it - `-ssh`, `-ceph`, or `-project-`
    and 16 hex digits - which read `.` and `-` alike (SDD §13.3, audit AP-04)."""
    end = longer[len(shorter) :] if longer.startswith(shorter) else ""
    return end in ("-ssh", "-ceph") or re.fullmatch(r"-project-[0-9a-f]{16}", end) is not None


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
        # key_updated_at is the key's write alone (SDD §4.2): a client's value is ignored
        updated = _with_ceph_key_time(
            updated, provider.ceph.key_updated_at if provider.ceph is not None else None
        )
        await svc.db.put("provider", updated, expected_version=version)
    if provider.ceph is not None and updated.ceph is None:
        # a key without an access to use it would stay in the store for good (SDD §13.3)
        await _delete_secret_quietly(svc, ceph_key_secret_name(provider_id))
    await emit(
        svc.store,
        svc.bus,
        "provider.updated",
        f"provider {provider_id} updated",
        actor=principal.name,
        data={"provider_id": provider_id, "fields": sorted(body), **_secret_names(updated)},
    )
    return updated


def _with_ceph_key_time(provider: Provider, when: Any) -> Provider:
    """The provider with ``ceph.key_updated_at`` set to ``when`` - the server's own value, never a
    client's (SDD §4.2) - or unchanged without a Ceph access."""
    if provider.ceph is None or provider.ceph.key_updated_at == when:
        return provider
    return provider.model_copy(
        update={"ceph": provider.ceph.model_copy(update={"key_updated_at": when})}
    )


async def _delete_secret_quietly(svc: Any, name: str) -> None:
    """A provider's Ceph key removed as best effort: a store that refuses is logged, never a
    failure of the request that no longer needs the secret (SDD §13.3). The line names neither
    the secret nor the provider, which the request's own event records (CodeQL py/log-injection,
    py/clear-text-logging-sensitive-data on a line built from the path's provider id)."""
    try:
        await asyncio.to_thread(secret_store(svc.settings).delete, name)
    except SecretStoreError as exc:
        log.warning("could not delete a provider's ceph key secret: %s", exc)


async def _store_secret(
    request: Request, name: str, values: dict[str, str], provider_id: str
) -> None:
    try:
        # a store that cannot be built (no ServiceAccount) is unavailable too (review RV52-X2)
        store = secret_store(services(request).settings)
        await asyncio.to_thread(store.write, name, values, {"seamless.io/provider": provider_id})
    except SecretStoreError as exc:
        raise ApiError(503, "unavailable", f"secret store: {exc}") from None


class _KeptChanging(Exception):
    """The provider changed under each of RECORD_SECRET_ATTEMPTS versioned writes in a row."""


async def _write_provider(
    svc: Any,
    provider_id: str,
    update: Callable[[Provider], dict[str, Any]],
    *,
    reset_check: bool = True,
) -> Provider:
    """A versioned write of what ``update`` derives from the provider's fresh copy - with the
    status and the check report reset unless ``reset_check`` is false -, retried when a check or
    another write changed the provider meanwhile (they touch other fields), so no concurrent change
    is lost (SDD §13.3). NotFound when the provider is gone; _KeptChanging after
    RECORD_SECRET_ATTEMPTS conflicts in a row."""
    # a conflict means another write landed, so each retry makes progress; giving up early would
    # leave a secret the store already holds unrecorded and unaudited (SDD §13.3)
    for attempt in range(RECORD_SECRET_ATTEMPTS):
        if attempt:
            await asyncio.sleep(min(0.5, 0.01 * 2**attempt) * random.uniform(0.5, 1.0))
        provider, version = await svc.db.get_versioned("provider", provider_id, Provider)
        reset = _SERVER_OWNED if reset_check else {}
        updated = provider.model_copy(update={**update(provider), **reset})
        try:
            await svc.db.put("provider", updated, expected_version=version)
        except ConflictError:
            continue
        return updated
    raise _KeptChanging


async def _record_secret(
    request: Request,
    provider_id: str,
    name: str,
    update: Callable[[Provider], dict[str, Any]],
    *,
    reset_check: bool = True,
) -> Provider:
    """The provider records a secret already in the store (SDD §13.3): a versioned write retried
    as ``_write_provider`` retries it, so a written secret is recorded, unless the provider keeps
    changing for RECORD_SECRET_ATTEMPTS writes (503 naming the secret); a provider deleted
    meanwhile answers 404 and the secret goes again."""
    svc = services(request)
    try:
        return await _write_provider(svc, provider_id, update, reset_check=reset_check)
    except NotFound:
        try:
            store = secret_store(svc.settings)
            await asyncio.to_thread(store.delete, name)
        except SecretStoreError as exc:
            # neither the secret's name nor the provider's id: both come from the request's path
            # (CodeQL py/log-injection, py/clear-text-logging-sensitive-data)
            log.warning("the secret of a provider deleted meanwhile could not be removed: %s", exc)
        raise
    except _KeptChanging:
        # SDD §13.3: the store holds the value; the operator sends it again once the provider
        # settles
        raise ApiError(
            503,
            "unavailable",
            f"secret {name} is stored, but provider {provider_id} changed "
            f"{RECORD_SECRET_ATTEMPTS} times while it was being recorded; send it again",
        ) from None


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


@router.put("/providers/{provider_id}/ceph-key", response_model=Provider)
async def set_ceph_key(
    provider_id: str,
    body: CephKeyRequest,
    request: Request,
    principal: Principal = Depends(require_role(Role.admin)),
) -> Provider:
    """Write-only Ceph key of the source cluster the provider declares (SDD §12, §13.3, §28.4):
    the secret ``provider-{id}.ceph``, key ``key``; the provider records the time alone."""
    svc = services(request)
    provider = await svc.db.get("provider", provider_id, Provider)  # 404 before any write
    if provider.ceph is None:
        raise ApiError(
            422, "validation_error", "provider has no ceph access: set `ceph` before its key"
        )
    key = body.key.strip()
    try:
        if any(c.isspace() for c in key) or len(base64.b64decode(key, validate=True)) < 16:
            raise ValueError
    except (ValueError, TypeError):
        raise ApiError(
            422, "validation_error", "key must be the base64 key `ceph auth get-key` prints"
        ) from None
    name = ceph_key_secret_name(provider_id)
    await _store_secret(request, name, {"key": key}, provider_id)

    def record(fresh: Provider) -> dict[str, Any]:
        if fresh.ceph is None:
            # the access went while the key was being recorded: the key goes with it
            raise _CephAccessGone
        return {"ceph": fresh.ceph.model_copy(update={"key_updated_at": utcnow()})}

    try:
        updated = await _record_secret(request, provider_id, name, record)
    except _CephAccessGone:
        await _delete_secret_quietly(svc, name)
        raise ApiError(
            409, "conflict", "the provider's ceph access was removed meanwhile; set it again"
        ) from None
    await emit(
        svc.store,
        svc.bus,
        "provider.credentials_updated",
        f"ceph key of provider {provider_id} updated",
        actor=principal.name,
        data={"provider_id": provider_id, "secret": name, "keys": ["key"]},
    )
    return updated


class _CephAccessGone(Exception):
    """The Ceph access was removed between the key's write and its record."""


def _check_project_id(project_id: str, name: str = "project_id") -> None:
    """A project credential's project id is 1-64 letters, digits, `-` or `_`, else a 422 naming
    ``name`` and never quoting the value (SDD §4.2, §12)."""
    if not is_project_id(project_id):
        raise ApiError(422, "validation_error", f"{name}: {PROJECT_ID_RULE}")


def _without_project_check(capabilities: dict[str, Any], project_id: str) -> dict[str, Any]:
    """The check report less the project's entry of its ``projects`` (SDD §12, §27.7), which
    tested a credential that is replaced or gone; the rest of the report stays."""
    projects = capabilities.get("projects")
    if not isinstance(projects, dict) or project_id not in projects:
        return capabilities
    return {**capabilities, "projects": {k: v for k, v in projects.items() if k != project_id}}


def _names_project(plan: Plan, provider_id: str, project_id: str) -> bool:
    """Whether ``plan`` acts in the project with the provider's credential for it: its
    ``source_project`` on its source provider, its ``destination_project`` on its destination
    (SDD §4.2, §12, §27.4); a plan stored before plan task TC4 names none."""
    return (plan.source_provider_id == provider_id and plan.source_project == project_id) or (
        plan.destination_provider_id == provider_id and plan.destination_project == project_id
    )


@router.put("/providers/{provider_id}/projects/{project_id}/credentials", response_model=Provider)
async def set_project_credentials(
    provider_id: str,
    project_id: str,
    body: ProjectCredentialsRequest,
    request: Request,
    principal: Principal = Depends(require_role(Role.admin)),
) -> Provider:
    """Write-only application credential of one Keystone project (SDD §12, §13.3, §27.4): the
    values go to the secret store as provider-{id}.project-<h>; the provider records the secret's
    name and the time under the project's id, and drops that project's entry of its check report
    alone. The write holds the lock of DELETE /providers/{id}, so a deletion never interleaves."""
    svc = services(request)
    values = body.values()
    blank = [key for key, value in values.items() if not value.strip()]
    if blank:
        raise ApiError(422, "validation_error", f"missing: {', '.join(blank)}")
    _check_project_id(project_id)
    name = project_secret_name(provider_id, project_id)
    async with svc.orchestrator.provider_lock():
        provider = await svc.db.get("provider", provider_id, Provider)  # 404 before any write
        if provider.kind == ProviderKind.vmware:
            raise ApiError(
                422,
                "validation_error",
                "a vmware provider takes no project credential: vCenter has no Keystone projects",
            )
        holder = next(
            (
                other
                for other, entry in provider.project_credentials.items()
                if entry.secret == name and other != project_id
            ),
            None,
        )
        if holder is not None:
            # equal 64-bit digests: the other project's secret is never written over (SDD §13.3)
            raise ApiError(
                409,
                "conflict",
                f"the credential of project {holder} holds the secret {name}, which project "
                f"{project_id} would be stored as",
            )
        await _store_secret(request, name, values, provider_id)
        updated = await _record_secret(
            request,
            provider_id,
            name,
            lambda fresh: {
                "project_credentials": {
                    **fresh.project_credentials,
                    project_id: ProjectCredential(secret=name, updated_at=utcnow()),
                },
                "capabilities": _without_project_check(fresh.capabilities, project_id),
            },
            reset_check=False,
        )
    await emit(
        svc.store,
        svc.bus,
        "provider.credentials_updated",
        f"credentials of project {project_id} of provider {provider_id} updated",
        actor=principal.name,
        data={
            "provider_id": provider_id,
            "project_id": project_id,
            "secret": name,
            "keys": sorted(values),
        },
    )
    return updated


@router.delete("/providers/{provider_id}/projects/{project_id}/credentials", status_code=204)
async def delete_project_credentials(
    provider_id: str,
    project_id: str,
    request: Request,
    principal: Principal = Depends(require_role(Role.admin)),
) -> Response:
    """The project's credential goes (SDD §12, §13.3, §27.4): 409 while a plan needs the project,
    judged as DELETE /providers/{id} judges a provider and under its lock, so no plan is saved
    naming the project meanwhile. The secret goes first - a store that cannot delete it answers 503
    and the entry stays, for the deletion to be sent again -, then the entry and that project's
    entry of the check report."""
    svc = services(request)
    _check_project_id(project_id)
    async with svc.orchestrator.provider_lock():
        provider = await svc.db.get("provider", provider_id, Provider)  # 404 when missing
        credential = provider.project_credentials.get(project_id)
        if credential is None:
            raise ApiError(
                404,
                "not_found",
                f"provider {provider_id} holds no credential for project {project_id}",
            )
        users = await _plans_that_need(svc, provider_id, project_id)
        if users:
            rest = len(users) - 10
            raise ApiError(
                409,
                "conflict",
                f"project {project_id} of provider {provider_id} is used by plan(s) that still "
                f"need it: {', '.join(users[:10])}{f' and {rest} more' if rest > 0 else ''}; a "
                "plan needs its projects until it completes and its VMs are finalized",
            )
        name = credential.secret
        try:
            store = secret_store(svc.settings)
            await asyncio.to_thread(store.delete, name)
        except SecretStoreError as exc:
            raise ApiError(503, "unavailable", f"secret store: {exc}") from None
        try:
            await _write_provider(
                svc,
                provider_id,
                lambda fresh: {
                    "project_credentials": {
                        k: v for k, v in fresh.project_credentials.items() if k != project_id
                    },
                    "capabilities": _without_project_check(fresh.capabilities, project_id),
                },
                reset_check=False,
            )
        except _KeptChanging:
            raise ApiError(
                503,
                "unavailable",
                f"secret {name} is deleted, but provider {provider_id} changed "
                f"{RECORD_SECRET_ATTEMPTS} times while its entry was being removed; send the "
                "deletion again",
            ) from None
    await emit(
        svc.store,
        svc.bus,
        "provider.credentials_updated",
        f"credentials of project {project_id} of provider {provider_id} deleted",
        actor=principal.name,
        data={
            "provider_id": provider_id,
            "project_id": project_id,
            "secret": name,
            "deleted": True,
        },
    )
    return Response(status_code=204)


async def _plans_that_need(svc: Any, provider_id: str, project_id: str | None = None) -> list[str]:
    """The plans that still need the provider (SDD §12), each with why: one that has not completed
    (a failed one can start again), or a completed one with a migration that holds its VM - it can
    be rolled back until it is finalized, and its rollback needs both providers (§8.1), or a
    rolled-back one that kept its copied data, which a cancel cleans up - and a completed or
    cancelled one with a cancelled migration whose cleanup is pending (§5.1). With ``project_id``,
    those of them that name the project on the provider's side alone (§27.4)."""
    plans = [
        p
        for p in await svc.db.list("plan", Plan)
        if provider_id in (p.source_provider_id, p.destination_provider_id)
        and (project_id is None or _names_project(p, provider_id, project_id))
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
        provider = await svc.db.get("provider", provider_id, Provider)  # 404 when missing
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
    for name in (
        credentials_secret_name(provider_id),
        conversion_key_secret_name(provider_id),
        ceph_key_secret_name(provider_id),
        # its project credentials' (SDD §12, §27.4)
        *sorted(entry.secret for entry in provider.project_credentials.values()),
    ):
        try:
            await asyncio.to_thread(secret_store(svc.settings).delete, name)
        except SecretStoreError as exc:
            left.append(name)
            # provider.deleted names the secret left (secret_left); the line names nothing the
            # request's path gave (CodeQL py/log-injection, py/clear-text-logging-sensitive-data)
            log.warning("a secret of a deleted provider could not be removed: %s", exc)
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
    provider_id: str,
    request: Request,
    project: str | None = Query(default=None),
    _: Principal = Depends(require_role(Role.viewer)),
) -> Any:
    """A source's VMs, or a destination's view, read with the provider's own credential; with
    ``project``, with that project's credential - the project credential the provider holds for
    it, on a connection of its own closed after the read, else its own credential for the project
    its last check names as its own -; 404 for any other project, nothing read (SDD §10, §12,
    §27.4, plan task TC4)."""
    svc = services(request)
    if project is not None:
        _check_project_id(project, "project")
    provider = await svc.db.get("provider", provider_id, Provider)
    try:
        # the credential the query names, as the one selection reads it (SDD §8, §27.5)
        reading = None if project is None else inventory_project(provider, project)
    except ProviderError as exc:
        raise ApiError(404, "not_found", str(exc)) from None
    if reading is None:
        return await _inventory_of(provider, svc.providers.get(provider))
    scoped = svc.providers.for_project(provider, reading)
    try:
        return await _inventory_of(provider, scoped)
    finally:
        scoped.close()


async def _inventory_of(provider: Provider, impl: Any) -> Any:
    """What ``impl`` reads of the provider's inventory, as the route answers it (SDD §12)."""
    if provider.role == ProviderRole.source:
        return [vm.model_dump(mode="json") for vm in await impl.list_vms()]
    return (await impl.inventory()).to_dict()
