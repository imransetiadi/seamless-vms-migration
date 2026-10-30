"""Provider protocols (SDD §10)."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from ..domain.models import GpuDevice, Provider, VMRef
from ..planning.preflight import DestinationInventory, GpuCapacity, SourceInventory


class ProviderError(Exception):
    """A provider could not be reached or answered with an error (message is credential-free)."""


class VMNotFound(ProviderError):
    """The cloud answered that it has no such VM (SDD §10): gone, never merely unreachable."""


class ProviderConflict(ProviderError):
    """A change the service refused while another is under way (HTTP 409): Octavia's load balancer
    in PENDING_* (SDD §8.5, §10), which the change may be sent again after."""


class ProviderUnreachable(ProviderError):
    """A sign-in Keystone could not answer (SDD §7.1, §10; the owner's decision of 2026-10-30): a
    connection refused or reset, a name that does not resolve, a TLS handshake that failed, a
    timeout, the call's bound, or a 5xx answer - the executors' transient sign-in failure, where a
    credential Keystone rejects stays a plain ProviderError."""


@runtime_checkable
class SourceProvider(Protocol):
    async def check(self) -> dict[str, Any]:
        """The source's capabilities; ProviderError when it cannot be reached."""

    async def list_vms(self) -> list[VMRef]:
        """Every VM of the source the credential sees."""

    async def get_vm(self, source_id: str) -> VMRef:
        """The VM as the source reads it now; VMNotFound when the cloud answers it has none."""

    async def inventory(self) -> SourceInventory:
        """The source's networks (name to MTU), projects and keypairs."""


@runtime_checkable
class DestinationProvider(Protocol):
    async def check(self) -> dict[str, Any]:
        """The destination's capabilities; ProviderError when it cannot be reached."""

    async def inventory(self) -> DestinationInventory:
        """The destination's networks, flavors, volume types, quotas, projects and keypairs, its
        GPU flavors and their Placement pools (SDD §22.3)."""

    async def get_server(self, server_id: str) -> dict[str, Any]:
        """``{status, ports: [{status, fixed_ips, floating_ips}], flavor: {name, extra_specs}}`` -
        compute 2.47's embedded flavor, its extra specs empty where Nova's policy hides them."""

    async def gpu_capacity(self, requests: list[GpuDevice]) -> GpuCapacity:
        """The Placement pools of the given vGPU requests only, read as the inventory's are: the
        cutover gate's read (SDD §22.3, §22.7)."""

    async def gpu_flavors(self) -> list[dict[str, Any]]:
        """The GPU flavors as ``DestinationInventory.gpu_flavors`` lists them - ``[{name, vcpus,
        ram_mb, disk_gb, gpus}]``, the declared profiles applied -, read with one flavor list:
        where the cutover gate reads a GPU flavor's requests (SDD §10, §22.3, §22.7)."""

    async def server_allocations(self, server_id: str) -> dict[str, int] | None:
        """The units the server holds per resource class, summed over its providers; None when
        Placement refuses the read or holds no allocation for it (SDD §22.3, §22.8)."""

    async def console_log(self, server_id: str, lines: int | None = 200) -> str | None:
        """The last ``lines`` lines of the server's console, all of it for None - a host key
        pin's read, never logged (SDD §10, §26.3); None when it has none."""

    async def delete_server(self, server_id: str) -> None:
        """Delete the server and wait, within the call's bound, until it is gone."""

    async def find_server(self, name: str) -> str | None:
        """Id of the destination server called ``name`` (extension used by executors)."""

    async def ports(
        self,
        *,
        network_id: str | None = None,
        mac_address: str | None = None,
        ip_address: str | None = None,
    ) -> list[dict[str, Any]]:
        """The ports the credential sees that match every filter given:
        ``[{id, name, network_id, device_id, mac_address, fixed_ips: [address, ...]}]``."""

    async def delete_port(self, port_id: str) -> None:
        """Delete a port; one already gone is no error."""

    async def volumes(self) -> list[dict[str, Any]]:
        """The volumes of the credential's project: ``[{id, name, status, metadata: {key: value},
        attachments: [server_id, ...]}]`` (§7.2 VMware rollback)."""

    async def delete_volume(self, volume_id: str) -> None:
        """Delete a volume once Cinder settled it, and wait until it is gone, within the call's
        bound; one already gone is no error."""

    async def detach_volume(self, server_id: str, volume_id: str) -> None:
        """Remove Nova's attachment of the volume to the server and wait until Cinder reads the
        volume available, within the call's bound; one not attached to it is no error."""

    async def server_of_address(self, address: str) -> str | None:
        """The Nova server holding the address: a compute port with it as a fixed IP, else the
        compute port Neutron's floating IP of that address is associated with; None when none
        (§7.2)."""

    async def server_volumes(self, server_id: str) -> list[str]:
        """The ids of the volumes Nova lists as attached to the server - its record alone, no
        Cinder or Neutron call; a server gone holds none (§7.2)."""

    async def set_volume_metadata(self, volume_id: str, metadata: dict[str, str]) -> None:
        """Set the given metadata keys on the volume and keep its other keys (Cinder's merge),
        within the call's bound: the mark of a VMware kit copy that completed (§7.2)."""

    async def reserve_floating_ip(self, address: str, project_id: str | None = None) -> None:
        """The keep-address move's reservation (SDD §8.3, §10): a floating IP of the address the
        project holds detached is kept, else one is created detached on the external network one
        of whose subnets holds it; a provider error when none does or Neutron refuses it. With
        ``project_id``, the provider's admin reserves for that project (§27.6, plan task TC5)."""

    async def grant_flavor_access(self, name: str, project_id: str) -> bool:
        """The private flavor of the name made usable by the project - access it holds already is
        no error -, True; a public one left as it is, False; none of the name, or two, a provider
        error (SDD §7.2, §10, §27.5, plan task TC5)."""

    async def server_port(self, server_id: str, address: str) -> str | None:
        """The id of the server's port with the fixed address, None when it has none (§8.3)."""


class FloatingIPMover(Protocol):
    """What an OpenStack provider, source or destination, moves floating IPs with (SDD §8.3, §10):
    each call on the credential's project's floating IP of the address."""

    async def floating_ip_port(self, address: str) -> str | None:
        """The port it is associated with; None when on none or when the project holds none."""

    async def disassociate_floating_ip(self, address: str) -> str | None:
        """Released from its port; returns that port's id, None when it was on none."""

    async def associate_floating_ip(self, address: str, port_id: str) -> None:
        """Associated with the port; one already there is no error."""

    async def release_floating_ip(self, address: str) -> None:
        """Deleted - its allocation released; none is no error."""


@runtime_checkable
class KeypairReader(Protocol):
    """What an OpenStack provider, source or destination, reads the os_migrate_conv keypair with
    before a pre-staging deploys conversion hosts (SDD §7.2 "Conversion key", §10, issue #112)."""

    async def keypair_public_key(self, name: str) -> str | None:
        """The public key of the credential's user's keypair called ``name``; None when the user
        holds none. Any other failure is a ProviderError, never None."""


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
