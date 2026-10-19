import builtins
import json
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from seamless_migrate.config import Settings
from seamless_migrate.domain.enums import ProviderKind, ProviderRole
from seamless_migrate.planning.platforms import destination_release
from seamless_migrate.planning.preflight import DestinationInventory, SourceInventory, is_legacy_os
from seamless_migrate.providers import registry
from seamless_migrate.providers.base import ProviderError, VMNotFound, provider_caps
from seamless_migrate.providers.fake import FakeDestinationProvider, FakeSourceProvider
from seamless_migrate.providers.openstack import OpenStackProvider
from seamless_migrate.providers.vmware import VMwareProvider
from tests.cloud_support import AUTH_URL, InProcessCloud
from tests.factories import make_provider

GIB = 2**30


def settings(**kw) -> Settings:
    return Settings(**kw)


# --------------------------------------------------------------------------------------------
# demo providers


async def test_fake_openstack_inventory_has_required_traits():
    src = FakeSourceProvider(ProviderKind.openstack, seed=42)
    vms = await src.list_vms()
    assert len(vms) == 24
    assert len({v.source_id for v in vms}) == 24 and len({v.name for v in vms}) == 24
    assert any(d.multiattach for v in vms for d in v.disks)
    assert any("resources:VGPU" in v.flavor_extra_specs for v in vms)
    assert any(is_legacy_os(v.os_type) and "rhel" in (v.os_type or "") for v in vms)
    assert any("windows" in (v.os_type or "") and "ad" in v.name for v in vms)
    big_dbs = [v for v in vms if "db" in v.name and v.disk_bytes >= 500 * GIB]
    assert len(big_dbs) >= 3
    assert await src.get_vm(vms[3].source_id) == vms[3]
    inv = await src.inventory()
    assert isinstance(inv, SourceInventory) and inv.networks and inv.projects
    caps = await src.check()
    assert caps["admin"] is True and "compute_microversion" in caps
    # the cloud answers it has no such VM: VMNotFound, a ProviderError (SDD §10)
    with pytest.raises(VMNotFound):
        await src.get_vm("does-not-exist")


async def test_fake_vmware_mixed_cbt():
    src = FakeSourceProvider(ProviderKind.vmware, seed=42)
    vms = await src.list_vms()
    assert len(vms) == 13
    assert {v.cbt_enabled for v in vms} == {True, False}
    # SDD §7.4: the demo VM whose warm passes meet what the cutover will refuse
    faults = {v.name: v.tags.get("demo-fault") for v in vms if v.tags.get("demo-fault")}
    assert faults == {"dc2-portal-02": "cutover-refusal"}
    assert any(v.snapshot_count > 0 for v in vms) and any(v.snapshot_count == 0 for v in vms)
    assert any(d.independent for v in vms for d in v.disks)
    assert any(v.tools_ok is False for v in vms)
    assert all(d.kind == "vmdk" for v in vms for d in v.disks)


async def test_fake_providers_are_deterministic_per_seed():
    a = await FakeSourceProvider(ProviderKind.openstack, seed=7).list_vms()
    b = await FakeSourceProvider(ProviderKind.openstack, seed=7).list_vms()
    c = await FakeSourceProvider(ProviderKind.openstack, seed=8).list_vms()
    assert a == b and a != c
    assert [v.name for v in a] == [v.name for v in c]  # traits are stable, numbers vary


async def test_fake_destination_inventory_and_servers():
    dst = FakeDestinationProvider(seed=42)
    inv = await dst.inventory()
    assert isinstance(inv, DestinationInventory)
    assert inv.flavors and inv.volume_types and inv.quotas and inv.networks
    # the demo's token, read with the inventory as its check reports it (SDD §10, RV19-01)
    caps = await dst.check()
    assert inv.roles == caps["roles"] == ["admin", "member", "reader"]
    assert caps["admin"] is True and caps["ovn"] is True
    server = await dst.get_server("srv-1")
    assert server["status"] == "ACTIVE" and server["ports"][0]["status"] == "ACTIVE"
    assert "login:" in (await dst.console_log("srv-1"))
    await dst.delete_server("srv-1")
    assert "srv-1" in dst.deleted
    assert await dst.find_server("web-01") is not None
    # the demo destination holds no port another migration could collide with (SDD §10)
    assert await dst.ports(network_id="net-1", mac_address="00:50:56:00:00:01") == []
    await dst.delete_port("port-gone")
    # nor a kit volume a VMware rollback would delete (SDD §7.2, §10)
    assert await dst.volumes() == []
    await dst.delete_volume("vol-gone")
    await dst.detach_volume("srv-conv", "vol-gone")


def test_registry_returns_fake_in_demo():
    demo = settings(demo=True)
    src = registry.build(make_provider(), demo)
    assert isinstance(src, FakeSourceProvider) and src.kind == ProviderKind.openstack
    vmw = registry.build(make_provider(id="vc", kind=ProviderKind.vmware), demo)
    assert isinstance(vmw, FakeSourceProvider) and vmw.kind == ProviderKind.vmware
    dst = registry.build(
        make_provider(id="dst", kind=ProviderKind.rhoso, role=ProviderRole.destination), demo
    )
    assert isinstance(dst, FakeDestinationProvider)

    real = settings(demo=False)
    assert isinstance(registry.build(make_provider(), real), OpenStackProvider)
    assert isinstance(
        registry.build(make_provider(id="vc", kind=ProviderKind.vmware), real), VMwareProvider
    )
    reg = registry.ProviderRegistry(demo)
    p = make_provider()
    assert reg.get(p) is reg.get(p), "instances are cached per provider configuration"
    assert reg.get(p) is not reg.get(p.model_copy(update={"region": "other"}))


def test_provider_caps_adds_conversion_host_flag():
    assert provider_caps(make_provider(capabilities={"admin": True})) == {
        "admin": True,
        "conversion_host": True,
    }
    assert provider_caps(make_provider(conversion_host=None))["conversion_host"] is False


# --------------------------------------------------------------------------------------------
# OpenStack provider against a stubbed openstacksdk connection


class FakeCompute:
    def __init__(self, servers, admin=True):
        self._servers = {s.id: s for s in servers}
        self._admin = admin
        self.deleted = []

    def servers(self, details=True, **_):
        return list(self._servers.values())

    def get_server(self, server_id):
        if server_id not in self._servers:
            import openstack.exceptions

            # what the SDK raises on HTTP 404
            raise openstack.exceptions.NotFoundException(f"No Server found for {server_id}")
        return self._servers[server_id]

    def volume_attachments(self, server):
        return server.attachments

    def hypervisors(self):
        if not self._admin:
            raise RuntimeError("HTTP 403: Policy doesn't allow os_compute_api:os-hypervisors")
        return [NS(name="compute-0")]

    def get_endpoint_data(self):
        return NS(max_microversion="2.95")

    def flavors(self, details=True):
        return [NS(name="m1.small", vcpus=2, ram=4096, disk=20, extra_specs={})]

    def keypairs(self, **_):
        # the credential's own keypairs (Nova lists only those without user_id)
        return [NS(name="ops-key"), NS(name="old-key")]

    def get_server_console_output(self, server_id, length=None):
        return {"output": "Booting...\nweb-01 login: "}

    def find_server(self, name, ignore_missing=True):
        for s in self._servers.values():
            if s.name == name:
                return s
        return None


class FakeBlockStorage:
    def __init__(self, volumes):
        self._volumes = {v.id: v for v in volumes}

    def get_volume(self, volume_id):
        return self._volumes[volume_id]

    def volumes(self, details=True, **_):
        return list(self._volumes.values())

    def types(self):
        return [NS(name="ceph-ssd"), NS(name="ceph-hdd")]

    def backend_pools(self):
        return [
            NS(
                name="hostgroup@ceph#ssd",
                capabilities={"vendor_name": "Open Source", "storage_protocol": "ceph"},
            )
        ]


class FakeNetwork:
    def __init__(self, ports, networks, ovn=True):
        self._ports = ports
        self._networks = {n.id: n for n in networks}
        self._ovn = ovn

    def ports(self, device_id=None, **_):
        if device_id is None:
            return list(self._ports)
        return [p for p in self._ports if p.device_id == device_id]

    def get_network(self, network_id):
        return self._networks[network_id]

    def networks(self, **_):
        return list(self._networks.values())

    def subnets(self, **_):
        return [
            NS(id="sub-1", name="app-subnet", network_id="net-1", cidr="10.0.0.0/24"),
            NS(
                id="sub-6",
                name="app-v6",
                network_id="net-1",
                cidr="fd00::/64",
                ipv6_address_mode="slaac",
                ipv6_ra_mode="slaac",
            ),
            NS(id="sub-x", name="orphan", network_id="net-gone", cidr="192.0.2.0/24"),
        ]

    def agents(self, **_):
        binary = "ovn-controller" if self._ovn else "neutron-openvswitch-agent"
        return [
            NS(
                agent_type="OVN Controller agent" if self._ovn else "Open vSwitch agent",
                binary=binary,
            )
        ]

    def ips(self, port_id=None, **_):
        return [NS(floating_ip_address="203.0.113.10")] if port_id == "port-1" else []


def fake_conn(admin=True, ovn=True):
    server = NS(
        id="srv-1",
        name="web-01",
        status="ACTIVE",
        project_id="proj-1",
        flavor={
            "original_name": "m1.small",
            "vcpus": 2,
            "ram": 4096,
            "disk": 20,
            "extra_specs": {"hw:cpu_policy": "shared"},
        },
        image={},
        metadata={"os_type": "rhel9", "app": "shop"},
        key_name="ops-key",
        compute_host="compute-0",
        attachments=[
            NS(volume_id="vol-root", device="/dev/vda"),
            NS(volume_id="vol-data", device="/dev/vdb"),
        ],
    )
    volumes = [
        NS(
            id="vol-root",
            name="web-01-root",
            size=20,
            is_bootable=True,
            volume_type="ceph-ssd",
            is_multiattach=False,
            is_encrypted=False,
        ),
        NS(
            id="vol-data",
            name="web-01-data",
            size=100,
            is_bootable=False,
            volume_type="ceph-hdd",
            is_multiattach=True,
            is_encrypted=True,
        ),
    ]
    ports = [
        NS(
            id="port-1",
            device_id="srv-1",
            network_id="net-1",
            mac_address="fa:16:3e:aa",
            fixed_ips=[{"ip_address": "10.0.0.5", "subnet_id": "sub-1"}],
            binding_vnic_type="normal",
            status="ACTIVE",
        )
    ]
    networks = [
        NS(
            id="net-1",
            name="app-net",
            mtu=1442,
            project_id="proj-1",
            is_shared=False,
            is_router_external=False,
        ),
        NS(
            id="net-9",
            name="provider-vlan9",
            mtu=1500,
            project_id="admin-proj",
            is_shared=True,
            is_router_external=False,
        ),
    ]
    # keystoneauth: conn.session.auth.get_access(conn.session).role_names (SDD §10)
    roles = ["admin", "member", "reader"] if admin else ["member", "reader"]
    auth = NS(get_access=lambda session: NS(role_names=roles))
    return NS(
        compute=FakeCompute([server], admin=admin),
        block_storage=FakeBlockStorage(volumes),
        network=FakeNetwork(ports, networks, ovn=ovn),
        identity=NS(get_project=lambda pid: NS(id=pid, name="finance")),
        current_project_id="proj-1",
        session=NS(auth=auth),
    )


@pytest.fixture
def stub_openstack(monkeypatch):
    openstack = pytest.importorskip("openstack")
    calls = {}

    def install(conn):
        def connect(**kwargs):
            calls.update(kwargs)
            return conn

        monkeypatch.setattr(openstack, "connect", connect)
        return calls

    return install


async def test_openstack_provider_maps_server_to_vmref(stub_openstack, tmp_path):
    clouds = tmp_path / "clouds.yaml"
    clouds.write_text(
        "clouds:\n  src:\n    auth:\n      auth_url: https://keystone.example/v3\n"
        "      username: admin\n      password: pw\n      project_name: finance\n"
        "    region_name: regionOne\n"
    )
    calls = stub_openstack(fake_conn())
    provider = OpenStackProvider(
        make_provider(region="regionTwo", verify_tls=False), settings(clouds_yaml=clouds)
    )
    [vm] = await provider.list_vms()
    assert calls["auth"]["username"] == "admin" and calls["region_name"] == "regionTwo"
    assert calls["verify"] is False
    assert (vm.source_id, vm.name, vm.project, vm.flavor) == (
        "srv-1",
        "web-01",
        "finance",
        "m1.small",
    )
    assert vm.key_name == "ops-key"  # SDD §4.1
    assert (vm.vcpus, vm.ram_mb, vm.power_state, vm.os_type, vm.host) == (
        2,
        4096,
        "running",
        "rhel9",
        "compute-0",
    )
    assert vm.flavor_extra_specs == {"hw:cpu_policy": "shared"}
    assert vm.tags["app"] == "shop"
    root, data = vm.disks
    assert (root.id, root.size_gb, root.bootable, root.device, root.kind) == (
        "vol-root",
        20,
        True,
        "/dev/vda",
        "volume",
    )
    assert (data.volume_type, data.multiattach, data.encrypted) == ("ceph-hdd", True, True)
    [nic] = vm.nics
    assert (nic.network, nic.mac, nic.fixed_ips, nic.mtu) == (
        "app-net",
        "fa:16:3e:aa",
        ["10.0.0.5"],
        1442,
    )
    assert await provider.get_vm("srv-1") == vm

    server = await provider.get_server("srv-1")
    assert server["status"] == "ACTIVE"
    assert server["ports"] == [
        {"status": "ACTIVE", "fixed_ips": ["10.0.0.5"], "floating_ips": ["203.0.113.10"]}
    ]
    assert "login:" in await provider.console_log("srv-1")
    assert await provider.find_server("web-01") == "srv-1"
    inv = await provider.inventory()
    assert inv.networks == {"app-net": 1442, "provider-vlan9": 1500}


def _fleet_conn(count=3):
    """fake_conn() with `count` servers, each with two volumes and a port, and call counters."""
    conn = fake_conn()
    base = conn.compute.get_server("srv-1")
    servers, volumes, ports = [], [], []
    for i in range(count):
        sid = f"srv-{i}"
        servers.append(
            NS(
                **{**vars(base), "id": sid, "name": f"web-{i:02d}"},
            )
        )
        servers[-1].attachments = [
            NS(volume_id=f"{sid}-root", device="/dev/vda"),
            NS(volume_id=f"{sid}-data", device="/dev/vdb"),
        ]
        volumes += [
            NS(id=f"{sid}-root", name="root", size=20, is_bootable=True, volume_type="ceph-ssd"),
            NS(id=f"{sid}-data", name="data", size=50, is_bootable=False, volume_type="ceph-hdd"),
        ]
        ports.append(
            NS(
                id=f"port-{sid}",
                device_id=sid,
                network_id="net-1",
                mac_address=f"fa:16:3e:0{i}",
                fixed_ips=[{"ip_address": f"10.0.0.{10 + i}"}],
                binding_vnic_type="normal",
            )
        )
    conn.compute = FakeCompute(servers)
    conn.block_storage = FakeBlockStorage(volumes)
    conn.network = FakeNetwork(ports, [NS(id="net-1", name="app-net", mtu=1442)])
    calls = {"get_volume": 0, "volumes": 0, "ports_one": 0, "ports_all": 0}
    get_volume, list_volumes, list_ports = (
        conn.block_storage.get_volume,
        conn.block_storage.volumes,
        conn.network.ports,
    )

    def counted_get_volume(volume_id):
        calls["get_volume"] += 1
        return get_volume(volume_id)

    def counted_volumes(**kwargs):
        calls["volumes"] += 1
        return list_volumes(**kwargs)

    def counted_ports(device_id=None, **kwargs):
        calls["ports_one" if device_id else "ports_all"] += 1
        return list_ports(device_id=device_id, **kwargs)

    conn.block_storage.get_volume = counted_get_volume
    conn.block_storage.volumes = counted_volumes
    conn.network.ports = counted_ports
    return conn, calls


async def test_openstack_list_vms_fetches_volumes_and_ports_in_bulk(stub_openstack):
    conn, calls = _fleet_conn(3)
    stub_openstack(conn)
    vms = await OpenStackProvider(make_provider(), settings()).list_vms()
    assert [vm.name for vm in vms] == ["web-00", "web-01", "web-02"]
    assert [[d.id for d in vm.disks] for vm in vms][1] == ["srv-1-root", "srv-1-data"]
    assert [vm.nics[0].fixed_ips for vm in vms] == [["10.0.0.10"], ["10.0.0.11"], ["10.0.0.12"]]
    # one listing each instead of a call per volume and per server
    assert calls == {"get_volume": 0, "volumes": 1, "ports_one": 0, "ports_all": 1}


async def test_openstack_list_vms_falls_back_per_vm_when_bulk_listing_is_refused(stub_openstack):
    conn, calls = _fleet_conn(2)

    def refused(**_):
        raise RuntimeError("HTTP 403")

    conn.block_storage.volumes = refused
    real_ports = conn.network.ports

    def ports(device_id=None, **kwargs):
        if device_id is None:
            raise RuntimeError("HTTP 403")
        return real_ports(device_id=device_id, **kwargs)

    conn.network.ports = ports
    stub_openstack(conn)
    vms = await OpenStackProvider(make_provider(), settings()).list_vms()
    assert [len(vm.disks) for vm in vms] == [2, 2]
    assert [vm.nics[0].mac for vm in vms] == ["fa:16:3e:00", "fa:16:3e:01"]
    assert calls["get_volume"] == 4 and calls["ports_one"] == 2


async def test_openstack_flavor_ephemeral_and_swap_become_disks(stub_openstack):
    """Flavor ephemeral (GiB) and swap (MiB) disks count towards capacity and the estimate."""
    conn = fake_conn()
    [server] = conn.compute._servers.values()
    server.flavor = {**server.flavor, "ephemeral": 200, "swap": 2048}
    stub_openstack(conn)
    provider = OpenStackProvider(make_provider(), settings())
    [vm] = await provider.list_vms()
    by_name = {d.name: d for d in vm.disks}
    assert by_name["ephemeral"].kind == "ephemeral" and by_name["ephemeral"].size_gb == 200
    assert by_name["swap"].kind == "ephemeral" and by_name["swap"].size_gb == 2
    assert vm.disk_bytes == (20 + 100 + 200 + 2) * 2**30
    assert vm.root_disk() is not None and vm.root_disk().id == "vol-root"


async def test_openstack_boot_volume_falls_back_to_cinder_bootable_flag(stub_openstack):
    """Nova may report no root_device_name, or one the attachment does not carry (virtio-scsi
    /dev/sda): the volume flagged bootable by Cinder is the boot disk then."""
    conn = fake_conn()
    [server] = conn.compute._servers.values()
    server.root_device_name = None
    server.attachments = [
        NS(volume_id="vol-data", device="/dev/sda"),
        NS(volume_id="vol-root", device="/dev/sdb"),
    ]
    stub_openstack(conn)
    [vm] = await OpenStackProvider(make_provider(), settings()).list_vms()
    assert [d.bootable for d in vm.disks] == [True, False]
    assert vm.root_disk().id == "vol-root"


async def test_openstack_tls_off_is_logged_and_calls_are_bounded(
    stub_openstack, monkeypatch, caplog
):
    import logging
    import time

    from seamless_migrate.providers import openstack as osp

    calls = stub_openstack(fake_conn())
    with caplog.at_level(logging.WARNING, logger="seamless_migrate.providers.openstack"):
        provider = OpenStackProvider(make_provider(verify_tls=False), settings())
        await provider.list_vms()
    assert "TLS certificate verification is disabled" in caplog.text
    assert calls["api_timeout"] == osp.PROVIDER_API_TIMEOUT_S

    monkeypatch.setattr(osp, "PROVIDER_CALL_TIMEOUT_S", 0.05)
    slow = OpenStackProvider(
        make_provider(), settings(), connect_fn=lambda p, s: time.sleep(0.3) or fake_conn()
    )
    with pytest.raises(ProviderError, match="timed out after 0.05 s"):
        await slow.check()

    closed = []
    provider._conn = NS(close=lambda: closed.append(True))
    provider.close()
    assert closed == [True] and provider._conn is None


async def test_openstack_transitional_nova_states_are_reported(stub_openstack):
    conn = fake_conn()
    [server] = conn.compute._servers.values()
    server.status = "VERIFY_RESIZE"
    stub_openstack(conn)
    [vm] = await OpenStackProvider(make_provider(), settings()).list_vms()
    assert vm.power_state == "transitioning"


@pytest.mark.parametrize(
    "status, task",
    [
        ("ACTIVE", "image_uploading"),
        ("ACTIVE", "powering-off"),
        ("SHUTOFF", "image_snapshot"),
        ("ACTIVE", None),
    ],
)
async def test_openstack_a_task_in_flight_is_read_apart_from_the_power_state(
    stub_openstack, status, task
):
    """SDD §4.2 (review PV-05): Nova runs a task under a steady status - a backup's image snapshot
    or its upload, a stop in flight - and reports it as OS-EXT-STS:task_state. The VM carries it
    as task_state, and its power state stays what the status says: the source watch still reads a
    running guest a backup snapshots as running (§8.1)."""
    conn = fake_conn()
    [server] = conn.compute._servers.values()
    server.status, server.task_state = status, task
    stub_openstack(conn)
    [vm] = await OpenStackProvider(make_provider(), settings()).list_vms()
    power = {"ACTIVE": "running", "SHUTOFF": "stopped"}[status]
    assert (vm.power_state, vm.task_state) == (power, task)


async def test_openstack_get_vm_tells_a_server_the_cloud_lacks_from_a_failing_cloud(stub_openstack):
    """SDD §8.1, §10: the source watch clears a source that ran again once its cloud says the VM is
    gone - a 404 is VMNotFound; any other failure stays a plain ProviderError (not gone)."""
    conn = fake_conn()
    stub_openstack(conn)
    provider = OpenStackProvider(make_provider(), settings())
    with pytest.raises(VMNotFound, match="not found"):
        await provider.get_vm("srv-gone")

    def unreachable(server_id):
        raise RuntimeError("Unable to establish connection to https://nova:8774")

    conn.compute.get_server = unreachable
    with pytest.raises(ProviderError) as failed:
        await provider.get_vm("srv-1")
    assert not isinstance(failed.value, VMNotFound)


async def test_openstack_image_booted_server_gets_image_root_disk(stub_openstack):
    conn = fake_conn()
    srv = conn.compute.get_server("srv-1")
    srv.image = {"id": "img-1"}
    srv.attachments = [NS(volume_id="vol-data", device="/dev/vdb")]
    stub_openstack(conn)
    vm = await OpenStackProvider(make_provider(), settings()).get_vm("srv-1")
    assert vm.disks[0].kind == "image_root" and vm.disks[0].size_gb == 20
    assert vm.disks[0].bootable and not vm.disks[1].bootable


async def test_openstack_check_reports_admin_and_ovn(stub_openstack):
    stub_openstack(fake_conn(admin=True, ovn=True))
    caps = await OpenStackProvider(make_provider(), settings()).check()
    assert caps == {
        "admin": True,
        "roles": ["admin", "member", "reader"],
        "compute_microversion": "2.95",
        # the stub's block-storage proxy has no version document: unreadable (SDD §10)
        "block_storage_microversion": "",
        "ovn": True,
        "volume_backends": ["hostgroup@ceph#ssd"],
        "storage_backends": [
            {
                "pool": "hostgroup@ceph#ssd",
                "vendor": "Open Source",
                "protocol": "ceph",
                "family": "rbd",
            }
        ],
        # an https endpoint, and no service endpoint the stub can tell (SDD §10, Security.md)
        "tls": True,
        "plain_http": [],
    }
    stub_openstack(fake_conn(admin=False, ovn=False))
    caps = await OpenStackProvider(make_provider(), settings()).check()
    assert caps["admin"] is False and caps["ovn"] is False
    assert caps["roles"] == ["member", "reader"]


async def test_the_check_reports_a_real_clouds_compute_maximum_as_major_minor(tmp_path):
    """Review RV18-01: keystoneauth hands openstacksdk a service's maximum microversion as a
    parsed tuple, (2, 96), and the check wrote it as text - "(2, 96)", which nothing reads as a
    microversion and the providers page showed as such. It reports "2.96" (SDD §10)."""
    cloud = InProcessCloud(tmp_path, compute="2.96")
    provider = make_provider(endpoint=AUTH_URL, cloud="dst")
    caps = await OpenStackProvider(provider, cloud.settings, connect_fn=cloud.connect).check()

    assert caps["compute_microversion"] == "2.96"
    # what keystoneauth hands the check: a tuple, never the text a stub hands
    conn = cloud.connect(provider, cloud.settings)
    assert conn.compute.get_endpoint_data().max_microversion == (2, 96)


VERSIONS = Path(__file__).parent / "fixtures" / "openstack_versions"


def _current(document):
    """The maximum of a recorded version document's CURRENT version; None without a document."""
    if not document:
        return None
    return next(v["version"] for v in document["versions"] if v["status"] == "CURRENT")


@pytest.mark.parametrize("path", sorted(VERSIONS.glob("*.json")), ids=lambda path: path.stem)
async def test_a_real_destination_of_each_series_is_read_as_that_series(tmp_path, path):
    """Review RV18-01: on a real cloud every destination read "not detected" - Outside, its
    profile unknown - since the check wrote keystoneauth's tuples as text. Through a real
    keystoneauth session each series' recorded maxima read as SDD §10.1 reads their text: both
    maxima as major.minor, the series' range, and the tier and profile of that range (SDD §10,
    §21.3)."""
    documents = json.loads(path.read_text(encoding="utf-8"))
    compute, block_storage = _current(documents["compute"]), _current(documents["block_storage"])
    cloud = InProcessCloud(tmp_path, compute=compute, block_storage=block_storage)
    provider = make_provider(
        id="dst-community",
        role=ProviderRole.destination,
        distribution="openstack_community",
        endpoint=AUTH_URL,
        cloud="dst",
    )
    caps = await OpenStackProvider(provider, cloud.settings, connect_fn=cloud.connect).check()

    maxima = (caps["compute_microversion"], caps["block_storage_microversion"])
    assert maxima == (compute, block_storage or "")
    assert caps["release_range"] == documents["expected_range"]
    reported = {key: caps[key] for key in ("release_range", "release", "support_tier")}
    expected = destination_release("openstack", None, None, compute, block_storage or "")
    assert reported | {"policy_profile": caps["policy_profile"]} == expected


async def test_every_openstack_check_reports_the_shared_keys(tmp_path):
    """SDD §10, §10.1: the real check through keystoneauth and the demo's fakes report the keys of
    check_report_keys.json - a destination's adds the destination_release keys -, the file the
    dashboard's mock check is held to (plan T8)."""
    keys = json.loads((VERSIONS.parent / "check_report_keys.json").read_text(encoding="utf-8"))
    source, destination = sorted(keys["openstack"]), sorted(keys["openstack"] + keys["destination"])
    cloud = InProcessCloud(tmp_path, compute="2.104", block_storage="3.71")
    for role, expected in ((ProviderRole.source, source), (ProviderRole.destination, destination)):
        provider = make_provider(endpoint=AUTH_URL, cloud="dst", role=role)
        caps = await OpenStackProvider(provider, cloud.settings, connect_fn=cloud.connect).check()
        assert sorted(caps) == expected, role
    assert sorted(await FakeSourceProvider(ProviderKind.openstack).check()) == source
    community = make_provider(id="dst-dc3", role=ProviderRole.destination, release="2026.2")
    for provider in (community, community.model_copy(update={"kind": ProviderKind.rhoso})):
        caps = await FakeDestinationProvider(provider=provider).check()
        assert sorted(caps) == destination, provider.kind


def _versions(conn, compute, block_storage):
    conn.compute.get_endpoint_data = lambda: NS(max_microversion=compute)
    conn.block_storage.get_endpoint_data = lambda: NS(max_microversion=block_storage)
    return conn


async def test_openstack_check_reports_release_range_tier_profile_and_tls(stub_openstack):
    """SDD §10, §10.1, §21.3, §21.5: a destination's check reports the series its maxima read as,
    the series it settles on, its support tier and its policy profile, and TLS; a source's check
    reports TLS but no destination keys."""
    hibiscus = make_provider(
        id="dst-hibiscus",
        role=ProviderRole.destination,
        distribution="kolla",
        endpoint="https://keystone.dst.example:5000/v3",
    )
    stub_openstack(_versions(fake_conn(), "2.104", "3.71"))
    caps = await OpenStackProvider(hibiscus, settings()).check()
    assert caps["compute_microversion"] == "2.104" and caps["block_storage_microversion"] == "3.71"
    assert {
        key: caps[key] for key in ("release_range", "release", "support_tier", "policy_profile")
    } == {
        "release_range": ["2026.2"],
        "release": "2026.2",
        "support_tier": {"tier": "tier1", "reason": None, "declared_outside_range": False},
        "policy_profile": "secure_rbac",
    }
    assert caps["tls"] is True and caps["plain_http"] == []

    # 2.96 is 2024.1 or 2024.2: Tier 3 until the operator declares the series; a declared series
    # the range does not hold warns, and a declared profile wins
    stub_openstack(_versions(fake_conn(), "2.96", "3.71"))
    caps = await OpenStackProvider(
        hibiscus.model_copy(update={"release": "2026.2"}), settings()
    ).check()
    assert caps["release_range"] == ["2024.1", "2024.2"] and caps["release"] is None
    assert caps["support_tier"] == {
        "tier": "tier3",
        "reason": "end_of_life",
        "declared_outside_range": True,
    }
    declared = hibiscus.model_copy(update={"release": "2024.1", "policy_profile": "legacy"})
    caps = await OpenStackProvider(declared, settings()).check()
    assert caps["release"] == "2024.1" and caps["support_tier"]["tier"] == "tier2"
    assert caps["policy_profile"] == "legacy"

    # Cinder's maximum reaches the detection: 2.98 alone is 2024.2 or 2025.1, and 3.72 is no
    # 2024.2 Cinder (SDD §10.1)
    stub_openstack(_versions(fake_conn(), "2.98", "3.72"))
    caps = await OpenStackProvider(hibiscus, settings()).check()
    assert caps["release_range"] == ["2025.1"] and caps["release"] == "2025.1"

    # RHOSO 18.0 is validated by its kind, and legacy until D14 is decided
    rhoso = make_provider(id="dst-rhoso", kind=ProviderKind.rhoso, role=ProviderRole.destination)
    stub_openstack(_versions(fake_conn(), "2.95", "3.70"))
    caps = await OpenStackProvider(rhoso, settings()).check()
    assert caps["support_tier"] == {
        "tier": "validated",
        "reason": None,
        "declared_outside_range": False,
    }
    assert caps["policy_profile"] == "legacy" and caps["release_range"] == ["2023.1", "2023.2"]

    # a source reports none of a destination's keys
    caps = await OpenStackProvider(make_provider(), settings()).check()
    assert not {"release_range", "release", "support_tier", "policy_profile"} & set(caps)
    assert caps["tls"] is True


async def test_a_plain_http_endpoint_is_reported(stub_openstack):
    """SDD §10, §21.7, Security.md: the check names the provider's own endpoint and each service
    endpoint that uses plain HTTP; an endpoint it cannot read counts neither way."""
    conn = fake_conn()
    conn.compute.get_endpoint = lambda: "http://nova.dst.example:8774/v2.1"
    conn.network = NS(**vars(conn.network), get_endpoint=lambda: "HTTP://neutron.dst.example:9696")
    conn.identity = NS(
        **vars(conn.identity), get_endpoint=lambda: "https://keystone.dst.example/v3"
    )

    def unreadable():
        raise RuntimeError("the catalog has no block-storage endpoint for this region")

    conn.block_storage.get_endpoint = unreadable
    stub_openstack(conn)
    plain = make_provider(endpoint="http://keystone.dst.example:5000/v3")
    caps = await OpenStackProvider(plain, settings()).check()
    assert caps["tls"] is False
    assert caps["plain_http"] == ["endpoint", "compute", "network"]

    secure = make_provider(endpoint="https://keystone.dst.example:5000/v3")
    caps = await OpenStackProvider(secure, settings()).check()
    assert caps["tls"] is False and caps["plain_http"] == ["compute", "network"]

    conn.compute.get_endpoint = lambda: "https://nova.dst.example:8774/v2.1"
    conn.network = NS(**{**vars(conn.network), "get_endpoint": lambda: "https://neutron.example"})
    caps = await OpenStackProvider(secure, settings()).check()
    assert caps["tls"] is True and caps["plain_http"] == []


async def test_the_check_reads_a_real_clouds_identity_endpoint(tmp_path):
    """Review RV18-03: openstacksdk's identity proxy overrides get_endpoint() - it fetches a
    Keystone endpoint record -, so the check never read identity's endpoint, and a plain-HTTP one
    counted neither way. It is read as keystoneauth reads every service's from the catalog (SDD
    §10, Security.md)."""
    provider = make_provider(endpoint=AUTH_URL, cloud="dst")
    secure = InProcessCloud(tmp_path / "secure")
    caps = await OpenStackProvider(provider, secure.settings, connect_fn=secure.connect).check()
    assert (caps["tls"], caps["plain_http"]) == (True, [])

    plain = InProcessCloud(
        tmp_path / "plain", catalog={"identity": "http://identity.example.org/v3"}
    )
    caps = await OpenStackProvider(provider, plain.settings, connect_fn=plain.connect).check()
    assert (caps["tls"], caps["plain_http"]) == (False, ["identity"])


async def test_the_check_reports_the_plain_http_auth_url_the_credential_is_posted_to(tmp_path):
    """Review RV18-03: the credential is posted to the auth URL of its clouds.yaml entry or
    credentials secret, which need not be the provider's endpoint - a plain-HTTP one sent the
    password in clear text while the check reported tls. It is named `auth_url` (SDD §10, §13.3,
    Security.md)."""
    cloud = InProcessCloud(tmp_path, auth_url="http://identity.example.org/v3")
    provider = make_provider(endpoint=AUTH_URL, cloud="dst")
    caps = await OpenStackProvider(provider, cloud.settings, connect_fn=cloud.connect).check()

    assert ("POST", "http://identity.example.org/v3/auth/tokens") in cloud.seen
    assert (caps["tls"], caps["plain_http"]) == (False, ["auth_url"])


async def test_the_demo_destination_reports_rhoso_validated_and_legacy():
    """The demo's RHOSO 18.0 destination reports the keys a real destination's check does (SDD
    §10, §10.1), so the dashboard shows them in the demo too."""
    caps = await FakeDestinationProvider(seed=42).check()
    assert caps["release_range"] == ["2023.1", "2023.2"] and caps["release"] is None
    assert caps["support_tier"] == {
        "tier": "validated",
        "reason": None,
        "declared_outside_range": False,
    }
    assert caps["policy_profile"] == "legacy"
    assert caps["tls"] is True and caps["plain_http"] == []
    assert caps["compute_microversion"] == "2.95" and caps["block_storage_microversion"] == "3.70"


async def test_openstack_check_leaves_out_roles_it_cannot_read(stub_openstack):
    """SDD §10, §9.3: the token's role names are what NET_PORT_NOT_PERMITTED judges; a token
    whose roles cannot be read reports none, so that half of the rule is not judged."""
    conn = fake_conn()

    def unreadable(session):
        raise RuntimeError("the auth plugin keeps no access info")

    conn.session = NS(auth=NS(get_access=unreadable))
    stub_openstack(conn)
    caps = await OpenStackProvider(make_provider(), settings()).check()
    assert "roles" not in caps and caps["admin"] is True
    conn.session = NS(
        auth=NS(get_access=lambda session: NS(role_names=["reader", "admin", "admin"]))
    )
    caps = await OpenStackProvider(make_provider(), settings()).check()
    assert caps["roles"] == ["admin", "reader"]


async def test_openstack_check_reports_storage_backends_with_family(stub_openstack):
    conn = fake_conn()
    conn.block_storage.backend_pools = lambda: [
        NS(
            name="overcloud@ontap_nfs#192.0.2.5:/cinder_vol",
            capabilities={"vendor_name": "NetApp", "storage_protocol": "nfs"},
        ),
        NS(
            name="overcloud@ontap_iscsi#flex_a",
            capabilities={"vendor_name": "NetApp", "storage_protocol": "iSCSI"},
        ),
        NS(name="overcloud@lvm#lvm", capabilities=None),
    ]
    stub_openstack(conn)
    caps = await OpenStackProvider(make_provider(), settings()).check()
    assert caps["volume_backends"] == [
        "overcloud@lvm#lvm",
        "overcloud@ontap_iscsi#flex_a",
        "overcloud@ontap_nfs#192.0.2.5:/cinder_vol",
    ]
    assert [(b["pool"], b["family"]) for b in caps["storage_backends"]] == [
        ("overcloud@lvm#lvm", "other"),
        ("overcloud@ontap_iscsi#flex_a", "netapp_block"),
        ("overcloud@ontap_nfs#192.0.2.5:/cinder_vol", "netapp_nfs"),
    ]
    assert caps["storage_backends"][1]["vendor"] == "NetApp"
    assert caps["storage_backends"][1]["protocol"] == "iSCSI"
    # without admin the pool listing is not attempted
    stub_openstack(fake_conn(admin=False))
    caps = await OpenStackProvider(make_provider(), settings()).check()
    assert caps["storage_backends"] == [] and caps["volume_backends"] == []


async def test_openstack_volume_disks_carry_their_cinder_pool(stub_openstack):
    conn = fake_conn()
    conn.block_storage.get_volume("vol-root").host = "overcloud@ontap_nfs#192.0.2.5:/cinder_vol"
    stub_openstack(conn)
    [vm] = await OpenStackProvider(make_provider(), settings()).list_vms()
    root, data = vm.disks
    assert root.pool == "overcloud@ontap_nfs#192.0.2.5:/cinder_vol"
    assert data.pool is None  # host attribute hidden from non-admin credentials


async def test_a_destinations_inventory_reads_its_tokens_roles_live(stub_openstack):
    """Review RV19-01, SDD §10: the destination inventory carries its token's role names, read with
    it for every run - what the VMware kit's map judges, not a check report new credentials may
    have reset. A token whose roles cannot be read gives None; a source inventory has none."""
    conn = fake_conn(admin=False)
    stub_openstack(conn)
    destination = make_provider(id="dst", kind=ProviderKind.rhoso, role=ProviderRole.destination)
    assert (await OpenStackProvider(destination, settings()).inventory()).roles == [
        "member",
        "reader",
    ]

    def unreadable(session):
        raise RuntimeError("the token has expired")

    conn.session = NS(auth=NS(get_access=unreadable))
    assert (await OpenStackProvider(destination, settings()).inventory()).roles is None
    assert not hasattr(await OpenStackProvider(make_provider(), settings()).inventory(), "roles")


async def test_openstack_destination_inventory(stub_openstack):
    conn = fake_conn()
    stub_openstack(conn)
    dst = OpenStackProvider(
        make_provider(id="dst", kind=ProviderKind.rhoso, role=ProviderRole.destination),
        settings(),
    )
    inv = await dst.inventory()
    assert isinstance(inv, DestinationInventory)
    assert inv.flavors == [
        {"name": "m1.small", "vcpus": 2, "ram_mb": 4096, "disk_gb": 20, "extra_specs": {}}
    ]
    assert inv.volume_types == ["ceph-ssd", "ceph-hdd"]
    # SDD §10: the destination credential's own keypairs (DST_KEYPAIR_MISSING)
    assert inv.keypairs == ["old-key", "ops-key"]
    # SDD §10: every network it sees, with its owner: what a NIC's network name resolves to;
    # shared and external: whether the credential may put a port on another project's (§6.6)
    assert inv.network_details == [
        {
            "id": "net-1",
            "name": "app-net",
            "mtu": 1442,
            "project_id": "proj-1",
            "own": True,
            "shared": False,
            "external": False,
        },
        {
            "id": "net-9",
            "name": "provider-vlan9",
            "mtu": 1500,
            "project_id": "admin-proj",
            "own": False,
            "shared": True,
            "external": False,
        },
    ]
    conn.network.get_network("net-9").is_router_external = True
    assert (await dst.inventory()).network_details[1]["external"] is True
    # the subnets it sees, by network name and id, where a NIC's addresses must lie
    # each with its id: what a VMware NIC's network_map value names (SDD §10, §7.2)
    assert inv.subnets == [
        {
            "id": "sub-1",
            "name": "app-subnet",
            "network": "app-net",
            "network_id": "net-1",
            "cidr": "10.0.0.0/24",
            "ipv6_address_mode": None,
            "ipv6_ra_mode": None,
        },
        {
            "id": "sub-6",
            "name": "app-v6",
            "network": "app-net",
            "network_id": "net-1",
            "cidr": "fd00::/64",
            "ipv6_address_mode": "slaac",
            "ipv6_ra_mode": "slaac",
        },
    ]


async def test_openstack_source_inventory_lists_the_credentials_keypairs(stub_openstack):
    """SDD §11: what export_keypairs.yml can export - the source credential's own keypairs."""
    stub_openstack(fake_conn())
    inv = await OpenStackProvider(make_provider(), settings()).inventory()
    assert isinstance(inv, SourceInventory)
    assert inv.keypairs == ["old-key", "ops-key"]


#: an SDK error whose 500-character cut falls inside the password of a URL
SDK_ERROR = "x" * 479 + " https://svc:Sup3rS3cretPw@keystone.example:5000/v3 refused"


async def test_openstack_errors_are_redacted_before_they_are_cut():
    """SDD §13.4: a provider SDK's error message is redacted over its cut plus 16384 characters
    before it is cut to 500 — a URL whose password the cut splits would otherwise reach a 502
    answer, an event or Migration.error, as the later redaction no longer recognizes it."""

    def failing_connect(provider, settings):
        raise RuntimeError(SDK_ERROR)

    provider = OpenStackProvider(make_provider(), settings(), connect_fn=failing_connect)
    with pytest.raises(ProviderError) as failed:
        await provider.check()
    assert "Sup3r" not in str(failed.value) and "[REDACTED]" in str(failed.value)


async def test_vmware_errors_are_redacted_before_they_are_cut(tmp_path):
    """SDD §13.4: the same for the VMware provider."""
    secret_dir = tmp_path / "secrets" / "vcenter-dc2"
    secret_dir.mkdir(parents=True)
    (secret_dir / "username").write_text("svc-migrate@vsphere.local\n")
    (secret_dir / "password").write_text("pw\n")

    def failing_connector(host, port, user, pwd, verify, ca_cert):
        raise RuntimeError(SDK_ERROR)

    provider = VMwareProvider(
        make_provider(
            id="vcenter",
            kind=ProviderKind.vmware,
            credentials_secret="vcenter-dc2",
            endpoint="https://vcenter.dc2.example:8443/sdk",
            cloud=None,
        ),
        settings(secrets_dir=tmp_path / "secrets"),
        connector=failing_connector,
        vim=FAKE_VIM,
    )
    with pytest.raises(ProviderError) as failed:
        await provider.list_vms()
    assert "Sup3r" not in str(failed.value) and "[REDACTED]" in str(failed.value)


async def test_openstack_connection_errors_become_provider_errors(monkeypatch):
    openstack = pytest.importorskip("openstack")

    def boom(**_):
        raise RuntimeError("The request you have made requires authentication.")

    monkeypatch.setattr(openstack, "connect", boom)
    with pytest.raises(ProviderError, match="authentication"):
        await OpenStackProvider(make_provider(), settings()).check()


# --------------------------------------------------------------------------------------------
# VMware provider against stubbed pyVmomi objects


class VirtualDisk:
    def __init__(self, key, label, capacity_kb, mode="persistent", uuid=None):
        self.key = key
        self.deviceInfo = NS(label=label)
        self.capacityInKB = capacity_kb
        self.backing = NS(diskMode=mode, uuid=uuid, fileName=f"[ds1] vm/{label}.vmdk")


class VirtualEthernetCard:
    def __init__(self, mac, network):
        self.key = 4000
        self.macAddress = mac
        # vSphere's summary of a NIC on a standard port group is the network's name
        self.deviceInfo = NS(label="Network adapter 1", summary=network)
        self.backing = NS(deviceName=network)


class VirtualMachine:  # marker type for the container view
    pass


FAKE_VIM = NS(
    VirtualMachine=VirtualMachine,
    vm=NS(device=NS(VirtualDisk=VirtualDisk, VirtualEthernetCard=VirtualEthernetCard)),
)


def fake_vcenter_vm():
    vm = VirtualMachine()
    vm.name = "dc2-db-01"
    vm._moId = "vm-101"
    vm.config = NS(
        instanceUuid="5012-abcd",
        guestId="rhel6_64Guest",
        template=False,
        changeTrackingEnabled=True,
        hardware=NS(
            numCPU=4,
            memoryMB=16384,
            device=[
                VirtualDisk(2000, "Hard disk 1", 40 * 1024 * 1024, uuid="6000-1"),
                VirtualDisk(
                    2001,
                    "Hard disk 2",
                    500 * 1024 * 1024,
                    mode="independent_persistent",
                    uuid="6000-2",
                ),
                VirtualEthernetCard("00:50:56:aa:bb:cc", "DC2-Prod"),
            ],
        ),
    )
    vm.runtime = NS(powerState="poweredOn", host=NS(name="esx-01.dc2"))
    vm.guest = NS(
        toolsRunningStatus="guestToolsNotRunning",
        net=[NS(macAddress="00:50:56:aa:bb:cc", ipAddress=["10.2.0.15", "fe80::1"])],
    )
    child = NS(childSnapshotList=[])
    vm.snapshot = NS(rootSnapshotList=[NS(childSnapshotList=[child])])
    vm.customValue = [NS(key=1, value="billing")]
    return vm


def fake_service_instance(vms):
    view = NS(view=vms, Destroy=lambda: None)
    content = NS(
        rootFolder=object(),
        viewManager=NS(CreateContainerView=lambda folder, types, recursive: view),
        about=NS(apiVersion="8.0.2.0", version="8.0.2"),
        customFieldsManager=NS(field=[NS(key=1, name="app")]),
    )
    return NS(RetrieveContent=lambda: content)


async def test_vmware_provider_maps_vm(tmp_path):
    secret_dir = tmp_path / "secrets" / "vcenter-dc2"
    secret_dir.mkdir(parents=True)
    (secret_dir / "username").write_text("svc-migrate@vsphere.local\n")
    (secret_dir / "password").write_text("pw\n")
    connections = []

    def connector(host, port, user, pwd, verify, ca_cert):
        connections.append((host, port, user, verify))
        return fake_service_instance([fake_vcenter_vm()])

    provider = VMwareProvider(
        make_provider(
            id="vcenter",
            kind=ProviderKind.vmware,
            credentials_secret="vcenter-dc2",
            endpoint="https://vcenter.dc2.example:8443/sdk",
            cloud=None,
        ),
        settings(secrets_dir=tmp_path / "secrets"),
        connector=connector,
        vim=FAKE_VIM,
    )
    [vm] = await provider.list_vms()
    assert connections == [("vcenter.dc2.example", 8443, "svc-migrate@vsphere.local", True)]
    assert (vm.source_id, vm.name, vm.vcpus, vm.ram_mb) == ("5012-abcd", "dc2-db-01", 4, 16384)
    assert (vm.power_state, vm.os_type, vm.host) == ("running", "rhel6_64Guest", "esx-01.dc2")
    assert vm.cbt_enabled is True and vm.snapshot_count == 2 and vm.tools_ok is False
    d1, d2 = vm.disks
    assert (d1.id, d1.size_gb, d1.bootable, d1.kind, d1.independent) == (
        "6000-1",
        40,
        True,
        "vmdk",
        False,
    )
    assert (d2.size_gb, d2.bootable, d2.independent) == (500, False, True)
    [nic] = vm.nics
    assert (nic.network, nic.mac, nic.fixed_ips) == (
        "DC2-Prod",
        "00:50:56:aa:bb:cc",
        ["10.2.0.15", "fe80::1"],
    )
    # SDD §4, §7.2: what the migration kit maps the NIC by - a standard port group's NIC by its
    # device summary
    assert nic.network_key == "DC2-Prod"
    assert vm.tags == {"app": "billing"}
    caps = await provider.check()
    assert caps["api_version"] == "8.0.2.0" and caps["cbt_enabled_vms"] == 1
    assert caps["vms_with_snapshots"] == 1 and caps["vms_with_independent_disks"] == 1
    assert caps["vms_without_tools"] == 1


def test_missing_optional_dependency_raises_provider_error(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.split(".")[0] in ("openstack", "pyVmomi", "pyVim"):
            raise ImportError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)

    for mod in [m for m in sys.modules if m.split(".")[0] in ("openstack", "pyVmomi", "pyVim")]:
        monkeypatch.delitem(sys.modules, mod)
    monkeypatch.setattr(builtins, "__import__", fake_import)

    import asyncio

    with pytest.raises(ProviderError, match="openstack"):
        asyncio.run(OpenStackProvider(make_provider(), settings()).check())
    with pytest.raises(ProviderError, match="vmware"):
        asyncio.run(
            VMwareProvider(
                make_provider(id="vc", kind=ProviderKind.vmware, credentials_secret="x"),
                settings(),
            ).check()
        )


class PoweredOffVM(VirtualMachine):
    def __init__(self, state="poweredOff", task_state="success"):
        base = fake_vcenter_vm()
        self.__dict__.update(base.__dict__)
        self.runtime = NS(powerState=state, host=NS(name="esx-01.dc2"))
        self.tasks = []
        self._task_state = task_state

    def PowerOnVM_Task(self):  # noqa: N802 - pyVmomi naming
        task = NS(info=NS(state=self._task_state, error="host in maintenance"))
        self.tasks.append(task)
        return task


async def test_vmware_os_type_prefers_the_tools_pretty_name(tmp_path):
    """SDD §10: VMware Tools report the exact release; the guest id only names the family."""
    secret_dir = tmp_path / "secrets" / "vcenter-dc2"
    secret_dir.mkdir(parents=True)
    (secret_dir / "username").write_text("u\n")
    (secret_dir / "password").write_text("p\n")
    with_tools = fake_vcenter_vm()
    with_tools.config.guestId = "ubuntu64Guest"
    with_tools.guest.guestDetailedData = (
        "architecture='X86' bitness='64' distroName='Ubuntu' distroVersion='22.04' "
        "familyName='Linux' kernelVersion='5.15.0-105-generic' prettyName='Ubuntu 22.04.4 LTS'"
    )
    without = fake_vcenter_vm()
    without.name = "dc2-db-02"
    without._moId = "vm-102"
    without.config.instanceUuid = "5012-ef01"
    without.config.guestId = "ubuntu64Guest"
    provider = VMwareProvider(
        make_provider(
            id="vcenter",
            kind=ProviderKind.vmware,
            credentials_secret="vcenter-dc2",
            endpoint="https://vcenter.dc2.example/sdk",
            cloud=None,
        ),
        settings(secrets_dir=tmp_path / "secrets"),
        connector=lambda *a, **kw: fake_service_instance([with_tools, without]),
        vim=FAKE_VIM,
    )
    first, second = await provider.list_vms()
    assert first.os_type == "Ubuntu 22.04.4 LTS" and first.guest_os.label == "Ubuntu 22.04"
    assert second.os_type == "ubuntu64Guest" and second.guest_os.label == "Ubuntu"


async def test_openstack_os_type_from_volume_or_image_properties(stub_openstack):
    conn = fake_conn()
    server = conn.compute.get_server("srv-1")
    server.metadata = {"os_type": "linux", "app": "shop"}  # generic: the image is more specific
    conn.block_storage.get_volume("vol-root").volume_image_metadata = {
        "os_distro": "ubuntu",
        "os_version": "22.04",
        "hw_firmware_type": "uefi",
    }
    stub_openstack(conn)
    [vm] = await OpenStackProvider(make_provider(), settings()).list_vms()
    assert vm.os_type == "ubuntu 22.04" and vm.guest_os.lifecycle == "current"

    # an image-booted server: the Glance image's properties
    conn = fake_conn()
    server = conn.compute.get_server("srv-1")
    server.metadata = {}
    server.image = {"id": "img-1"}
    server.attachments = []
    conn.image = NS(
        get_image=lambda image_id: NS(id=image_id, os_distro="windows", os_version="2022")
    )
    stub_openstack(conn)
    [vm] = await OpenStackProvider(make_provider(), settings()).list_vms()
    assert vm.os_type == "windows 2022" and vm.guest_os.label == "Windows Server 2022"


async def test_vmware_power_on_and_inventory(tmp_path):
    secret_dir = tmp_path / "secrets" / "vcenter-dc2"
    secret_dir.mkdir(parents=True)
    (secret_dir / "username").write_text("u")
    (secret_dir / "password").write_text("p")
    vm = PoweredOffVM()

    def provider_for(vms):
        return VMwareProvider(
            make_provider(
                id="vcenter", kind=ProviderKind.vmware, credentials_secret="vcenter-dc2", cloud=None
            ),
            settings(secrets_dir=tmp_path / "secrets"),
            connector=lambda **_: fake_service_instance(vms),
            vim=FAKE_VIM,
        )

    await provider_for([vm]).power_on("5012-abcd")
    assert len(vm.tasks) == 1
    running = PoweredOffVM(state="poweredOn")
    await provider_for([running]).power_on("5012-abcd")
    assert running.tasks == [], "already running: nothing to do"
    with pytest.raises(ProviderError, match="power-on failed"):
        await provider_for([PoweredOffVM(task_state="error")]).power_on("5012-abcd")
    with pytest.raises(ProviderError, match="not found"):
        await provider_for([vm]).power_on("missing")
    with pytest.raises(VMNotFound, match="not found"):
        await provider_for([vm]).get_vm("missing")
    inv = await provider_for([vm]).inventory()
    assert inv.networks == {} and inv.projects == []  # the fake vim has no Network type


async def test_vmware_tells_whether_vcenter_recorded_a_power_on_since_a_time(tmp_path):
    """SDD §7.2, §10 (review RV30-03): powered_on_since asks vCenter's event manager for the VM's
    power-on, DRS power-on and resume events since the time - its power state cannot tell a VM
    that ran and was shut down again."""
    from datetime import UTC, datetime

    secret_dir = tmp_path / "secrets" / "vcenter-dc2"
    secret_dir.mkdir(parents=True)
    (secret_dir / "username").write_text("u")
    (secret_dir / "password").write_text("p")
    vm = PoweredOffVM()
    since = datetime(2026, 10, 17, 9, 0, tzinfo=UTC)
    queries: list = []
    found: list = []  # the events vCenter answers with

    class EventFilterSpec:
        def __init__(self, entity, time, eventTypeId):  # noqa: N803 - pyVmomi naming
            self.entity, self.time, self.event_type_ids = entity, time, eventTypeId

    EventFilterSpec.ByEntity = lambda entity, recursion: NS(entity=entity, recursion=recursion)
    EventFilterSpec.ByTime = lambda beginTime: NS(begin=beginTime)  # noqa: N803

    def service_instance(**_):
        si = fake_service_instance([vm])
        si.RetrieveContent().eventManager = NS(
            QueryEvents=lambda spec: queries.append(spec) or list(found)
        )
        return si

    provider = VMwareProvider(
        make_provider(
            id="vcenter", kind=ProviderKind.vmware, credentials_secret="vcenter-dc2", cloud=None
        ),
        settings(secrets_dir=tmp_path / "secrets"),
        connector=service_instance,
        vim=NS(**vars(FAKE_VIM), event=NS(EventFilterSpec=EventFilterSpec)),
    )
    assert await provider.powered_on_since("5012-abcd", since) is False
    found.append(NS(createdTime=since))
    assert await provider.powered_on_since("5012-abcd", since) is True
    spec = queries[0]
    assert (spec.entity.entity, spec.entity.recursion, spec.time.begin) == (vm, "self", since)
    assert set(spec.event_type_ids) == {"VmPoweredOnEvent", "DrsVmPoweredOnEvent", "VmResumedEvent"}
    with pytest.raises(ProviderError, match="not found"):
        await provider.powered_on_since("missing", since)


async def test_vmware_lists_a_vms_disk_keys_in_vcenters_device_order(tmp_path):
    """SDD §7.2, §10 (reviews RV32A-01, RV32A-02): disk_keys answers the keys of the VM's disks in
    vCenter's device order - config.hardware.device, as the migration kit reads them to choose the
    disk it converts first -, never sorted, and no other device's."""
    secret_dir = tmp_path / "secrets" / "vcenter-dc2"
    secret_dir.mkdir(parents=True)
    (secret_dir / "username").write_text("u")
    (secret_dir / "password").write_text("p")
    vm = fake_vcenter_vm()
    first, second, nic = vm.config.hardware.device
    vm.config.hardware.device = [second, nic, first]  # vCenter lists the higher key first
    provider = provider_for_plain(tmp_path, vm)
    assert await provider.disk_keys("5012-abcd") == [2001, 2000]
    with pytest.raises(ProviderError, match="not found"):
        await provider.disk_keys("missing")


async def test_the_demo_source_lists_its_vms_disk_keys_as_vcenter_numbers_them():
    """SDD §10: the demo's VMware VMs answer disk_keys as vCenter numbers a VM's disks, from 2000
    in their order - the names the kit gives their volumes (SDD §7.2)."""
    source = FakeSourceProvider(ProviderKind.vmware, seed=42)
    for vm in await source.list_vms():
        assert await source.disk_keys(vm.source_id) == [2000 + i for i in range(len(vm.disks))]
    with pytest.raises(VMNotFound):
        await source.disk_keys("missing")


async def test_vmware_get_vm_and_power_on_use_the_uuid_index(tmp_path):
    """One FindByUuid call instead of a walk over every VM in the vCenter."""
    secret_dir = tmp_path / "secrets" / "vcenter-dc2"
    secret_dir.mkdir(parents=True)
    (secret_dir / "username").write_text("u")
    (secret_dir / "password").write_text("p")
    vm = PoweredOffVM()
    lookups: list[tuple] = []
    walked: list[str] = []

    def service_instance(**_):
        si = fake_service_instance([vm])
        content = si.RetrieveContent()
        real_view = content.viewManager.CreateContainerView

        def counted_view(folder, types, recursive):
            walked.append(str(types))
            return real_view(folder, types, recursive)

        content.viewManager = NS(CreateContainerView=counted_view)
        content.searchIndex = NS(
            FindByUuid=lambda dc, uuid, is_vm, instance: (
                lookups.append((uuid, is_vm, instance)) or (vm if uuid == "5012-abcd" else None)
            )
        )
        return si

    provider = VMwareProvider(
        make_provider(
            id="vcenter", kind=ProviderKind.vmware, credentials_secret="vcenter-dc2", cloud=None
        ),
        settings(secrets_dir=tmp_path / "secrets"),
        connector=service_instance,
        vim=FAKE_VIM,
    )
    found = await provider.get_vm("5012-abcd")
    assert found.source_id == "5012-abcd" and lookups == [("5012-abcd", True, True)]
    assert not any("VirtualMachine" in t for t in walked), "no inventory walk for a lookup"
    await provider.power_on("5012-abcd")
    assert len(vm.tasks) == 1 and len(lookups) == 2
    assert not any("VirtualMachine" in t for t in walked)
    with pytest.raises(VMNotFound, match="not found"):
        await provider.get_vm("missing")  # falls back to the walk, then fails


def _vcenter(tmp_path, vms, find=None):
    """A VMwareProvider over ``vms``; ``find`` replaces the uuid index (None: no index at all)."""
    secret_dir = tmp_path / "secrets" / "vcenter-dc2"
    secret_dir.mkdir(parents=True, exist_ok=True)
    (secret_dir / "username").write_text("u")
    (secret_dir / "password").write_text("p")

    def service_instance(**_):
        si = fake_service_instance(vms)
        content = si.RetrieveContent()
        content.searchIndex = NS(FindByUuid=find) if find else NS()
        return si

    return VMwareProvider(
        make_provider(
            id="vcenter", kind=ProviderKind.vmware, credentials_secret="vcenter-dc2", cloud=None
        ),
        settings(secrets_dir=tmp_path / "secrets"),
        connector=service_instance,
        vim=FAKE_VIM,
    )


async def test_vmware_get_vm_never_calls_a_vm_gone_while_one_it_cannot_read_may_be_it(tmp_path):
    """SDD §10 (review RV3-04): when the uuid index cannot answer (a fault, or none) the walk over
    every VM decides - and a VM whose configuration vCenter cannot read may be the one sought, so
    "not found" is no answer: a ProviderError, which leaves the source-running alert as it is."""
    unreadable = PoweredOffVM(state="poweredOn")
    unreadable.config = None

    def faulting(dc, uuid, is_vm, instance):
        raise RuntimeError("vmodl.fault.SystemError")

    for find in (faulting, None):
        provider = _vcenter(tmp_path, [unreadable], find=find)
        with pytest.raises(ProviderError, match="cannot be read") as failed:
            await provider.get_vm("5012-abcd")
        assert not isinstance(failed.value, VMNotFound), find
    # addressed by its moId, the unreadable VM is the one sought: there, not gone
    with pytest.raises(ProviderError, match="configuration is unavailable") as failed:
        await _vcenter(tmp_path, [unreadable]).get_vm("vm-101")
    assert not isinstance(failed.value, VMNotFound)


async def test_vmware_get_vm_without_an_index_still_calls_a_vm_gone_once_it_read_every_vm(
    tmp_path,
):
    """SDD §10: with every VM readable, the walk is vCenter's answer: no such VM is VMNotFound."""

    def faulting(dc, uuid, is_vm, instance):
        raise RuntimeError("vmodl.fault.SystemError")

    provider = _vcenter(tmp_path, [PoweredOffVM(state="poweredOn")], find=faulting)
    assert (await provider.get_vm("5012-abcd")).source_id == "5012-abcd"
    with pytest.raises(VMNotFound, match="not found"):
        await provider.get_vm("missing")


async def test_vmware_get_vm_of_an_inaccessible_vm_is_not_vm_not_found(tmp_path):
    """SDD §10 (review RV-06): vCenter leaves a VM's config unset while its files are unreachable
    (a datastore outage) - FindByUuid still finds it: the VM is there, not gone. A VMNotFound would
    let the source watch clear the running flag of a VM that exists."""
    secret_dir = tmp_path / "secrets" / "vcenter-dc2"
    secret_dir.mkdir(parents=True)
    (secret_dir / "username").write_text("u")
    (secret_dir / "password").write_text("p")
    vm = PoweredOffVM(state="poweredOn")
    vm.config = None

    def service_instance(**_):
        si = fake_service_instance([vm])
        content = si.RetrieveContent()
        content.searchIndex = NS(
            FindByUuid=lambda dc, uuid, is_vm, instance: vm if uuid == "5012-abcd" else None
        )
        return si

    provider = VMwareProvider(
        make_provider(
            id="vcenter", kind=ProviderKind.vmware, credentials_secret="vcenter-dc2", cloud=None
        ),
        settings(secrets_dir=tmp_path / "secrets"),
        connector=service_instance,
        vim=FAKE_VIM,
    )
    with pytest.raises(ProviderError, match="configuration is unavailable") as failed:
        await provider.get_vm("5012-abcd")
    assert not isinstance(failed.value, VMNotFound)
    with pytest.raises(VMNotFound, match="not found"):
        await provider.get_vm("missing")  # vCenter has no such VM: gone


async def test_vmware_used_bytes_come_from_the_disk_layout(tmp_path):
    """Thin disks: the extent files behind a disk's chain are its used space (no 60 % rule)."""
    secret_dir = tmp_path / "secrets" / "vcenter-dc2"
    secret_dir.mkdir(parents=True)
    (secret_dir / "username").write_text("u")
    (secret_dir / "password").write_text("p")
    vm = fake_vcenter_vm()
    vm.layoutEx = NS(
        file=[
            NS(key=0, size=700),  # descriptor
            NS(key=1, size=12 * GIB),  # Hard disk 1 extent
            NS(key=2, size=3 * GIB),  # Hard disk 1 snapshot delta
            NS(key=3, size=100 * GIB),  # Hard disk 2 extent
        ],
        disk=[
            NS(key=2000, chain=[NS(fileKey=[0, 1]), NS(fileKey=[2])]),
            NS(key=2001, chain=[NS(fileKey=[3])]),
        ],
    )
    provider = VMwareProvider(
        make_provider(
            id="vcenter", kind=ProviderKind.vmware, credentials_secret="vcenter-dc2", cloud=None
        ),
        settings(secrets_dir=tmp_path / "secrets"),
        connector=lambda **_: fake_service_instance([vm]),
        vim=FAKE_VIM,
    )
    [mapped] = await provider.list_vms()
    by_name = {d.name: d for d in mapped.disks}
    assert by_name["Hard disk 1"].used_gb == 15.0 and by_name["Hard disk 2"].used_gb == 100.0
    assert mapped.used_bytes == 115 * GIB
    # without a layout the used size stays unknown (estimator fallback)
    plain = (await provider_for_plain(tmp_path, fake_vcenter_vm()).list_vms())[0]
    assert all(d.used_gb is None for d in plain.disks)


def provider_for_plain(tmp_path, vm):
    return VMwareProvider(
        make_provider(
            id="vcenter", kind=ProviderKind.vmware, credentials_secret="vcenter-dc2", cloud=None
        ),
        settings(secrets_dir=tmp_path / "secrets"),
        connector=lambda **_: fake_service_instance([vm]),
        vim=FAKE_VIM,
    )


async def test_vmware_pci_and_vgpu_devices_become_blocking_extra_specs(tmp_path):
    """SDD §9.3 VM_PCI_PASSTHROUGH / VM_VGPU key off extra specs: the provider reports the
    VMware device classes that way."""
    secret_dir = tmp_path / "secrets" / "vcenter-dc2"
    secret_dir.mkdir(parents=True)
    (secret_dir / "username").write_text("u")
    (secret_dir / "password").write_text("p")

    class VirtualPCIPassthrough:
        def __init__(self, label, vgpu=None):
            self.key = 13000
            self.deviceInfo = NS(label=label)
            self.backing = NS(vgpu=vgpu)

    vim = NS(
        VirtualMachine=VirtualMachine,
        vm=NS(
            device=NS(
                VirtualDisk=VirtualDisk,
                VirtualEthernetCard=VirtualEthernetCard,
                VirtualPCIPassthrough=VirtualPCIPassthrough,
            )
        ),
    )
    pci = fake_vcenter_vm()
    pci.config.hardware.device.append(VirtualPCIPassthrough("PCI device 0"))
    vgpu = fake_vcenter_vm()
    vgpu.config.instanceUuid = "5012-vgpu"
    vgpu.config.hardware.device.append(VirtualPCIPassthrough("PCI device 1", vgpu="grid_t4-8q"))

    def provider_for(vms):
        return VMwareProvider(
            make_provider(
                id="vcenter", kind=ProviderKind.vmware, credentials_secret="vcenter-dc2", cloud=None
            ),
            settings(secrets_dir=tmp_path / "secrets"),
            connector=lambda **_: fake_service_instance(vms),
            vim=vim,
        )

    listed = {v.source_id: v for v in await provider_for([pci, vgpu]).list_vms()}
    assert listed["5012-abcd"].flavor_extra_specs == {"pci_passthrough:alias": "PCI device 0"}
    assert listed["5012-vgpu"].flavor_extra_specs == {"resources:VGPU": "1"}
    plain = await provider_for([fake_vcenter_vm()]).list_vms()
    assert plain[0].flavor_extra_specs == {}


async def test_vmware_requires_credentials_secret():
    provider = VMwareProvider(
        make_provider(id="vc", kind=ProviderKind.vmware, cloud=None),
        settings(),
        connector=lambda **_: None,
        vim=FAKE_VIM,
    )
    with pytest.raises(ProviderError, match="credentials_secret"):
        await provider.list_vms()


# --------------------------------------------------------------------------------------------
# Edge paths of the real providers (stubbed): connection setup, quota, console, lookups


def test_openstack_connect_builds_kwargs_from_clouds_yaml(monkeypatch, tmp_path):
    from seamless_migrate.providers import openstack as osp

    calls = {}
    monkeypatch.setattr(
        osp, "_import_openstack", lambda: NS(connect=lambda **kw: calls.update(kw) or "conn")
    )
    clouds = tmp_path / "clouds.yaml"
    clouds.write_text(
        "clouds:\n  src:\n    auth: {auth_url: 'https://src/v3', username: u, password: p}\n"
        "    region_name: regionOne\n"
    )
    cfg = settings(clouds_yaml=clouds)
    provider = make_provider(cloud="src", region="regionTwo", ca_cert_path="/etc/pki/ca.pem")
    assert osp.connect(provider, cfg) == "conn"
    assert calls["auth"]["username"] == "u" and calls["load_yaml_config"] is False
    assert calls["region_name"] == "regionTwo", "Provider.region wins over clouds.yaml"
    assert calls["cacert"] == "/etc/pki/ca.pem" and calls["verify"] is True
    # unknown cloud in clouds.yaml -> ProviderError; no clouds.yaml -> openstacksdk's own lookup
    with pytest.raises(ProviderError, match="clouds.yaml"):
        osp.connect(make_provider(cloud="nope"), cfg)
    calls.clear()
    osp.connect(make_provider(cloud="src"), settings())
    assert calls["cloud"] == "src" and "auth" not in calls
    with pytest.raises(ProviderError, match="no 'cloud'"):
        osp.connect(make_provider(cloud=None), settings())
    assert osp._truthy("Yes") and osp._truthy(" true ") and not osp._truthy("no")
    assert osp._truthy(1) and not osp._truthy(0)


#: server names Nova reads differently as a regular expression - a group, a quantifier, an
#: alternation, a class, an escape, anchors, any character - and names that hold no such character
NOVA_NAMES = [
    "app (blue)",
    "db+1",
    "web(1",
    "a|b",
    "a|",
    "x?",
    "web[2]",
    "c:\\temp",
    "^lead",
    "tail$",
    "dot.name",
    "web-01",
    "em\u2014dash (2)",
    "\u65e5\u672c-\u30b5\u30fc\u30d0",
]


@pytest.mark.parametrize("nova_db", ["postgresql", "mysql"])
@pytest.mark.parametrize("name", NOVA_NAMES)
async def test_openstack_find_server_finds_a_name_nova_reads_as_a_regular_expression(
    tmp_path, name, nova_db
):
    """Nova reads the ``name`` filter of its server list as a regular expression searched anywhere
    in the name - on MySQL/MariaDB after escaping ``|`` itself, on PostgreSQL as sent - and refuses
    one that is none with 400: the lookup finds the server named exactly so, never one whose name
    holds it or differs where the pattern reads any character (SDD §10, review PV-01)."""
    servers = [
        {"id": "srv-1", "name": name},
        {"id": "srv-2", "name": "unrelated"},
        {"id": "srv-3", "name": f"{name}-old"},
        {"id": "srv-4", "name": f"old-{name}"},
    ]
    similar = "".join("Z" if c.isascii() and not (c.isalnum() or c in " _-") else c for c in name)
    if similar != name:
        servers.append({"id": "srv-5", "name": similar})
    cloud = InProcessCloud(tmp_path, servers=servers, nova_db=nova_db)
    dst = make_provider(id="dst", role=ProviderRole.destination, endpoint=AUTH_URL, cloud="dst")
    provider = OpenStackProvider(dst, cloud.settings, connect_fn=cloud.connect)

    assert await provider.find_server(name) == "srv-1"


async def test_openstack_find_server_sends_an_anchored_pattern_and_refuses_two_of_a_name(tmp_path):
    """The lookup sends Nova ``^...$`` with every ASCII character but a letter, a digit, a space,
    ``_`` and ``-`` as ``.``: no alternation, group, class or escape on any backend. No server of
    the name is None; two are a provider error (SDD §10, review PV-01)."""
    import urllib.parse

    cloud = InProcessCloud(
        tmp_path,
        servers=[{"id": "srv-1", "name": "app (blue)_1-a"}, {"id": "srv-2", "name": "web-01"}],
    )
    dst = make_provider(id="dst", role=ProviderRole.destination, endpoint=AUTH_URL, cloud="dst")
    provider = OpenStackProvider(dst, cloud.settings, connect_fn=cloud.connect)

    assert await provider.find_server("app (blue)_1-a") == "srv-1"
    assert await provider.find_server("absent") is None
    # no metacharacter outside ASCII
    assert await provider.find_server("caf\u00e9\u2014(2)") is None
    sent = [
        urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["name"][0]
        for method, url in cloud.seen
        if method == "GET" and "name=" in url
    ]
    assert sent == ["^app .blue._1-a$", "^absent$", "^caf\u00e9\u2014.2.$"]
    cloud.servers.append({"id": "srv-3", "name": "web-01"})
    with pytest.raises(ProviderError, match=r"^dst: more than one server is named 'web-01'$"):
        await provider.find_server("web-01")


async def test_openstack_destination_ports_match_every_filter_and_delete_is_idempotent():
    """SDD §10 (§7.2 VMware networks, review RT-11): ports() returns the ports matching every
    filter given, checked again here in case the server ignores one, with the MAC compared in
    lower case and an address in its canonical form; delete_port takes a port already gone as
    deleted."""
    queries, deleted = [], []

    def port(port_id, mac, ips, network="net-1", device="", name=""):
        return NS(
            id=port_id,
            name=name,
            network_id=network,
            device_id=device,
            mac_address=mac,
            fixed_ips=[{"subnet_id": "sub-1", "ip_address": ip} for ip in ips],
        )

    everything = [
        port("p1", "00:50:56:00:00:01", ["10.0.0.5"], name="web-01-NIC-0-VLAN-app-net"),
        port("p2", "fa:16:3e:00:00:02", ["10.0.0.6", "fd00::6"], device="srv-1"),
        port("p3", "00:50:56:00:00:01", ["192.0.2.5"], network="net-2"),
    ]

    class Network:
        def ports(self, **query):
            queries.append(query)
            return list(everything)  # a server that ignores every filter

        def delete_port(self, port_id, ignore_missing=True):
            deleted.append((port_id, ignore_missing))

    dst = make_provider(
        id="dst", kind=ProviderKind.rhoso, role=ProviderRole.destination, cloud="dst"
    )
    provider = OpenStackProvider(dst, settings(), connect_fn=lambda p, s: NS(network=Network()))
    assert await provider.ports(network_id="net-1", mac_address="00:50:56:00:00:01") == [
        {
            "id": "p1",
            "name": "web-01-NIC-0-VLAN-app-net",
            "network_id": "net-1",
            "device_id": "",
            "mac_address": "00:50:56:00:00:01",
            "fixed_ips": ["10.0.0.5"],
        }
    ]
    assert queries[-1] == {"network_id": "net-1", "mac_address": "00:50:56:00:00:01"}
    assert [p["id"] for p in await provider.ports(mac_address="00:50:56:00:00:01")] == ["p1", "p3"]
    # Neutron keeps a MAC in lower case: one asked for in upper case is the same MAC
    assert [p["id"] for p in await provider.ports(mac_address="FA:16:3E:00:00:02")] == ["p2"]
    assert queries[-1] == {"mac_address": "fa:16:3e:00:00:02"}
    assert [p["id"] for p in await provider.ports(ip_address="fd00:0::6")] == ["p2"]
    assert queries[-1] == {"fixed_ips": ["ip_address=fd00::6"]}
    assert [p["id"] for p in await provider.ports()] == ["p1", "p2", "p3"]
    await provider.delete_port("p1")
    assert deleted == [("p1", True)]


async def test_openstack_destination_volumes_and_a_delete_that_waits_for_the_volume(
    monkeypatch,
):
    """SDD §10 (§7.2 VMware rollback, review RV28-01): volumes() lists the project's volumes with
    their metadata and the servers holding them; delete_volume waits while a volume still detaches
    from the server just deleted (Cinder refuses to delete it meanwhile), deletes it and waits
    until Cinder no longer has it, within the call's bound; a volume already gone is no error, and
    one that does not settle in time is a provider error, never a delete Cinder refuses."""
    from seamless_migrate.providers import openstack as osp

    clock = [0.0]

    def tick():
        clock[0] += 1.0
        return clock[0]

    monkeypatch.setattr(osp, "time", NS(monotonic=tick, sleep=lambda s: None))
    statuses = {"v1": ["in-use", "detaching", "available"], "stuck": ["detaching"]}
    calls = []

    def volume(volume_id, name, status, metadata=None, holders=()):
        return NS(
            id=volume_id,
            name=name,
            status=status,
            metadata=metadata or {},
            attachments=[{"server_id": s, "attachment_id": f"a-{s}"} for s in holders],
        )

    class BlockStorage:
        def volumes(self, details=True, **query):
            calls.append(("volumes", details, query))
            return [
                volume("v1", "web-01-2000", "in-use", {"osm": "true", "converted": "true"}, ["d9"]),
                volume("v2", None, "available"),
            ]

        def find_volume(self, volume_id, ignore_missing=True):
            calls.append(("find_volume", volume_id))
            left = statuses.get(volume_id)
            if not left:
                return None
            return volume(volume_id, "x", left.pop(0) if len(left) > 1 else left[0])

        def delete_volume(self, volume, ignore_missing=True):
            assert volume.status == "available", volume.status
            calls.append(("delete_volume", volume.id, ignore_missing))

        def wait_for_delete(self, volume, interval=2, wait=120):
            calls.append(("wait_for_delete", volume.id, wait > 0))
            return volume

    dst = make_provider(
        id="dst", kind=ProviderKind.rhoso, role=ProviderRole.destination, cloud="dst"
    )
    provider = OpenStackProvider(
        dst, settings(), connect_fn=lambda p, s: NS(block_storage=BlockStorage())
    )
    assert await provider.volumes() == [
        {
            "id": "v1",
            "name": "web-01-2000",
            "status": "in-use",
            "metadata": {"osm": "true", "converted": "true"},
            "attachments": ["d9"],
        },
        {"id": "v2", "name": "", "status": "available", "metadata": {}, "attachments": []},
    ]
    assert calls == [("volumes", True, {})]
    calls.clear()
    await provider.delete_volume("v1")
    assert calls == [
        ("find_volume", "v1"),
        ("find_volume", "v1"),
        ("find_volume", "v1"),
        ("delete_volume", "v1", True),
        ("wait_for_delete", "v1", True),
    ]
    calls.clear()
    await provider.delete_volume("gone")
    assert calls == [("find_volume", "gone")]
    started = clock[0]
    with pytest.raises(ProviderError, match="volume stuck is still detaching"):
        await provider.delete_volume("stuck")
    assert ("delete_volume", "stuck", True) not in calls
    assert clock[0] - started < osp.PROVIDER_CALL_TIMEOUT_S, "the wait outlived the call's bound"


def test_the_inventory_looks_up_a_flavor_nova_did_not_embed():
    """SDD §4.2, §10 (review RV28-08): Nova embeds the server's flavor - its original_name,
    vCPUs, RAM, disks - from compute 2.47 on; below it (or with a clouds.yaml that pins an older
    compute microversion) the server holds only the flavor's id. openstacksdk's Flavor then reads
    0 for every field Nova left out, never None, so the inventory looks the flavor up by its id
    whenever no original_name was embedded - and reads its ephemeral and swap disks from it too."""
    from openstack.compute.v2.flavor import Flavor
    from openstack.compute.v2.server import Server

    from seamless_migrate.providers.openstack import _MapContext

    looked = []

    def get_flavor(flavor_id, get_extra_specs=False):
        looked.append(flavor_id)
        return Flavor(id=flavor_id, name="m1.big", vcpus=4, ram=8192, disk=40, ephemeral=10)

    conn = NS(
        compute=NS(get_flavor=get_flavor, volume_attachments=lambda server: []),
        network=NS(ports=lambda **kw: [], get_network=lambda network_id: None),
        identity=NS(get_project=lambda project_id: None),
        image=NS(get_image=lambda image_id: None),
    )
    ctx = _MapContext(conn)
    body = {"id": "srv-1", "name": "old-01", "status": "ACTIVE", "image": {"id": "img-1"}}
    body["flavor"] = {"id": "flv-7", "links": []}
    for server in (dict(body), Server(**body)):
        looked.clear()
        vm = ctx.server_to_vmref(server)
        assert (looked, vm.vcpus, vm.ram_mb, vm.flavor) == (["flv-7"], 4, 8192, "m1.big")
        assert [(d.kind, d.size_gb) for d in vm.disks] == [("image_root", 40), ("ephemeral", 10)]
    # an embedded flavor (2.47+) is read as it is: no lookup
    looked.clear()
    embedded = {"original_name": "m1.small", "vcpus": 2, "ram": 2048, "disk": 20, "swap": 0}
    vm = ctx.server_to_vmref(Server(**{**body, "flavor": embedded}))
    assert (looked, vm.vcpus, vm.flavor) == ([], 2, "m1.small")

    # a flavor deleted since: the server keeps what Nova gave of it, the inventory goes on
    def gone(flavor_id, get_extra_specs=False):
        raise RuntimeError(f"HTTP 404: flavor {flavor_id} could not be found")

    conn.compute.get_flavor = gone
    assert ctx.server_to_vmref(Server(**body)).vcpus == 0


def _looked_up_flavors(forbidden=False):
    """openstacksdk's get_flavor over a Nova below 2.61: a flavor without extra specs, which
    get_extra_specs=True or Flavor.fetch_extra_specs reads from os-extra_specs - refused (403)
    where the policy forbids the credential to read them."""
    import openstack.exceptions
    from openstack.compute.v2.flavor import Flavor

    class LookedUp(Flavor):
        def fetch_extra_specs(self, session):
            if forbidden:
                raise openstack.exceptions.ForbiddenException(
                    "Policy doesn't allow os_compute_api:os-flavor-extra-specs:index"
                )
            self._update(extra_specs={"resources:VGPU": "1"})
            return self

    def get_flavor(flavor_id, get_extra_specs=False):
        flavor = LookedUp(id=flavor_id, name="g1.large", vcpus=8, ram=16384)
        return flavor.fetch_extra_specs(None) if get_extra_specs else flavor

    return get_flavor


def test_the_inventory_reads_the_extra_specs_of_a_flavor_it_looked_up():
    """SDD §4.2, §9.3 (review RV31-04): below compute 2.61 Nova's flavor holds no extra specs -
    openstacksdk fetches them (os-extra_specs) only when asked. A flavor the inventory looks up
    because Nova did not embed it (below 2.47) is asked for them, so a vGPU or PCI-passthrough
    VM still carries its flavor_extra_specs - and gets its VM_VGPU or VM_PCI_PASSTHROUGH blocker."""
    from openstack.compute.v2.server import Server

    from seamless_migrate.providers.openstack import _MapContext

    get_flavor = _looked_up_flavors()

    conn = NS(
        compute=NS(get_flavor=get_flavor, volume_attachments=lambda server: []),
        network=NS(ports=lambda **kw: [], get_network=lambda network_id: None),
        identity=NS(get_project=lambda project_id: None),
        image=NS(get_image=lambda image_id: None),
    )
    body = {"id": "srv-1", "name": "gpu-01", "status": "ACTIVE", "image": {"id": "img-1"}}
    vm = _MapContext(conn).server_to_vmref(Server(**body, flavor={"id": "flv-gpu", "links": []}))
    assert (vm.flavor, vm.flavor_extra_specs) == ("g1.large", {"resources:VGPU": "1"})


def test_the_inventory_keeps_a_looked_up_flavor_whose_extra_specs_it_may_not_read():
    """SDD §4.2 (review RV32B-03): a policy may forbid the credential to read flavor extra specs
    (os-extra_specs answers 403). The flavor the inventory looked up is then kept without them -
    its vCPUs, RAM and name - as Nova 2.47+ leaves the specs out under that policy; never a VM read
    as 0 vCPUs and no flavor."""
    from openstack.compute.v2.server import Server

    from seamless_migrate.providers.openstack import _MapContext

    conn = NS(
        compute=NS(
            get_flavor=_looked_up_flavors(forbidden=True), volume_attachments=lambda server: []
        ),
        network=NS(ports=lambda **kw: [], get_network=lambda network_id: None),
        identity=NS(get_project=lambda project_id: None),
        image=NS(get_image=lambda image_id: None),
    )
    body = {"id": "srv-1", "name": "gpu-01", "status": "ACTIVE", "image": {"id": "img-1"}}
    vm = _MapContext(conn).server_to_vmref(Server(**body, flavor={"id": "flv-gpu", "links": []}))
    assert (vm.vcpus, vm.ram_mb, vm.flavor) == (8, 16384, "g1.large")
    assert not vm.flavor_extra_specs


@pytest.mark.parametrize(
    "case", ["deleting", "began meanwhile", "error_deleting", "gone", "maintenance"]
)
async def test_openstack_delete_volume_waits_for_a_deletion_cinder_runs(case):
    """SDD §10 (review RV29-04): Cinder's delete takes only an available, error, error_restoring,
    error_extending or error_managing volume and refuses any other with 400. A volume it is
    already deleting - an earlier call's deletion outlived that call, or another delete began
    between the read and this one - is waited for until it is gone, never deleted a second time;
    one it failed to delete (error_deleting) is a provider error that names it."""
    from openstack import exceptions

    statuses = {
        "v1": {
            "deleting": "deleting",
            "began meanwhile": "available",
            "error_deleting": "error_deleting",
            "gone": None,
            "maintenance": "maintenance",
        }[case]
    }
    reads, deletes = [0], []

    class BlockStorage:
        def find_volume(self, volume_id, ignore_missing=True):
            reads[0] += 1
            status = statuses.get(volume_id)
            if status == "deleting" and reads[0] > 3:
                statuses[volume_id] = status = None  # the backend is done
            return None if status is None else NS(id=volume_id, status=status)

        def delete_volume(self, volume, ignore_missing=True):
            deletes.append(volume.id)
            if case == "began meanwhile":
                statuses[volume.id] = "deleting"  # another delete landed first
            if statuses[volume.id] not in ("available", "error", "error_restoring"):
                raise exceptions.BadRequestException(
                    "400: Invalid volume: Volume status must be available or error"
                )
            statuses[volume.id] = "deleting"

        def wait_for_delete(self, volume, interval=2, wait=120):
            while self.find_volume(volume.id) is not None:
                pass
            return volume

    dst = make_provider(
        id="dst", kind=ProviderKind.rhoso, role=ProviderRole.destination, cloud="dst"
    )
    provider = OpenStackProvider(
        dst, settings(), connect_fn=lambda p, s: NS(block_storage=BlockStorage())
    )
    if case == "error_deleting":
        with pytest.raises(ProviderError, match="volume v1 is error_deleting"):
            await provider.delete_volume("v1")
        assert deletes == []
        return
    if case == "maintenance":  # a refusal of its own: no deletion runs, the error stands
        with pytest.raises(ProviderError, match="400: Invalid volume"):
            await provider.delete_volume("v1")
        assert deletes == ["v1"]
        return
    await provider.delete_volume("v1")
    assert statuses["v1"] is None
    assert deletes == (["v1"] if case == "began meanwhile" else [])


def _detaching(monkeypatch, reads, calls, *, step=0.0):
    """An OpenStackProvider whose Cinder answers ``reads`` (the volume, read after read) and whose
    clock advances ``step`` seconds a look; ``calls`` records what it asked."""
    from openstack import exceptions

    from seamless_migrate.providers import openstack as osp

    answered = [0]
    clock = [0.0]

    class Compute:
        def delete_volume_attachment(self, server, volume, ignore_missing=True):
            calls.append(("delete_volume_attachment", server, volume, ignore_missing))

    class BlockStorage:
        def get_volume(self, volume_id):
            calls.append(("get_volume", volume_id))
            answered[0] += 1
            return reads[min(answered[0], len(reads)) - 1]  # the last answer stays

        def wait_for_status(self, volume, status="available", failures=None, interval=2, wait=None):
            # openstacksdk's wait: reads the volume until it reads the status, else times out
            for _ in range(3):
                volume = self.get_volume(volume.id)
                if volume.status == status:
                    return volume
            raise exceptions.ResourceTimeout(
                f"Timeout waiting for Volume:{volume.id} to transition to {status}"
            )

    def tick():
        clock[0] += step
        return clock[0]

    monkeypatch.setattr(osp, "time", NS(monotonic=tick, sleep=lambda s: calls.append(("sleep", s))))
    dst = make_provider(
        id="dst", kind=ProviderKind.rhoso, role=ProviderRole.destination, cloud="dst"
    )
    return OpenStackProvider(
        dst,
        settings(),
        connect_fn=lambda p, s: NS(compute=Compute(), block_storage=BlockStorage()),
    )


def _volume(status, *holders):
    return NS(id="vol-1", status=status, attachments=[{"server_id": s} for s in holders])


async def test_openstack_destination_detaches_a_volume_and_waits_until_it_is_free(monkeypatch):
    """SDD §10 (§7.2 VMware rollback, reviews RV29-02, PV-03): detach_volume removes Nova's
    attachment of the volume to the server, ignoring one already gone, and waits until Cinder no
    longer lists the server among the volume's attachments, within the call's bound."""
    calls = []
    reads = [
        _volume("in-use", "srv-conv"),
        _volume("detaching", "srv-conv"),
        _volume("available"),
    ]
    provider = _detaching(monkeypatch, reads, calls)
    await provider.detach_volume("srv-conv", "vol-1")
    assert calls == [
        ("get_volume", "vol-1"),
        ("delete_volume_attachment", "srv-conv", "vol-1", True),
        ("get_volume", "vol-1"),
        ("sleep", 2),
        ("get_volume", "vol-1"),
    ]


@pytest.mark.parametrize(
    "holders", [(), ("srv-dst",)], ids=["attached nowhere", "held by another server"]
)
async def test_openstack_detach_volume_of_a_volume_the_server_does_not_hold_is_no_error(
    monkeypatch, holders
):
    """SDD §10 (review PV-03): a volume not attached to that server - attached nowhere, or held by
    another server only - is no error, and is neither detached nor waited for: Cinder never reads
    such a volume available from this detach, so the wait ran out its whole bound, then failed."""
    calls = []
    provider = _detaching(
        monkeypatch, [_volume("in-use" if holders else "available", *holders)], calls
    )
    await provider.detach_volume("srv-conv", "vol-1")
    assert calls == [("get_volume", "vol-1")]


async def test_openstack_detach_volume_of_a_multiattach_volume_ends_once_that_server_lets_go(
    monkeypatch,
):
    """SDD §10 (review PV-03): a multi-attach volume another server still holds stays in-use after
    the detach - the wait ends once Cinder no longer lists the server, never waiting for
    available."""
    calls = []
    reads = [_volume("in-use", "srv-conv", "srv-dst"), _volume("in-use", "srv-dst")]
    provider = _detaching(monkeypatch, reads, calls)
    await provider.detach_volume("srv-conv", "vol-1")
    assert calls == [
        ("get_volume", "vol-1"),
        ("delete_volume_attachment", "srv-conv", "vol-1", True),
        ("get_volume", "vol-1"),
    ]


@pytest.mark.parametrize(
    "case, reads, message",
    [
        (
            "still held",
            [_volume("in-use", "srv-conv")] * 5,
            r"^dst: volume vol-1 is still attached to srv-conv$",
        ),
        (
            "cinder failed",
            [_volume("in-use", "srv-conv"), _volume("error", "srv-conv")],
            r"^dst: volume vol-1 is in error: Cinder failed to detach it from srv-conv$",
        ),
    ],
)
async def test_openstack_detach_volume_names_a_volume_the_server_keeps(
    monkeypatch, case, reads, message
):
    """SDD §10 (review PV-03): a volume the server still holds when the call's bound ends, or one
    Cinder reads in error, is a provider error naming the volume and the server."""
    calls = []
    provider = _detaching(monkeypatch, reads, calls, step=100.0)
    with pytest.raises(ProviderError, match=message):
        await provider.detach_volume("srv-conv", "vol-1")


async def test_openstack_destination_finds_the_server_holding_an_address():
    """SDD §10 (§7.2 VMware rollback, review RV30-06): server_of_address takes a compute port's
    device - by the address as one of its fixed IPs, else through Neutron's floating IP of that
    address - and never a floating IP's own port or a router's."""
    ports = [
        NS(
            id="conv-port",
            device_id="srv-conv",
            device_owner="compute:nova",
            fixed_ips=[{"ip_address": "10.0.0.40"}],
        ),
        NS(
            id="fip-port",
            device_id="fip-1",
            device_owner="network:floatingip",
            fixed_ips=[{"ip_address": "198.51.100.40"}],
        ),
        NS(
            id="router-port",
            device_id="router-1",
            device_owner="network:router_interface",
            fixed_ips=[{"ip_address": "10.0.0.1"}],
        ),
    ]
    floating = [NS(floating_ip_address="198.51.100.40", port_id="conv-port")]

    class Network:
        def ports(self, fixed_ips):
            address = fixed_ips[0].split("=", 1)[1]
            return [p for p in ports if address in [ip["ip_address"] for ip in p.fixed_ips]]

        def ips(self, floating_ip_address):
            return [f for f in floating if f.floating_ip_address == floating_ip_address]

        def get_port(self, port_id):
            return {p.id: p for p in ports}[port_id]

    dst = make_provider(
        id="dst", kind=ProviderKind.rhoso, role=ProviderRole.destination, cloud="dst"
    )
    provider = OpenStackProvider(dst, settings(), connect_fn=lambda p, s: NS(network=Network()))
    assert await provider.server_of_address("10.0.0.40") == "srv-conv"
    assert await provider.server_of_address("198.51.100.40") == "srv-conv"
    assert await provider.server_of_address("10.0.0.1") is None  # a router's
    assert await provider.server_of_address("203.0.113.9") is None


@pytest.mark.parametrize("read", ["503", "gone"])
async def test_openstack_destination_takes_a_failed_port_read_for_no_answer(read):
    """SDD §7.2, §10 (review RV32A-04): the port a floating IP is associated with is read through
    Neutron; a read that fails (a 503, a timeout) is no answer - a provider error, which the
    rollback retries - while a port gone since the listing is no server holding the address."""
    import openstack.exceptions

    floating = [NS(floating_ip_address="198.51.100.40", port_id="conv-port")]

    class Network:
        def ports(self, fixed_ips):
            return []

        def ips(self, floating_ip_address):
            return [f for f in floating if f.floating_ip_address == floating_ip_address]

        def get_port(self, port_id):
            if read == "gone":
                raise openstack.exceptions.NotFoundException(f"No Port found for {port_id}")
            raise openstack.exceptions.HttpException(message="Service Unavailable", http_status=503)

    dst = make_provider(
        id="dst", kind=ProviderKind.rhoso, role=ProviderRole.destination, cloud="dst"
    )
    provider = OpenStackProvider(dst, settings(), connect_fn=lambda p, s: NS(network=Network()))
    if read == "gone":
        assert await provider.server_of_address("198.51.100.40") is None
        return
    with pytest.raises(ProviderError, match="503|Service Unavailable"):
        await provider.server_of_address("198.51.100.40")


async def test_openstack_destination_reads_the_volumes_nova_lists_for_a_server():
    """SDD §10 (§7.2 VMware rollback, review RV30-04): server_volumes reads Nova's record of the
    server - no Cinder or Neutron call, so a rollback reads it before the source's power-on; a
    server gone holds none."""
    import openstack.exceptions

    class Compute:
        def get_server(self, server_id):
            if server_id == "gone":
                raise openstack.exceptions.NotFoundException("No Server found for gone")
            return NS(attached_volumes=[{"id": "vol-1"}, NS(id="vol-2")])

    dst = make_provider(
        id="dst", kind=ProviderKind.rhoso, role=ProviderRole.destination, cloud="dst"
    )
    provider = OpenStackProvider(dst, settings(), connect_fn=lambda p, s: NS(compute=Compute()))
    assert await provider.server_volumes("srv-1") == ["vol-1", "vol-2"]
    assert await provider.server_volumes("gone") == []


async def test_openstack_destination_marks_a_volume_keeping_its_other_metadata():
    """SDD §10 (§7.2 VMware, review RV30-01): set_volume_metadata sets the given keys through
    Cinder's merge (POST /volumes/{id}/metadata), so the kit's own keys - osm, changeID - stay."""
    calls = []

    class BlockStorage:
        def set_volume_metadata(self, volume, **metadata):
            calls.append(("set_volume_metadata", volume, metadata))

    dst = make_provider(
        id="dst", kind=ProviderKind.rhoso, role=ProviderRole.destination, cloud="dst"
    )
    provider = OpenStackProvider(
        dst, settings(), connect_fn=lambda p, s: NS(block_storage=BlockStorage())
    )
    await provider.set_volume_metadata("vol-1", {"seamless_copied": "true"})
    assert calls == [("set_volume_metadata", "vol-1", {"seamless_copied": "true"})]


async def test_openstack_free_quota_console_delete_and_project_lookup(tmp_path):
    from seamless_migrate.providers import openstack as osp
    from seamless_migrate.providers.openstack import OpenStackProvider, _MapContext

    conn = fake_conn()
    conn.compute.get_quota_set = lambda pid, usage=True: NS(
        cores=20, ram=65536, instances=-1, usage={"cores": 6, "ram": 4096, "instances": 3}
    )
    conn.block_storage.get_quota_set = lambda pid, usage=True: NS(
        volumes=None, gigabytes=1000, usage={"gigabytes": 1500}
    )
    free = OpenStackProvider._free_quota(conn)
    assert free == {"cores": 14, "ram_mb": 61440, "instances": -1, "volumes": None, "gigabytes": 0}
    dst = make_provider(
        id="dst", kind=ProviderKind.rhoso, role=ProviderRole.destination, cloud="dst"
    )
    provider = OpenStackProvider(dst, settings(), connect_fn=lambda p, s: conn)
    inv = await provider.inventory()
    assert inv.quotas == {"finance": free}

    # console output unavailable (HTTP 409 / policy) -> None, never an error
    def boom(server_id, length=None):
        raise RuntimeError("HTTP 409: console log not available")

    conn.compute.get_server_console_output = boom
    assert await provider.console_log("srv-1") is None
    conn.compute.get_server_console_output = lambda server_id, length=None: None
    assert await provider.console_log("srv-1") is None

    # deleting a server that is already gone is a no-op; an existing one is deleted and awaited,
    # within the call's bound: a wait past it would report a timeout while the deletion goes on
    # (SDD §8.1)
    waited = []
    conn.compute.delete_server = lambda server, ignore_missing=True: conn.compute.deleted.append(
        server.id
    )
    conn.compute.wait_for_delete = lambda server, wait=600: waited.append((server.id, wait))
    await provider.delete_server("missing")
    assert conn.compute.deleted == []
    await provider.delete_server("web-01")
    [(server_id, wait)] = waited
    assert conn.compute.deleted == ["srv-1"] and server_id == "srv-1"
    assert 0 < wait < osp.PROVIDER_CALL_TIMEOUT_S
    assert await provider.find_server("web-01") == "srv-1"
    assert await provider.find_server("nope") is None

    # project names are looked up once and tolerate identity failures
    ctx = _MapContext(conn)
    assert ctx.project_name(None) is None
    assert ctx.project_name("proj-1") == "finance" and ctx.project_name("proj-1") == "finance"
    conn.identity = NS(get_project=lambda pid: (_ for _ in ()).throw(RuntimeError("403")))
    assert _MapContext(conn).project_name("proj-2") is None


async def test_openstack_flavor_without_embedded_specs_is_fetched(stub_openstack):
    conn = fake_conn()
    server = conn.compute.get_server("srv-1")
    server.flavor = {"id": "flv-1"}  # older Nova: no embedded vcpus/ram/disk
    conn.compute.get_flavor = lambda flavor_id, get_extra_specs=False: NS(
        id=flavor_id, name="m1.small", vcpus=2, ram=4096, disk=20, extra_specs={"hw:x": "1"}
    )
    stub_openstack(conn)
    from seamless_migrate.providers.openstack import OpenStackProvider

    vm = await OpenStackProvider(make_provider(cloud="src"), settings()).get_vm("srv-1")
    assert (vm.flavor, vm.vcpus, vm.ram_mb) == ("m1.small", 2, 4096)
    assert vm.flavor_extra_specs == {"hw:x": "1"}


async def test_vmware_endpoint_dvs_portgroups_and_transport_errors(tmp_path):
    from seamless_migrate.providers.vmware import VMwareProvider, parse_endpoint

    assert parse_endpoint("vcenter.dc2.example") == ("vcenter.dc2.example", 443)
    assert parse_endpoint("https://vcenter.dc2.example:8443/sdk") == ("vcenter.dc2.example", 8443)
    with pytest.raises(ProviderError, match="invalid vCenter endpoint"):
        parse_endpoint("https://")

    secret_dir = tmp_path / "secrets" / "vcenter-dc2"
    secret_dir.mkdir(parents=True)
    (secret_dir / "username").write_text("u")
    (secret_dir / "password").write_text("p")
    base = make_provider(
        id="vcenter", kind=ProviderKind.vmware, credentials_secret="vcenter-dc2", cloud=None
    )

    # a NIC on a distributed portgroup resolves its name through the DVS view
    class Portgroup:
        def __init__(self, key, name):
            self.key, self.name = key, name

    vm = fake_vcenter_vm()
    nic = vm.config.hardware.device[-1]
    nic.backing = NS(port=NS(portgroupKey="dvportgroup-7", portKey="12"))
    nic.deviceInfo = NS(label="Network adapter 1", summary="DVSwitch: 50 2c 9e")
    vim = NS(
        VirtualMachine=VirtualMachine,
        vm=FAKE_VIM.vm,
        dvs=NS(DistributedVirtualPortgroup=Portgroup),
    )

    def connector(**_):
        objects = [vm, Portgroup("dvportgroup-7", "DC2-DMZ")]

        def view_for(folder, types, recursive):
            return NS(
                view=[o for o in objects if isinstance(o, tuple(types))], Destroy=lambda: None
            )

        content = NS(
            rootFolder=object(),
            viewManager=NS(CreateContainerView=view_for),
            about=NS(apiVersion="8.0.2.0", version="8.0.2"),
            customFieldsManager=NS(field=[NS(key=1, name="app")]),
        )
        return NS(RetrieveContent=lambda: content)

    provider = VMwareProvider(
        base, settings(secrets_dir=tmp_path / "secrets"), connector=connector, vim=vim
    )
    [mapped] = await provider.list_vms()
    assert mapped.nics[0].network == "DC2-DMZ"
    # the migration kit maps a distributed port group's NIC by the port group's key (SDD §7.2)
    assert mapped.nics[0].network_key == "dvportgroup-7"
    # an NSX opaque network: Seamless names it by its id, the kit maps it by the device summary
    nic.backing = NS(opaqueNetworkId="nsx-ls-1")
    nic.deviceInfo = NS(label="Network adapter 1", summary="nsx.LogicalSwitch: 5ab3")
    [mapped] = await provider.list_vms()
    assert (mapped.nics[0].network, mapped.nics[0].network_key) == (
        "nsx-ls-1",
        "nsx.LogicalSwitch: 5ab3",
    )

    # pyVmomi exceptions become ProviderError with the vSphere message
    def failing(**_):
        raise RuntimeError("Cannot complete login due to an incorrect user name or password.")

    broken = VMwareProvider(
        base, settings(secrets_dir=tmp_path / "secrets"), connector=failing, vim=vim
    )
    with pytest.raises(ProviderError, match="incorrect user name"):
        await broken.check()

    class Fault(Exception):
        msg = "The session is not authenticated."

    def faulting(**_):
        raise Fault()

    faulty = VMwareProvider(
        base, settings(secrets_dir=tmp_path / "secrets"), connector=faulting, vim=vim
    )
    with pytest.raises(ProviderError, match="session is not authenticated"):
        await faulty.list_vms()


async def test_vmware_power_on_times_out(tmp_path, monkeypatch):
    from seamless_migrate.providers import vmware as vmw

    secret_dir = tmp_path / "secrets" / "vcenter-dc2"
    secret_dir.mkdir(parents=True)
    (secret_dir / "username").write_text("u")
    (secret_dir / "password").write_text("p")
    vm = PoweredOffVM(task_state="running")
    clock = [0.0]

    def fast_clock():  # every call advances 400 s: the deadline (the call's, less 5 s) passes
        clock[0] += 400.0
        return clock[0]

    # patch the provider's view of `time` only: asyncio's own clock must stay real
    monkeypatch.setattr(vmw, "time", NS(monotonic=fast_clock, sleep=lambda s: None))
    provider = vmw.VMwareProvider(
        make_provider(
            id="vcenter", kind=ProviderKind.vmware, credentials_secret="vcenter-dc2", cloud=None
        ),
        settings(secrets_dir=tmp_path / "secrets"),
        connector=lambda **_: fake_service_instance([vm]),
        vim=FAKE_VIM,
    )
    with pytest.raises(ProviderError, match="timed out"):
        await provider.power_on("5012-abcd")


@pytest.mark.parametrize("verify, ca_cert", [(True, None), (True, "vcenter-ca.crt"), (False, None)])
def test_vmware_connector_bounds_every_request_and_the_version_probe(
    monkeypatch, caplog, verify, ca_cert
):
    """Every request of a vCenter connection gives up after the provider API timeout: pyVmomi's
    ``httpConnectionTimeout`` bounds its SOAP requests, and the SSL context bounds every socket it
    wraps without a timeout of its own - SmartConnect's version probe opens one and takes no
    timeout. pyVmomi's own unverified context (``disableSslCertValidation``) would replace that
    context, so verify_tls=false hands an unverified one of the provider's (SDD §10, review
    PV-02)."""
    import socket
    import ssl

    from seamless_migrate.providers import openstack as osp
    from seamless_migrate.providers import vmware as vmw

    seen: dict = {}
    cafiles: list = []
    real_default_context = ssl.create_default_context

    def create_default_context(cafile=None):
        cafiles.append(cafile)
        return real_default_context()

    def smart_connect(**kwargs):
        seen.update(kwargs)
        return NS()

    monkeypatch.setattr(vmw, "_import_pyvmomi", lambda: (smart_connect, lambda si: None, NS()))
    monkeypatch.setattr(vmw.ssl, "create_default_context", create_default_context)
    vmw.default_connector("vcenter.example.org", 443, "svc", "placeholder", verify, ca_cert)

    assert seen["httpConnectionTimeout"] == osp.PROVIDER_API_TIMEOUT_S
    assert "disableSslCertValidation" not in seen
    context = seen["sslContext"]
    assert (context.verify_mode, context.check_hostname) == (
        (ssl.CERT_REQUIRED, True) if verify else (ssl.CERT_NONE, False)
    )
    assert cafiles == ([ca_cert] if verify else [])
    assert ("TLS certificate verification is disabled" in caplog.text) is not verify
    # a socket without a timeout reads with the provider API timeout; one with its own keeps it
    for own, expected in ((None, osp.PROVIDER_API_TIMEOUT_S), (5.0, 5.0)):
        left, right = socket.socketpair()
        left.settimeout(own)
        wrapped = context.wrap_socket(
            left, server_hostname="vcenter.example.org", do_handshake_on_connect=False
        )
        try:
            assert wrapped.gettimeout() == expected
        finally:
            wrapped.close()
            right.close()


async def test_a_vcenter_that_accepts_and_never_answers_frees_the_worker_thread(
    tmp_path, monkeypatch
):
    """A vCenter that accepts connections and never answers - an appliance hung while it is
    patched - fails the call once a request gives up, the version probe of pyVmomi's real
    SmartConnect included, and so frees the worker thread: provider calls share asyncio's default
    executor with the store's, which a thread pinned per call would starve. The error names the
    request's bound, not the call's (SDD §10, review PV-02)."""
    import asyncio
    import socket
    import threading
    from concurrent.futures import ThreadPoolExecutor

    pytest.importorskip("pyVim.connect")
    from seamless_migrate.providers import vmware as vmw

    server = socket.create_server(("127.0.0.1", 0))
    server.settimeout(0.1)
    held: list = []
    stop = threading.Event()

    def accept() -> None:  # accepts every connection, answers none
        while not stop.is_set():
            try:
                conn, _ = server.accept()
            except TimeoutError:
                continue
            held.append(conn)

    acceptor = threading.Thread(target=accept, daemon=True)
    acceptor.start()
    monkeypatch.setattr(vmw, "PROVIDER_API_TIMEOUT_S", 0.2)
    monkeypatch.setattr(vmw, "PROVIDER_CALL_TIMEOUT_S", 3.0)
    secret_dir = tmp_path / "secrets" / "vcenter-dc2"
    secret_dir.mkdir(parents=True)
    (secret_dir / "username").write_text("u")
    (secret_dir / "password").write_text("p")
    provider = vmw.VMwareProvider(
        make_provider(
            id="vcenter",
            kind=ProviderKind.vmware,
            credentials_secret="vcenter-dc2",
            endpoint=f"https://127.0.0.1:{server.getsockname()[1]}/sdk",
            cloud=None,
        ),
        settings(secrets_dir=tmp_path / "secrets"),
    )
    executor = ThreadPoolExecutor(max_workers=1)  # the store's only thread as well
    asyncio.get_running_loop().set_default_executor(executor)
    try:
        with pytest.raises(ProviderError, match=r"^vcenter: vCenter did not answer within 0\.2 s$"):
            await provider.get_vm("5012-abcd")
        assert held, "the call never reached the vCenter"
        assert await asyncio.wait_for(asyncio.to_thread(lambda: "stored"), 2.0) == "stored"
    finally:
        stop.set()
        acceptor.join()
        server.close()
        for conn in held:
            conn.close()
        executor.shutdown(wait=True)


class ShuttingDown:
    """A VM whose guest VMware Tools shut down - the migration kit's ShutdownGuest: vSphere reads
    it poweredOn for ``looks`` reads of its power state, then poweredOff; its guest reads
    shuttingDown until the last poweredOn look, which reads notRunning (the Tools stop just before
    vSphere powers the VM off). A power-on meanwhile would find it on and do nothing."""

    def __init__(self, looks):
        self.left, self.host = looks, NS(name="esx-01.dc2")

    @property
    def powerState(self):  # noqa: N802 - pyVmomi naming
        if self.left > 0:
            self.left -= 1
            return "poweredOn"
        return "poweredOff"


class ShuttingDownGuest:
    def __init__(self, runtime):
        self.runtime = runtime

    @property
    def guestState(self):  # noqa: N802 - pyVmomi naming
        return "shuttingDown" if self.runtime.left > 0 else "notRunning"


def shutting_down_vm(looks):
    vm = PoweredOffVM()
    vm.runtime = ShuttingDown(looks)
    vm.guest = ShuttingDownGuest(vm.runtime)
    return vm


async def test_a_vmware_power_on_waits_for_a_guest_still_shutting_down(tmp_path, monkeypatch):
    """SDD §7.2 (review RV26-10): the VMware rollback powers the source on while its guest is
    still shutting down - the kit's shutdown was cut off by the step timeout or a rollback
    request. vSphere reads the VM poweredOn until the guest is down, so the power-on waits for
    it to be off and then powers it on, instead of reading it as running and leaving it to go
    off a moment later; a guest that never ends its shutdown is an error, not "running"."""
    from seamless_migrate.providers import vmware as vmw

    secret_dir = tmp_path / "secrets" / "vcenter-dc2"
    secret_dir.mkdir(parents=True)
    (secret_dir / "username").write_text("u")
    (secret_dir / "password").write_text("p")
    clock = [0.0]

    def tick():
        clock[0] += 1.0
        return clock[0]

    monkeypatch.setattr(vmw, "time", NS(monotonic=tick, sleep=lambda s: None))

    def provider_for(vm):
        return vmw.VMwareProvider(
            make_provider(
                id="vcenter", kind=ProviderKind.vmware, credentials_secret="vcenter-dc2", cloud=None
            ),
            settings(secrets_dir=tmp_path / "secrets"),
            connector=lambda **_: fake_service_instance([vm]),
            vim=FAKE_VIM,
        )

    vm = shutting_down_vm(looks=3)
    await provider_for(vm).power_on("5012-abcd")
    assert len(vm.tasks) == 1, "the power-on read a guest shutting down as running"
    assert vm.runtime.left == 0
    # a VM running without Tools reads notRunning too: never a shutdown, nothing to wait for
    tools_less = PoweredOffVM(state="poweredOn")
    tools_less.guest = NS(guestState="notRunning")
    started = clock[0]
    await provider_for(tools_less).power_on("5012-abcd")
    assert tools_less.tasks == [] and clock[0] - started < 2
    stuck = shutting_down_vm(looks=10**6)
    with pytest.raises(ProviderError, match="still shutting down"):
        await provider_for(stuck).power_on("5012-abcd")
    assert stuck.tasks == []


class Reads:
    """vSphere's answers to one attribute, read after read; the last one stays."""

    def __init__(self, *answers):
        self.answers = list(answers)

    def next(self):
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


def tools_stopped_vm(power, guest, tools="guestToolsCurrent"):
    """A VM whose power state and guest state read ``power`` and ``guest`` in turn; its VMware
    Tools are ``tools`` (installed and current by default, but not running)."""
    vm = PoweredOffVM()
    power_reads, guest_reads = Reads(*power), Reads(*guest)

    class Runtime:
        host = NS(name="esx-01.dc2")

        @property
        def powerState(self):  # noqa: N802 - pyVmomi naming
            return power_reads.next()

    class Guest:
        toolsVersionStatus2 = tools
        toolsRunningStatus = "guestToolsNotRunning"

        @property
        def guestState(self):  # noqa: N802 - pyVmomi naming
            return guest_reads.next()

    vm.runtime, vm.guest = Runtime(), Guest()
    return vm


def vcenter_for(tmp_path, vm):
    from seamless_migrate.providers import vmware as vmw

    secret_dir = tmp_path / "secrets" / "vcenter-dc2"
    secret_dir.mkdir(parents=True, exist_ok=True)
    (secret_dir / "username").write_text("u")
    (secret_dir / "password").write_text("p")
    return vmw.VMwareProvider(
        make_provider(
            id="vcenter", kind=ProviderKind.vmware, credentials_secret="vcenter-dc2", cloud=None
        ),
        settings(secrets_dir=tmp_path / "secrets"),
        connector=lambda **_: fake_service_instance([vm]),
        vim=FAKE_VIM,
    )


async def test_a_vmware_power_on_looks_again_at_a_vm_whose_tools_stopped(tmp_path, monkeypatch):
    """SDD §7.2 (review RV27-01): a guest's VMware Tools stop just before vSphere powers the VM
    off, so a rollback whose first look falls in the last seconds of a shutdown reads the VM on
    and its guest notRunning, as for a VM running without Tools. A VM whose installed Tools no
    longer run is looked at again for up to 120 s: powered on once it is off, left alone once its
    guest runs (one still starting) or when the 120 s end; a VM without Tools is left alone at
    once, and so is a guest seen shutting down that runs again (a reboot)."""
    from seamless_migrate.providers import vmware as vmw

    clock = [0.0]

    def tick():
        clock[0] += 1.0
        return clock[0]

    monkeypatch.setattr(vmw, "time", NS(monotonic=tick, sleep=lambda s: None))

    async def power_on(vm):
        started = clock[0]
        await vcenter_for(tmp_path, vm).power_on("5012-abcd")
        return len(vm.tasks), clock[0] - started

    # the last seconds of a shutdown: off at the next look - powered on
    tail = tools_stopped_vm(["poweredOn", "poweredOff"], ["notRunning"])
    assert (await power_on(tail))[0] == 1, "the power-on read a shutdown's tail as a VM running"
    # a guest still starting: its Tools run at the third look - left alone, no 120 s wait
    starting = tools_stopped_vm(["poweredOn"], ["notRunning", "notRunning", "running"])
    tasks, waited = await power_on(starting)
    assert tasks == 0 and waited < 20, waited
    # installed Tools that never run again on a VM that stays on: left alone after 120 s
    silent = tools_stopped_vm(["poweredOn"], ["notRunning"])
    tasks, waited = await power_on(silent)
    assert tasks == 0 and 120 <= waited < 150, waited
    # no Tools at all: a VM running without them, left alone at once
    tools_less = tools_stopped_vm(["poweredOn"], ["notRunning"], tools="guestToolsNotInstalled")
    tasks, waited = await power_on(tools_less)
    assert tasks == 0 and waited < 3, waited
    # a guest seen shutting down that runs again - it rebooted: left alone once it runs
    rebooted = tools_stopped_vm(["poweredOn"], ["shuttingDown", "notRunning", "running"])
    tasks, waited = await power_on(rebooted)
    assert tasks == 0 and waited < 20, waited


async def test_a_vmware_power_on_ends_within_its_call_and_never_acts_after_it(
    tmp_path, monkeypatch
):
    """SDD §7.2 (review RV27-02): every provider call is cut at its bound (300 s, SDD §8.1), and
    the thread that runs it cannot be stopped. The wait for a guest shutdown ends within that
    bound, less 5 s, with the "still shutting down" error the rollback retries; and a call that
    ended - timed out or cancelled - never powers the VM on afterwards, behind the back of the
    rollback's retry."""
    import asyncio
    import time as real_time

    from seamless_migrate.providers import vmware as vmw

    clock = [0.0]

    def tick():
        clock[0] += 1.0
        return clock[0]

    monkeypatch.setattr(vmw, "time", NS(monotonic=tick, sleep=lambda s: None))
    stuck = shutting_down_vm(looks=10**6)
    with pytest.raises(ProviderError, match="still shutting down"):
        await vcenter_for(tmp_path, stuck).power_on("5012-abcd")
    assert clock[0] < vmw.PROVIDER_CALL_TIMEOUT_S, "the wait outlived the call's bound"

    # a call cancelled while the guest shuts down: the guest is down a moment later
    real_sleep = real_time.sleep
    monkeypatch.setattr(
        vmw, "time", NS(monotonic=real_time.monotonic, sleep=lambda s: real_sleep(0.01))
    )
    off_at = real_time.monotonic() + 0.3
    vm = tools_stopped_vm(["poweredOn"], ["shuttingDown"])
    looks = []

    class Runtime:
        host = NS(name="esx-01.dc2")

        @property
        def powerState(self):  # noqa: N802 - pyVmomi naming
            looks.append(1)
            return "poweredOn" if real_time.monotonic() < off_at else "poweredOff"

    vm.runtime = Runtime()
    call = asyncio.create_task(vcenter_for(tmp_path, vm).power_on("5012-abcd"))
    await asyncio.sleep(0.1)
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    await asyncio.sleep(0.2)
    seen = len(looks)
    await asyncio.sleep(0.4)  # the guest is down by now
    assert vm.tasks == [], "the call was cancelled, yet its thread powered the VM on"
    assert len(looks) == seen, "the thread of a cancelled call went on asking vCenter"


@pytest.mark.parametrize("later", [2, 1801])
async def test_a_vmware_rollback_retry_remembers_the_shutdown_an_earlier_attempt_saw(
    tmp_path, monkeypatch, later
):
    """SDD §7.2 (review RV29-08): a guest that does not end its shutdown within the call's bound
    is a provider error the rollback retries. The retry's first look may read the guest
    notRunning with its Tools stopped - the shutdown's last phase, which the provider looks at
    for 120 s only as a guest that might be starting. The provider remembers the shutdown it saw
    for 30 minutes: the retry waits through that phase to its own bound and powers the VM on once
    it is off. Past the 30 minutes it looks for 120 s again. Here vmtoolsd stops 250 s into the
    rollback and the guest powers off 180 s later."""
    from seamless_migrate.providers import vmware as vmw

    clock = [0.0]

    def sleep(seconds):
        clock[0] += seconds

    monkeypatch.setattr(vmw, "time", NS(monotonic=lambda: clock[0], sleep=sleep))
    vm = PoweredOffVM()
    tools_stop = [250.0]

    class Runtime:
        host = NS(name="esx-01.dc2")

        @property
        def powerState(self):  # noqa: N802 - pyVmomi naming
            if vm.tasks:
                return "poweredOn"
            return "poweredOn" if clock[0] < tools_stop[0] + 180 else "poweredOff"

    class Guest:
        toolsVersionStatus2 = "guestToolsCurrent"

        @property
        def guestState(self):  # noqa: N802 - pyVmomi naming
            return "shuttingDown" if clock[0] < tools_stop[0] else "notRunning"

    vm.runtime, vm.guest = Runtime(), Guest()
    vcenter = vcenter_for(tmp_path, vm)
    with pytest.raises(ProviderError, match="still shutting down"):
        await vcenter.power_on("5012-abcd")  # the first attempt: its bound ends at 295 s
    if later > 1800:
        tools_stop[0] = clock[0] + later  # a new shutdown's tail, long after the remembered one
    sleep(later)
    await vcenter.power_on("5012-abcd")
    sleep(240)
    if later < 1800:
        assert vm.tasks, "the retry returned without a power-on; the VM went off under it"
        # powered on, the shutdown is over: a later look at the VM, its Tools stopped, is 120 s
        started = clock[0]
        await vcenter.power_on("5012-abcd")
        assert len(vm.tasks) == 1 and clock[0] - started <= 125
    else:
        assert vm.tasks == [], "a shutdown seen 30 minutes ago still decided the look"


async def test_a_guest_that_runs_again_ends_the_shutdown_the_provider_remembers(
    tmp_path, monkeypatch
):
    """SDD §7.2 (review RV29-08): a shutdown the provider remembers ends when the guest runs
    again - it rebooted rather than powering off: the retry leaves it alone, and a later look at
    the VM, its Tools stopped, is the 120 s one again, never a wait to the call's bound."""
    from seamless_migrate.providers import vmware as vmw

    clock = [0.0]

    def sleep(seconds):
        clock[0] += seconds

    monkeypatch.setattr(vmw, "time", NS(monotonic=lambda: clock[0], sleep=sleep))
    guest_state = ["shuttingDown"]
    vm = tools_stopped_vm(["poweredOn"], ["shuttingDown"])

    class Guest:
        toolsVersionStatus2 = "guestToolsCurrent"

        @property
        def guestState(self):  # noqa: N802 - pyVmomi naming
            return guest_state[0]

    vm.guest = Guest()
    vcenter = vcenter_for(tmp_path, vm)
    with pytest.raises(ProviderError, match="still shutting down"):
        await vcenter.power_on("5012-abcd")
    guest_state[0] = "running"  # it rebooted
    await vcenter.power_on("5012-abcd")
    guest_state[0] = "notRunning"  # its Tools stopped later on: no shutdown is remembered
    started = clock[0]
    await vcenter.power_on("5012-abcd")
    assert vm.tasks == [] and clock[0] - started <= 125


async def test_a_power_state_read_in_flight_when_the_power_on_call_ends_powers_nothing_on(
    tmp_path, monkeypatch
):
    """SDD §7.2 (review RV29-05): "a call that ended - timed out or cancelled - powers nothing on
    afterwards". Each power-state read is a vCenter round trip: one still in flight when the call
    is cancelled answers poweredOff afterwards, and the thread must not then power the VM on
    behind the back of the call that ended (and of the rollback's retry)."""
    import asyncio
    import threading
    import time as real_time

    from seamless_migrate.providers import vmware as vmw

    real_sleep = real_time.sleep
    monkeypatch.setattr(
        vmw, "time", NS(monotonic=real_time.monotonic, sleep=lambda s: real_sleep(0.01))
    )
    in_read, answer = threading.Event(), threading.Event()
    reads = []
    vm = PoweredOffVM()

    class Runtime:
        host = NS(name="esx-01.dc2")

        @property
        def powerState(self):  # noqa: N802 - pyVmomi naming
            reads.append(1)
            if len(reads) == 1:
                return "poweredOn"  # the first look: the guest still shuts down
            in_read.set()  # the next look: vCenter answers slowly ...
            answer.wait(30)  # ... only after the call ended
            return "poweredOff"

    class Guest:
        toolsVersionStatus2 = "guestToolsCurrent"
        guestState = "shuttingDown"

    vm.runtime, vm.guest = Runtime(), Guest()
    call = asyncio.create_task(vcenter_for(tmp_path, vm).power_on("5012-abcd"))
    assert await asyncio.to_thread(in_read.wait, 10)
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    answer.set()
    await asyncio.sleep(0.5)
    assert vm.tasks == [], "the call was cancelled, yet its thread powered the VM on afterwards"


async def test_fake_destination_inventory_resolves_every_demo_nic():
    """SDD §10, §9.3: in demo mode every OpenStack demo VM's NIC resolves to one RHOSO network whose
    subnets take its addresses, and that the demo's admin credential may use - the demo plans are
    not blocked by MAP_NETWORK_AMBIGUOUS, NET_ADDRESS_REFUSED or NET_PORT_NOT_PERMITTED - and the
    shared provider network is another project's."""
    from seamless_migrate.planning.preflight import run_preflight
    from tests.factories import make_plan

    caps = await FakeDestinationProvider(seed=42).check()
    assert "admin" in caps["roles"]
    dst = await FakeDestinationProvider(seed=42).inventory()
    ids = {n["id"] for n in dst.network_details}
    assert all(s["network_id"] in ids for s in dst.subnets)
    [shared] = [n for n in dst.network_details if not n["own"]]
    assert (shared["name"], shared["shared"]) == ("provider-vlan100", True)
    # the demo's seed (Settings.demo_seed) and another
    for seed in (Settings().demo_seed, 7):
        source = FakeSourceProvider(ProviderKind.openstack, seed=seed)
        vms = await source.list_vms()
        src = await source.inventory()
        for mode in ("nova", "neutron"):
            plan = make_plan(port_creation_mode=mode)
            for vm in vms:
                codes = [f.code for f in run_preflight(vm, plan, src, dst, vms, dst_caps=caps)]
                assert "MAP_NETWORK_AMBIGUOUS" not in codes, (vm.name, codes)
                assert "NET_ADDRESS_REFUSED" not in codes, (seed, vm.name, mode, codes)
                assert "NET_PORT_NOT_PERMITTED" not in codes, (seed, vm.name, mode, codes)


async def test_fake_vmware_vms_get_a_network_map_value_for_every_nic():
    """SDD §7.2, §9.3: the demo's VMware plan (DC2-Prod and DC2-DMZ mapped) blocks none of its VMs
    on the network findings, and the migration kit gets every NIC's network and subnet."""
    from seamless_migrate.domain.models import Mappings
    from seamless_migrate.planning.networks import (
        kit_network_key,
        kit_network_value,
        kit_subnet,
        resolve_network,
    )
    from seamless_migrate.planning.preflight import run_preflight
    from tests.factories import make_plan, make_provider

    dst_provider = FakeDestinationProvider(seed=42)
    caps, dst = await dst_provider.check(), await dst_provider.inventory()
    vcenter = make_provider(id="vc", kind=ProviderKind.vmware, cloud=None, credentials_secret="s")
    source = FakeSourceProvider(ProviderKind.vmware, seed=Settings().demo_seed)
    vms, src = await source.list_vms(), await source.inventory()
    plan = make_plan(mappings=Mappings(networks={"DC2-Prod": "dc2-prod", "DC2-DMZ": "dc2-dmz"}))
    for vm in vms:
        codes = [
            f.code for f in run_preflight(vm, plan, src, dst, vms, source=vcenter, dst_caps=caps)
        ]
        for code in ("MAP_NETWORK_AMBIGUOUS", "NET_ADDRESS_REFUSED", "NET_PORT_NOT_PERMITTED"):
            assert code not in codes, (vm.name, codes)
        for nic in vm.nics:
            network, _ = resolve_network(dst.network_details, plan.nic_target(vm.source_id, nic)[0])
            own = [s for s in dst.subnets if s["network_id"] == network["id"]]
            subnet, why = kit_subnet(nic, own)
            assert why is None, (vm.name, why)
            assert kit_network_key(nic) == nic.network
            assert kit_network_value(network, subnet) == f"{network['id']},{subnet['id']}"
