import builtins
import sys
from types import SimpleNamespace as NS

import pytest

from seamless_migrate.config import Settings
from seamless_migrate.domain.enums import ProviderKind, ProviderRole
from seamless_migrate.planning.preflight import DestinationInventory, SourceInventory, is_legacy_os
from seamless_migrate.providers import registry
from seamless_migrate.providers.base import ProviderError, provider_caps
from seamless_migrate.providers.fake import FakeDestinationProvider, FakeSourceProvider
from seamless_migrate.providers.openstack import OpenStackProvider
from seamless_migrate.providers.vmware import VMwareProvider
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
    with pytest.raises(ProviderError):
        await src.get_vm("does-not-exist")


async def test_fake_vmware_mixed_cbt():
    src = FakeSourceProvider(ProviderKind.vmware, seed=42)
    vms = await src.list_vms()
    assert len(vms) == 12
    assert {v.cbt_enabled for v in vms} == {True, False}
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
    caps = await dst.check()
    assert caps["admin"] is True and caps["ovn"] is True
    server = await dst.get_server("srv-1")
    assert server["status"] == "ACTIVE" and server["ports"][0]["status"] == "ACTIVE"
    assert "login:" in (await dst.console_log("srv-1"))
    await dst.delete_server("srv-1")
    assert "srv-1" in dst.deleted
    assert await dst.find_server("web-01") is not None


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
        return [NS(name="hostgroup@ceph#ssd")]


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
    networks = [NS(id="net-1", name="app-net", mtu=1442)]
    return NS(
        compute=FakeCompute([server], admin=admin),
        block_storage=FakeBlockStorage(volumes),
        network=FakeNetwork(ports, networks, ovn=ovn),
        identity=NS(get_project=lambda pid: NS(id=pid, name="finance")),
        current_project_id="proj-1",
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
    assert inv.networks == {"app-net": 1442}


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
        "compute_microversion": "2.95",
        "ovn": True,
        "volume_backends": ["hostgroup@ceph#ssd"],
    }
    stub_openstack(fake_conn(admin=False, ovn=False))
    caps = await OpenStackProvider(make_provider(), settings()).check()
    assert caps["admin"] is False and caps["ovn"] is False


async def test_openstack_destination_inventory(stub_openstack):
    stub_openstack(fake_conn())
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
        self.deviceInfo = NS(label="Network adapter 1")
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
    with pytest.raises(ProviderError, match="not found"):
        await provider_for([vm]).get_vm("missing")
    inv = await provider_for([vm]).inventory()
    assert inv.networks == {} and inv.projects == []  # the fake vim has no Network type


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
    with pytest.raises(ProviderError, match="not found"):
        await provider.get_vm("missing")  # falls back to the walk, then fails


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


async def test_openstack_free_quota_console_delete_and_project_lookup(tmp_path):
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

    # deleting a server that is already gone is a no-op; an existing one is deleted and awaited
    waited = []
    conn.compute.delete_server = lambda server, ignore_missing=True: conn.compute.deleted.append(
        server.id
    )
    conn.compute.wait_for_delete = lambda server, wait=600: waited.append(server.id)
    await provider.delete_server("missing")
    assert conn.compute.deleted == []
    await provider.delete_server("web-01")
    assert conn.compute.deleted == ["srv-1"] and waited == ["srv-1"]
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
    conn.compute.get_flavor = lambda flavor_id: NS(
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
    nic.backing = NS(port=NS(portgroupKey="dvportgroup-7"))
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

    def fast_clock():  # every call advances 400 s: the 600 s deadline passes on the 2nd poll
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
