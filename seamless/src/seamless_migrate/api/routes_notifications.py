"""``/notifications`` routes: the channel catalog an admin keeps and the subscriptions an operator
adds to a plan (SDD §12, §24.3, §24.4, plan task NT3)."""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Body, Depends, Query, Request, Response
from pydantic import Field, ValidationError

from ..domain.enums import NotifyTopic, Role
from ..domain.models import (
    DEFAULT_NOTIFY_TOPICS,
    HOOK_NAME,
    REQUEST_BODY,
    NotificationChannel,
    NotificationChannelView,
    NotificationCursor,
    NotificationSubscription,
    Plan,
    _Model,
    channel_view,
)
from ..events import emit
from ..security.auth import Principal
from ..security.secret_store import SecretStoreError, secret_store
from ..store import ConflictError, NotFound
from .deps import ApiError, _audit_denied, require_role, services, validation_message

log = logging.getLogger(__name__)

router = APIRouter(tags=["notifications"])

#: PATCH /notifications/channels/{name} (SDD §12): the name, the kind and the server-owned fields
#: are immutable
CHANNEL_EDITABLE_FIELDS = frozenset({"description", "recipients", "url", "redact_names", "enabled"})
#: a Slack incoming webhook's URL (SDD §24.3): the credential, kept in the secret alone
SLACK_WEBHOOK_PREFIX = "https://hooks.slack.com/services/"
#: a Standard Webhooks signing key (SDD §24.3): the prefix and the base64 of 24 to 64 bytes
SIGNING_KEY_PREFIX = "whsec_"
SIGNING_KEY_BYTES = (24, 64)
#: the secret's key per kind
SECRET_KEYS = {"slack": "url", "webhook": "signing_key"}


class SubscriptionCreate(_Model):
    """``POST /notifications/subscriptions``'s body (SDD §12, §24.4)."""

    channel: str = Field(pattern=HOOK_NAME)
    plan_id: str | None
    topics: list[NotifyTopic] = Field(
        default_factory=lambda: list(DEFAULT_NOTIFY_TOPICS), min_length=1
    )


def _channel(body: dict[str, Any]) -> NotificationChannel:
    """``body`` as a channel, a field it does not take refused at any depth (SDD §12, AP-01)."""
    try:
        return NotificationChannel.model_validate(body, context=REQUEST_BODY)
    except ValidationError as exc:
        raise ApiError(
            422, "validation_error", validation_message(exc.errors(), "invalid channel")
        ) from None


async def _view(request: Request, channel: NotificationChannel, principal: Principal) -> Any:
    """The channel as the principal's role reads it (SDD §24.3), with its cursor."""
    try:
        cursor = await services(request).db.get(
            "notification_cursor", channel.name, NotificationCursor
        )
    except NotFound:
        cursor = None
    return channel_view(channel, cursor, principal.role.at_least(Role.admin))


async def _emit_channel(
    request: Request, kind: str, channel: NotificationChannel, actor: str, **data: Any
) -> None:
    """A channel.* event (SDD §4.3, §24.7): the channel's name and kind - never a URL, a recipient
    or a secret's value."""
    svc = services(request)
    verb = kind.split(".", 1)[1]
    await emit(
        svc.store,
        svc.bus,
        kind,
        f"notification channel {channel.name} {verb}",
        actor=actor,
        data={"name": channel.name, "kind": channel.kind, **data},
    )


async def _emit_subscription(
    request: Request, kind: str, sub: NotificationSubscription, actor: str
) -> None:
    """A subscription.* event (SDD §4.3, §24.7): the subscription, its channel, its plan and its
    topics."""
    svc = services(request)
    verb = kind.split(".", 1)[1]
    where = "every plan" if sub.plan_id is None else f"plan {sub.plan_id}"
    await emit(
        svc.store,
        svc.bus,
        kind,
        f"subscription {sub.id} of channel {sub.channel} to {where} {verb}",
        plan_id=sub.plan_id,
        actor=actor,
        data={
            "id": sub.id,
            "channel": sub.channel,
            "plan_id": sub.plan_id,
            "topics": [str(topic) for topic in sub.topics],
        },
    )


async def _subscriptions_of(request: Request, channel: str) -> list[NotificationSubscription]:
    subs = await services(request).db.list("notification_subscription", NotificationSubscription)
    return sorted((s for s in subs if s.channel == channel), key=lambda s: s.id)


# ---------------------------------------------------------------------------- the catalog (§24.3)


@router.get("/notifications/channels", response_model=list[NotificationChannelView])
async def list_channels(
    request: Request, principal: Principal = Depends(require_role(Role.viewer))
) -> list[NotificationChannelView]:
    channels = await services(request).db.list("notification_channel", NotificationChannel)
    return [
        await _view(request, channel, principal)
        for channel in sorted(channels, key=lambda c: c.name)
    ]


@router.post("/notifications/channels", response_model=NotificationChannelView, status_code=201)
async def create_channel(
    request: Request,
    body: dict[str, Any] = Body(...),
    principal: Principal = Depends(require_role(Role.admin)),
) -> NotificationChannelView:
    svc = services(request)
    now = svc.orchestrator.now()
    channel = _channel(body).model_copy(update={"created_at": now, "updated_at": now})
    async with svc.orchestrator.provider_lock():
        try:
            await svc.db.put("notification_channel", channel, expected_version=0)
        except ConflictError:
            raise ApiError(409, "conflict", f"channel {channel.name!r} already exists") from None
    await _emit_channel(request, "channel.created", channel, principal.name)
    return await _view(request, channel, principal)


@router.get("/notifications/channels/{name}", response_model=NotificationChannelView)
async def get_channel(
    name: str, request: Request, principal: Principal = Depends(require_role(Role.viewer))
) -> NotificationChannelView:
    channel = await services(request).db.get("notification_channel", name, NotificationChannel)
    return await _view(request, channel, principal)


@router.patch("/notifications/channels/{name}", response_model=NotificationChannelView)
async def update_channel(
    name: str,
    request: Request,
    body: dict[str, Any] = Body(...),
    principal: Principal = Depends(require_role(Role.admin)),
) -> NotificationChannelView:
    svc = services(request)
    unknown = sorted(set(body) - CHANNEL_EDITABLE_FIELDS)
    if unknown:
        raise ApiError(422, "validation_error", f"fields cannot be changed: {', '.join(unknown)}")
    async with svc.orchestrator.provider_lock():
        channel, version = await svc.db.get_versioned(
            "notification_channel", name, NotificationChannel
        )
        merged = _channel({**channel.model_dump(mode="json"), **body})
        updated = merged.model_copy(
            update={"created_at": channel.created_at, "updated_at": svc.orchestrator.now()}
        )
        await svc.db.put("notification_channel", updated, expected_version=version)
    await _emit_channel(request, "channel.updated", updated, principal.name, fields=sorted(body))
    return await _view(request, updated, principal)


@router.delete("/notifications/channels/{name}", status_code=204)
async def delete_channel(
    name: str, request: Request, principal: Principal = Depends(require_role(Role.admin))
) -> Response:
    svc = services(request)
    # SDD §12: a subscription is added under the same lock, so none lands between the check and
    # the deletion
    async with svc.orchestrator.provider_lock():
        channel = await svc.db.get("notification_channel", name, NotificationChannel)  # 404
        subs = await _subscriptions_of(request, name)
        if subs:
            named = [
                f"{s.id} ({'every plan' if s.plan_id is None else f'plan {s.plan_id}'})"
                for s in subs
            ]
            raise ApiError(
                409,
                "conflict",
                f"channel {name} is named by subscription(s) {', '.join(named)}: remove them first",
            )
        await svc.db.delete("notification_channel", name)
        try:
            await svc.db.delete("notification_cursor", name)
        except NotFound:
            pass
    # the secret the store manages for the channel goes with it (SDD §13.3, §24.3), as a
    # provider's; the deletion is audited whatever the store answers, naming what it left
    left: dict[str, Any] = {}
    try:
        await asyncio.to_thread(secret_store(svc.settings).delete, channel.secret_name)
    except SecretStoreError as exc:
        left = {"secret_left": channel.secret_name}
        log.warning("a deleted notification channel's secret could not be removed: %s", exc)
    await _emit_channel(request, "channel.deleted", channel, principal.name, **left)
    return Response(status_code=204)


def _slack_url(value: Any) -> str:
    """A Slack incoming webhook's URL (SDD §24.3), else 422."""
    if not isinstance(value, str) or not value.startswith(SLACK_WEBHOOK_PREFIX):
        raise ApiError(
            422, "validation_error", f"url: a Slack incoming webhook URL, {SLACK_WEBHOOK_PREFIX}..."
        )
    try:
        parts = urlsplit(value)
    except ValueError:
        parts = None
    if (
        parts is None
        or parts.hostname != "hooks.slack.com"
        or parts.username is not None
        or parts.password is not None
        or any(ch.isspace() or ord(ch) < 32 for ch in value)
        or len(value) > 2048
    ):
        raise ApiError(
            422, "validation_error", "url: a Slack incoming webhook URL, as Slack gave it"
        )
    return value


def _signing_key(value: Any) -> str:
    """A Standard Webhooks signing key (SDD §24.3): ``whsec_`` and the base64 of 24 to 64 bytes."""
    why = f"signing_key: {SIGNING_KEY_PREFIX} and the base64 of 24 to 64 random bytes"
    if not isinstance(value, str) or not value.startswith(SIGNING_KEY_PREFIX):
        raise ApiError(422, "validation_error", why)
    try:
        raw = base64.b64decode(value[len(SIGNING_KEY_PREFIX) :], validate=True)
    except (binascii.Error, ValueError):
        raise ApiError(422, "validation_error", why) from None
    low, high = SIGNING_KEY_BYTES
    if not low <= len(raw) <= high:
        raise ApiError(422, "validation_error", why)
    return value


@router.put("/notifications/channels/{name}/secret", response_model=NotificationChannelView)
async def set_channel_secret(
    name: str,
    request: Request,
    body: dict[str, Any] = Body(...),
    principal: Principal = Depends(require_role(Role.admin)),
) -> NotificationChannelView:
    """Write-only (SDD §24.3): the value reaches the secret store under notify-{name} and no
    route returns it."""
    svc = services(request)
    channel = await svc.db.get("notification_channel", name, NotificationChannel)  # 404
    key = SECRET_KEYS.get(channel.kind)
    if key is None:
        raise ApiError(
            422,
            "validation_error",
            "an email channel has no secret: the relay's credential is the installation's "
            "(SEAMLESS_SMTP_SECRET, SDD §24.3)",
        )
    if set(body) != {key}:
        raise ApiError(
            422, "validation_error", f'a {channel.kind} channel\'s secret is {{"{key}"}} alone'
        )
    value = _slack_url(body[key]) if key == "url" else _signing_key(body[key])
    try:
        store = secret_store(svc.settings)
        await asyncio.to_thread(
            store.write, channel.secret_name, {key: value}, {"seamless.io/channel": name}
        )
    except SecretStoreError as exc:
        raise ApiError(503, "unavailable", f"secret store: {exc}") from None
    await _emit_channel(request, "channel.updated", channel, principal.name, fields=["secret"])
    return await _view(request, channel, principal)


# -------------------------------------------------------------------- the subscriptions (§24.4)


@router.get("/notifications/subscriptions", response_model=list[NotificationSubscription])
async def list_subscriptions(
    request: Request,
    plan_id: str | None = Query(default=None),
    channel: str | None = Query(default=None),
    _: Principal = Depends(require_role(Role.viewer)),
) -> list[NotificationSubscription]:
    subs = await services(request).db.list("notification_subscription", NotificationSubscription)
    return sorted(
        (
            s
            for s in subs
            if (plan_id is None or s.plan_id == plan_id)
            and (channel is None or s.channel == channel)
        ),
        key=lambda s: s.id,
    )


async def _every_plan_is_an_admins(request: Request, principal: Principal) -> None:
    """A subscription to every plan is an admin's (SDD §13.2, §24.4): 403 and an auth.denied."""
    if principal.role.at_least(Role.admin):
        return
    reason = f"a subscription to every plan requires the {Role.admin} role"
    await _audit_denied(request, reason, Role.admin, principal)
    raise ApiError(403, "forbidden", reason)


@router.post(
    "/notifications/subscriptions", response_model=NotificationSubscription, status_code=201
)
async def create_subscription(
    request: Request,
    body: dict[str, Any] = Body(...),
    principal: Principal = Depends(require_role(Role.operator)),
) -> NotificationSubscription:
    svc = services(request)
    try:
        asked = SubscriptionCreate.model_validate(body, context=REQUEST_BODY)
    except ValidationError as exc:
        raise ApiError(
            422, "validation_error", validation_message(exc.errors(), "invalid subscription")
        ) from None
    if asked.plan_id is None:
        await _every_plan_is_an_admins(request, principal)
    sub = NotificationSubscription(
        channel=asked.channel,
        plan_id=asked.plan_id,
        topics=asked.topics,
        created_by=principal.name,
        created_at=svc.orchestrator.now(),
    )
    # SDD §12: the channel is deleted under the same lock, so a subscription never names a
    # channel that went meanwhile
    async with svc.orchestrator.provider_lock():
        try:
            await svc.db.get("notification_channel", asked.channel, NotificationChannel)
        except NotFound:
            raise ApiError(
                422, "validation_error", f"channel: no channel {asked.channel!r} in the catalog"
            ) from None
        if asked.plan_id is not None:
            try:
                await svc.db.get("plan", asked.plan_id, Plan)
            except NotFound:
                raise ApiError(
                    422, "validation_error", f"plan_id: no plan {asked.plan_id!r}"
                ) from None
        for other in await _subscriptions_of(request, asked.channel):
            if other.plan_id == asked.plan_id:
                where = "every plan" if asked.plan_id is None else f"plan {asked.plan_id}"
                raise ApiError(
                    409,
                    "conflict",
                    f"channel {asked.channel} already has subscription {other.id} to {where}",
                )
        await svc.db.put("notification_subscription", sub, expected_version=0)
    await _emit_subscription(request, "subscription.created", sub, principal.name)
    return sub


@router.delete("/notifications/subscriptions/{sub_id}", status_code=204)
async def delete_subscription(
    sub_id: str, request: Request, principal: Principal = Depends(require_role(Role.operator))
) -> Response:
    svc = services(request)
    sub = await svc.db.get("notification_subscription", sub_id, NotificationSubscription)  # 404
    if sub.plan_id is None:
        await _every_plan_is_an_admins(request, principal)
    await svc.db.delete("notification_subscription", sub_id)
    await _emit_subscription(request, "subscription.deleted", sub, principal.name)
    return Response(status_code=204)
