"""Provider protocols (SDD §10)."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from ..domain.models import Provider, VMRef
from ..planning.preflight import DestinationInventory, SourceInventory


class ProviderError(Exception):
    """A provider could not be reached or answered with an error (message is credential-free)."""


class VMNotFound(ProviderError):
    """The cloud answered that it has no such VM (SDD §10): gone, never merely unreachable."""


@runtime_checkable
class SourceProvider(Protocol):
    async def check(self) -> dict[str, Any]: ...

    async def list_vms(self) -> list[VMRef]: ...

    async def get_vm(self, source_id: str) -> VMRef: ...

    async def inventory(self) -> SourceInventory: ...


@runtime_checkable
class DestinationProvider(Protocol):
    async def check(self) -> dict[str, Any]: ...

    async def inventory(self) -> DestinationInventory: ...

    async def get_server(self, server_id: str) -> dict[str, Any]:
        """``{status, ports: [{status, fixed_ips, floating_ips}]}``."""
        ...

    async def console_log(self, server_id: str, lines: int = 200) -> str | None: ...

    async def delete_server(self, server_id: str) -> None: ...

    async def find_server(self, name: str) -> str | None:
        """Id of the destination server called ``name`` (extension used by executors)."""
        ...

    async def ports(
        self,
        *,
        network_id: str | None = None,
        mac_address: str | None = None,
        ip_address: str | None = None,
    ) -> list[dict[str, Any]]:
        """The ports the credential sees that match every filter given:
        ``[{id, name, network_id, device_id, mac_address, fixed_ips: [address, ...]}]``."""
        ...

    async def delete_port(self, port_id: str) -> None:
        """Delete a port; one already gone is no error."""
        ...

    async def volumes(self) -> list[dict[str, Any]]:
        """The volumes of the credential's project: ``[{id, name, status, metadata: {key: value},
        attachments: [server_id, ...]}]`` (§7.2 VMware rollback)."""
        ...

    async def delete_volume(self, volume_id: str) -> None:
        """Delete a volume once Cinder settled it, and wait until it is gone, within the call's
        bound; one already gone is no error."""
        ...

    async def detach_volume(self, server_id: str, volume_id: str) -> None:
        """Remove Nova's attachment of the volume to the server and wait until Cinder reads the
        volume available, within the call's bound; one not attached to it is no error."""
        ...


def provider_caps(provider: Provider) -> dict[str, Any]:
    """Capabilities used by the selector: stored capabilities plus ``conversion_host``."""
    caps = dict(provider.capabilities)
    caps["conversion_host"] = provider.conversion_host is not None
    return caps


def microversion_text(value: Any) -> str:
    """A service's maximum microversion as ``major.minor`` text, ``""`` when there is none.

    keystoneauth hands openstacksdk's ``get_endpoint_data().max_microversion`` a parsed version
    tuple, ``(2, 96)``; ``str()`` of it is "(2, 96)", which nothing reads as a microversion (review
    RV18-01, RV18-02). Text, as a test double may hand it, is kept as it is (SDD §10).
    """
    if isinstance(value, (tuple, list)):
        parts = [part for part in value[:2] if isinstance(part, int) and not isinstance(part, bool)]
        return f"{parts[0]}.{parts[1]}" if len(parts) == 2 else ""
    return str(value or "")


def missing_dependency(extra: str, module: str) -> ProviderError:
    return ProviderError(
        f"the {module!r} Python package is not installed; install seamless-migrate[{extra}]"
    )
