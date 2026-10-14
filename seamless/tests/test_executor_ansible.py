import ast
import copy
import importlib
import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from seamless_migrate.config import Settings, find_repo_root
from seamless_migrate.domain.enums import ProviderKind, ProviderRole, Strategy, SyncPassKind
from seamless_migrate.domain.models import ConversionHostConfig, Mappings, Nic, utcnow
from seamless_migrate.executors.ansible import (
    CONVERSION_KEY_FILE,
    KIT_PLAYBOOK,
    STOP_TASK,
    AnsibleExecutor,
    Deferred,
    ansible_yaml,
    apply_mappings,
    build_inventory,
    build_vars,
    classify_failure,
    effective_mappings,
    resolve_network_refs,
    workload_file_stem,
    workload_filter,
)
from seamless_migrate.executors.ansible import _inventory as build_inventory_doc
from seamless_migrate.executors.base import PermanentStepError, StepName, TransientStepError
from seamless_migrate.planning.preflight import DestinationInventory
from seamless_migrate.providers.base import ProviderError
from tests.executor_support import make_ctx
from tests.factories import make_migration, make_plan, make_provider, make_vm
from tests.fake_ansible import install, read_log

SRC = make_provider(cloud="src")
DST = make_provider(
    id="dst-rhoso",
    kind=ProviderKind.rhoso,
    role=ProviderRole.destination,
    cloud="dst",
    region="regionTwo",
    conversion_host=ConversionHostConfig(name="conv-dst", address="192.0.2.10"),
)
VCENTER = make_provider(
    id="vcenter-dc2",
    kind=ProviderKind.vmware,
    cloud=None,
    endpoint="https://vcenter.dc2.example/sdk",
    credentials_secret="vcenter-dc2",
    conversion_host=None,
)
# a NIC as the VMware provider records it: a standard port group's key is its name (SDD §4)
VMW_NICS = [
    Nic(network="app-net", mac="00:50:56:00:00:01", fixed_ips=["10.0.0.5"], network_key="app-net")
]
MAPPINGS = Mappings(
    networks={"app-net": "rhoso-app", "DC2-Prod": "dc2-prod"},
    flavors={"m1.small": "rhoso.small"},
    volume_types={"ceph-hdd": "rbd-hdd"},
    projects={"finance": "finance-rhoso"},
)


class AnsibleLikeLoader(yaml.SafeLoader):
    """Reads generated Ansible input like Ansible does: a ``!unsafe`` scalar is a plain string."""


AnsibleLikeLoader.add_constructor("!unsafe", lambda loader, node: loader.construct_scalar(node))


def load_ansible_yaml(text: str):
    return yaml.load(text, Loader=AnsibleLikeLoader)


class FakeImpl:
    """Destination/source provider double: a server named like the VM exists when the test
    declared it (``registry.existing``) or once a fake import/cutover playbook created it."""

    def __init__(self, registry):
        self.calls = registry.calls
        self.registry = registry

    async def find_server(self, name):
        self.calls.append(("find_server", name))
        if name in self.registry.existing:
            return f"dst-{name}"
        return f"dst-{name}" if name in self.registry.created() else None

    async def delete_server(self, server_id):
        self.calls.append(("delete_server", server_id))

    async def power_on(self, source_id):
        self.calls.append(("power_on", source_id))

    async def ports(self, *, network_id=None, mac_address=None, ip_address=None):
        """The destination's ports matching every filter given (SDD §10)."""
        if self.registry.ports_error:
            raise ProviderError("ports: HTTP 503")
        return [
            dict(p)
            for p in self.registry.ports
            if (network_id is None or p["network_id"] == network_id)
            and (mac_address is None or p["mac_address"] == mac_address)
            and (ip_address is None or ip_address in p["fixed_ips"])
        ]

    async def delete_port(self, port_id):
        self.calls.append(("delete_port", port_id))
        self.registry.ports = [p for p in self.registry.ports if p["id"] != port_id]

    async def inventory(self):
        """The destination's networks, for the network references (SDD §7.2)."""
        self.calls.append(("inventory",))
        if self.registry.inventory_error:
            raise ProviderError("networks: HTTP 503")
        return DestinationInventory(
            network_details=list(self.registry.networks),
            subnets=[dst_subnet(n) for n in self.registry.networks] + self.registry.subnets,
        )


def dst_net(name, own=True, project_id=None):
    return {
        "id": f"net-{name}-{project_id or ('mine' if own else 'theirs')}",
        "name": name,
        "mtu": 1500,
        "project_id": project_id or ("finance-rhoso" if own else "admin"),
        "own": own,
        "shared": not own,
    }


def dst_subnet(network, cidr="10.0.0.0/16"):
    """A network's one subnet, by id: what a VMware NIC's network_map value names (SDD §7.2)."""
    return {
        "id": f"sub-{network['id']}",
        "name": f"{network['name']}-v4",
        "network": network["name"],
        "network_id": network["id"],
        "cidr": cidr,
        "ipv6_address_mode": None,
        "ipv6_ra_mode": None,
    }


class FakeRegistry:
    def __init__(self, existing=()):
        self.calls = []
        self.existing = set(existing)
        # the credential's own networks the tests send ports to (SDD §7.2)
        self.networks = [dst_net(n) for n in ("rhoso-app", "rhoso-dmz", "app-net", "dc2-prod")]
        # more subnets, after each network's one
        self.subnets: list[dict] = []
        self.inventory_error = False
        # the destination's ports (SDD §10 DestinationProvider.ports)
        self.ports: list[dict] = []
        self.ports_error = False

    def get(self, provider):
        return FakeImpl(self)

    @staticmethod
    def created() -> list[str]:
        """Names the fake playbooks created (see ``ANSIBLE_FAKE_CREATED`` in fake_ansible)."""
        marker = os.environ.get("ANSIBLE_FAKE_CREATED")
        if not marker or not os.path.exists(marker):
            return []
        with open(marker, encoding="utf-8") as fh:
            return fh.read().split()


@pytest.fixture
def env(tmp_path, monkeypatch):
    fake = install(tmp_path / "bin")
    log = tmp_path / "ansible.log"
    monkeypatch.setenv("ANSIBLE_FAKE_LOG", str(log))
    monkeypatch.setenv("ANSIBLE_FAKE_CREATED", str(tmp_path / "created.txt"))
    monkeypatch.setenv("ANSIBLE_FAKE_SLEEP", "0.05")
    monkeypatch.setenv("SEAMLESS_JEV_TOKEN", "must-not-leak")
    clouds = tmp_path / "clouds.yaml"
    clouds.write_text(
        "clouds:\n"
        "  src:\n    auth: {auth_url: 'https://src/v3', username: su, password: spw, "
        "project_name: finance}\n    region_name: regionOne\n"
        "  dst:\n    auth: {auth_url: 'https://dst/v3', username: du, password: dpw, "
        "project_name: finance}\n    region_name: regionOne\n    interface: public\n"
    )
    secret = tmp_path / "secrets" / "vcenter-dc2"
    secret.mkdir(parents=True)
    (secret / "username").write_text("svc@vsphere.local")
    (secret / "password").write_text("vpw")
    (secret / "datacenter").write_text("DC2")
    settings = Settings(
        data_dir=tmp_path / "data",
        ansible_playbook=str(fake),
        clouds_yaml=clouds,
        secrets_dir=tmp_path / "secrets",
        collection_root=find_repo_root(),
    )
    registry = FakeRegistry()
    executor = AnsibleExecutor(settings, providers=registry, poll_s=0.02)
    return SimpleNamespace(settings=settings, log=log, executor=executor, registry=registry)


def ctx_for(
    env, strategy=Strategy.warm, source=SRC, name="web-01", mappings=MAPPINGS, nics=None, **mig_kw
):
    plan = make_plan(id="plan-00c0ffee", mappings=mappings)
    vm = make_vm(source_id="srv-1", name=name, **({"nics": nics} if nics is not None else {}))
    mig = make_migration(id="mig-00000000aa", plan_id=plan.id, vm=vm, strategy=strategy, **mig_kw)
    return make_ctx(plan, mig, source, DST, env.settings)


def run_dir(env) -> Path:
    return env.settings.data_dir / "plans" / "plan-00c0ffee" / "migrations" / "mig-00000000aa"


def test_workload_filter_escapes_regex():
    name = "app.server[1]+(x)"
    [query] = workload_filter(name)
    assert query == {"regex": "^" + re.escape(name) + "$"}
    assert re.search(query["regex"], name)
    for other in ("appXserver[1]+(x)", name + "-2", "x" + name, "app.server1+(x)"):
        assert not re.search(query["regex"], other), other


def test_apply_mappings_rewrites_refs():
    ref = lambda name, project="finance": {  # noqa: E731
        "name": name,
        "project_name": project,
        "domain_name": "Default",
    }
    doc = {
        "os_migrate_version": "1.0.5",
        "resources": [
            {
                "type": "openstack.compute.Server",
                "params": {
                    "name": "web-01",
                    "flavor_ref": ref("m1.small"),
                    "ports": [
                        {
                            "params": {
                                "network_ref": ref("app-net"),
                                "fixed_ips_refs": [
                                    {
                                        "ip_address": "10.0.0.5",
                                        "subnet_ref": {
                                            **ref("app-subnet"),
                                            "network_ref": ref("app-net"),
                                        },
                                    },
                                    {
                                        "ip_address": "10.0.1.5",
                                        "subnet_ref": ref("other-subnet", "%auth%"),
                                    },
                                ],
                            }
                        }
                    ],
                    "volumes": [
                        {"params": {"name": "d", "volume_type": "ceph-hdd"}},
                        {"params": {"name": "e", "volume_type": "unmapped"}},
                    ],
                    "security_group_refs": [ref("default")],
                    "image_ref": None,
                },
                "_info": {"id": "srv-1"},
                "_migration_params": {"boot_volume_params": {"volume_type": "ceph-hdd"}},
            }
        ],
    }
    original = copy.deepcopy(doc)
    out = apply_mappings(doc, MAPPINGS)
    assert doc == original, "apply_mappings must be pure"
    params = out["resources"][0]["params"]
    assert params["flavor_ref"]["name"] == "rhoso.small"
    port = params["ports"][0]["params"]
    assert port["network_ref"]["name"] == "rhoso-app"
    assert port["fixed_ips_refs"][0]["subnet_ref"]["network_ref"]["name"] == "rhoso-app"
    assert port["fixed_ips_refs"][0]["subnet_ref"]["name"] == "app-subnet"
    assert [v["params"]["volume_type"] for v in params["volumes"]] == ["rbd-hdd", "unmapped"]
    boot = out["resources"][0]["_migration_params"]["boot_volume_params"]
    assert boot["volume_type"] == "rbd-hdd"
    # project names in every ref follow the project mapping; %auth% is left alone
    assert params["flavor_ref"]["project_name"] == "finance-rhoso"
    assert params["security_group_refs"][0]["project_name"] == "finance-rhoso"
    assert port["fixed_ips_refs"][1]["subnet_ref"]["project_name"] == "%auth%"


async def test_warm_precopy_runs_export_once_then_precopy(env):
    ctx, rec = ctx_for(env)
    result = await env.executor.run(StepName.PRECOPY, ctx)
    calls = read_log(env.log)
    assert [c["playbook"] for c in calls] == [
        "export_workloads.yml",
        "import_workloads_precopy.yml",
    ]
    for call in calls:
        assert call["argv"][0] == "-i"
        assert Path(call["argv"][2]).parent == env.settings.collection_root / "playbooks"
        assert call["argv"][3:] == [
            "-e",
            f"@{run_dir(env) / 'vars.yml'}",
            "-e",
            f"@{run_dir(env) / 'secrets.yml'}",
        ]
        assert call["cwd"] == str(run_dir(env))
    v = calls[1]["vars"]
    osm = run_dir(env) / "osm"
    plan_osm = env.settings.data_dir / "plans" / "plan-00c0ffee" / "osm"
    assert v["os_migrate_data_dir"] == str(osm)
    assert v["os_migrate_workloads_filter"] == workload_filter("web-01")
    assert v["os_migrate_conversion_keypair_private_path"] == str(
        plan_osm / "conversion" / "ssh.key"
    )
    assert v["os_migrate_warm_chunk_size"] == 4194304 and v["os_migrate_warm_workers"] == 4
    assert v["os_migrate_dst_conversion_host_name"] == "conv-dst"
    # the exported workloads.yml was rewritten with the plan mappings before the pre-copy
    exported = yaml.safe_load((osm / "workloads.yml").read_text())
    assert exported["resources"][0]["params"]["flavor_ref"]["name"] == "rhoso.small"
    sp = result.sync_pass
    assert (sp.number, sp.kind, sp.bytes_changed, sp.duration_s) == (
        1,
        SyncPassKind.full,
        1000,
        300.0,
    )
    assert rec.downtime_marks == 0

    ctx.migration.sync_passes.append(sp)
    second = await env.executor.run(StepName.SYNC, ctx)
    calls = read_log(env.log)
    assert [c["playbook"] for c in calls][2:] == ["import_workloads_precopy.yml"]
    assert (second.sync_pass.number, second.sync_pass.kind) == (2, SyncPassKind.delta)


async def test_secrets_file_0600_and_deleted_after_run(env, monkeypatch):
    ctx, _ = ctx_for(env)
    await env.executor.run(StepName.PRECOPY, ctx)
    calls = read_log(env.log)
    for call in calls:
        assert call["secrets_mode"] == "0o600"
        assert {"os_migrate_src_auth", "os_migrate_dst_auth"} <= set(call["secret_keys"])
        assert "SEAMLESS_JEV_TOKEN" not in call["env_keys"], "control-plane env is not leaked"
        assert "spw" not in yaml.safe_dump(call["vars"]) and "dpw" not in str(call["vars"])
    assert not (run_dir(env) / "secrets.yml").exists()
    assert not (run_dir(env) / "osm" / "clouds.yaml").exists()

    monkeypatch.setenv("ANSIBLE_FAKE_FAIL", "Unable to find flavor rhoso.small")
    with pytest.raises(PermanentStepError):
        await env.executor.run(StepName.SYNC, ctx)
    assert not (run_dir(env) / "secrets.yml").exists()
    assert not (run_dir(env) / "osm" / "clouds.yaml").exists()


async def test_cutover_cold_sets_stop_before_migration(env):
    ctx, rec = ctx_for(env, strategy=Strategy.cold)
    result = await env.executor.run(StepName.CUTOVER, ctx)
    calls = read_log(env.log)
    assert [c["playbook"] for c in calls] == ["export_workloads.yml", "import_workloads.yml"]
    assert calls[1]["vars"]["os_migrate_workload_stop_before_migration"] is True
    assert rec.downtime_marks == 1
    assert result.destination_server_id == "dst-web-01"
    assert ("find_server", "web-01") in env.registry.calls

    rb, _ = ctx_for(env, strategy=Strategy.cold)
    await env.executor.run(StepName.ROLLBACK, rb)
    last = read_log(env.log)[-1]
    assert last["playbook"] == "rollback_workloads.yml"
    assert "os_migrate_rollback_delete_dest_volumes" not in last["vars"]
    # the cleanup after a cancel also drops the destination volumes of the abandoned pass
    plan = make_plan(id="plan-00c0ffee", mappings=MAPPINGS)
    mig = make_migration(plan_id=plan.id, vm=make_vm(source_id="srv-1"), strategy=Strategy.warm)
    cleanup, _ = make_ctx(plan, mig, SRC, DST, env.settings, delete_dest_volumes=True)
    await env.executor.run(StepName.ROLLBACK, cleanup)
    assert read_log(env.log)[-1]["vars"]["os_migrate_rollback_delete_dest_volumes"] is True
    assert not env.executor.supports(Strategy.storage_handover)


async def test_a_cold_cutover_records_its_copy_as_a_full_pass(env):
    """SDD §7.2/§5.5: the copy a cold cutover reported as progress is its full pass, so
    bytes_transferred stays its listed passes (invariant 2) - before, every real cold migration
    completed BYTES_MISMATCH."""
    started = utcnow()
    ctx, _ = ctx_for(env, strategy=Strategy.cold, bytes_total=7 * 2**30)
    result = await env.executor.run(StepName.CUTOVER, ctx)
    full = result.sync_pass
    assert full is not None and full.kind == SyncPassKind.full
    # what the state file's 100 % stands for: the VM's used bytes
    assert (full.bytes_changed, full.bytes_transferred) == (7 * 2**30, 7 * 2**30)
    assert full.bytes_scanned == ctx.migration.vm.disk_bytes
    assert started <= full.started_at <= full.ended_at
    assert full.duration_s == (full.ended_at - full.started_at).total_seconds()
    assert result.destination_server_id == "dst-web-01"
    # without a byte count from validation, the VM's used bytes
    plain, _ = ctx_for(env, strategy=Strategy.cold, name="db-01")
    assert (await env.executor.run(StepName.CUTOVER, plain)).sync_pass.bytes_transferred == (
        plain.migration.vm.used_bytes
    )


def port_mode_ctx(env, strategy, mode="neutron"):
    plan = make_plan(id="plan-00c0ffee", mappings=MAPPINGS, port_creation_mode=mode)
    vm = make_vm(source_id="srv-1", name="web-01")
    mig = make_migration(id="mig-00000000aa", plan_id=plan.id, vm=vm, strategy=strategy)
    return make_ctx(plan, mig, SRC, DST, env.settings)


def exported_server(env):
    doc = yaml.safe_load((run_dir(env) / "osm" / "workloads.yml").read_text())
    return next(r for r in doc["resources"] if r["type"] == "openstack.compute.Server")


async def test_the_plans_port_creation_mode_reaches_workloads_before_the_import(env, monkeypatch):
    """SDD §7.2: the executor writes plan.port_creation_mode into the server's _migration_params
    after the export and before every playbook run, so a mode chosen after a warm export applies
    to the next run."""
    monkeypatch.setenv("ANSIBLE_FAKE_PORT_SECURITY_GROUPS", "1")
    cold, _ = port_mode_ctx(env, Strategy.cold)
    await env.executor.run(StepName.CUTOVER, cold)
    import_call = read_log(env.log)[-1]
    assert import_call["playbook"] == "import_workloads.yml"
    assert import_call["port_creation_mode"] == ["neutron"]
    assert exported_server(env)["_migration_params"]["port_creation_mode"] == "neutron"

    (run_dir(env) / "osm" / "workloads.yml").unlink()
    warm, _ = port_mode_ctx(env, Strategy.warm, mode="nova")
    await env.executor.run(StepName.PRECOPY, warm)
    assert read_log(env.log)[-1]["port_creation_mode"] == ["nova"]
    later, _ = port_mode_ctx(env, Strategy.warm)
    await env.executor.run(StepName.SYNC, later)
    sync_call = read_log(env.log)[-1]
    assert sync_call["playbook"] == "import_workloads_precopy.yml"  # no new export
    assert sync_call["port_creation_mode"] == ["neutron"]


async def test_a_neutron_warm_cutover_exports_again_a_workload_without_port_security_groups(
    env, monkeypatch
):
    """SDD §7.2: an export from before §6.6's rule lists ports without their security groups;
    in "neutron" mode the warm cutover exports the workload again first."""
    precopy, _ = port_mode_ctx(env, Strategy.warm, mode="nova")
    await env.executor.run(StepName.PRECOPY, precopy)
    assert "security_group_refs" not in exported_server(env)["params"]["ports"][0]["params"]
    monkeypatch.setenv("ANSIBLE_FAKE_PORT_SECURITY_GROUPS", "1")
    cutover, _ = port_mode_ctx(env, Strategy.warm)
    await env.executor.run(StepName.CUTOVER, cutover)
    assert [c["playbook"] for c in read_log(env.log)][2:] == [
        "export_workloads.yml",
        "import_workloads_cutover.yml",
    ]
    assert exported_server(env)["params"]["ports"][0]["params"]["security_group_refs"]
    assert read_log(env.log)[-1]["port_creation_mode"] == ["neutron"]


async def test_neutron_mode_refuses_ports_exported_without_security_groups_before_the_stop(env):
    """A collection that still exports ports without their security groups would create them
    with the project's default group: the cutover stops after the export, before the import."""
    cold, rec = port_mode_ctx(env, Strategy.cold)
    with pytest.raises(PermanentStepError, match="security groups"):
        await env.executor.run(StepName.CUTOVER, cold)
    assert [c["playbook"] for c in read_log(env.log)] == ["export_workloads.yml"]
    assert rec.downtime_marks == 0
    # the rewrite did not finish: no export record, so the next run exports again (SDD §7.2)
    assert not (run_dir(env) / "osm" / "rewrite.json").exists()


def nic_ctx(env, strategy, overrides, mappings=MAPPINGS, **mig_kw):
    plan = make_plan(
        id="plan-00c0ffee",
        mappings=mappings,
        nic_overrides={"srv-1": overrides} if overrides else {},
    )
    vm = make_vm(source_id="srv-1", name="web-01")
    mig = make_migration(id="mig-00000000aa", plan_id=plan.id, vm=vm, strategy=strategy, **mig_kw)
    return make_ctx(plan, mig, SRC, DST, env.settings)


def test_apply_nic_overrides_sends_a_port_elsewhere_by_address():
    """SDD §7.2: the port with an entry (by MAC) goes to the entry's network; its addresses -
    the entry's, else its own - become address-only references Neutron places by subnet."""
    from seamless_migrate.domain.models import NicOverride
    from seamless_migrate.executors.ansible import apply_nic_overrides

    port = {
        "mac_address": "FA:16:3E:00:00:01",
        "network_ref": {"name": "app-net", "project_name": "finance", "domain_name": "Default"},
        "fixed_ips_refs": [{"ip_address": "10.0.0.5", "subnet_ref": {"name": "app-subnet"}}],
    }
    other = {**port, "mac_address": "fa:16:3e:00:00:02"}
    doc = {
        "resources": [
            {
                "type": "openstack.compute.Server",
                "params": {"ports": [{"params": port}, {"params": other}]},
            }
        ]
    }
    both = apply_nic_overrides(
        doc, {"fa:16:3e:00:00:01": NicOverride(network="rhoso-dmz", fixed_ips=["192.168.10.5"])}
    )
    first, second = (p["params"] for p in both["resources"][0]["params"]["ports"])
    assert first["network_ref"] == {
        "name": "rhoso-dmz",
        "project_name": "finance",
        "domain_name": "Default",
    }
    assert first["fixed_ips_refs"] == [{"ip_address": "192.168.10.5"}]
    assert second == other  # no entry: untouched
    assert doc["resources"][0]["params"]["ports"][0]["params"] == port  # a copy
    kept = apply_nic_overrides(doc, {"fa:16:3e:00:00:01": NicOverride(network="rhoso-dmz")})
    assert kept["resources"][0]["params"]["ports"][0]["params"]["fixed_ips_refs"] == [
        {"ip_address": "10.0.0.5"}
    ]


def fip_doc(addresses=("10.0.0.5",)):
    """A planned server: port fa:16:3e:00:00:01 holds ``addresses``, a floating IP 10.0.0.5."""
    return {
        "resources": [
            {
                "type": "openstack.compute.Server",
                "params": {
                    "ports": [
                        {
                            "params": {
                                "mac_address": "fa:16:3e:00:00:01",
                                "network_ref": {"name": "app-net"},
                                "fixed_ips_refs": [
                                    {"ip_address": a, "subnet_ref": {"name": "app-subnet"}}
                                    for a in addresses
                                ],
                            }
                        }
                    ],
                    "floating_ips": [
                        {
                            "params": {
                                "fixed_ip_address": "10.0.0.5",
                                "floating_ip_address": "203.0.113.7",
                            }
                        }
                    ],
                },
            }
        ]
    }


def test_apply_nic_overrides_moves_the_floating_ip_to_the_new_address():
    """SDD §7.2: os-migrate attaches a floating IP by its fixed address, so it follows the port to
    its new address of the same IP version."""
    from seamless_migrate.domain.models import NicOverride
    from seamless_migrate.executors.ansible import apply_nic_overrides

    entry = NicOverride(network="rhoso-dmz", fixed_ips=["fd00:10::5", "192.168.10.5"])
    out = apply_nic_overrides(fip_doc(), {"fa:16:3e:00:00:01": entry})
    [fip] = out["resources"][0]["params"]["floating_ips"]
    assert fip["params"]["fixed_ip_address"] == "192.168.10.5"
    # a network-only entry keeps the address, and the floating IP stays on it
    kept = apply_nic_overrides(fip_doc(), {"fa:16:3e:00:00:01": NicOverride(network="rhoso-dmz")})
    assert kept["resources"][0]["params"]["floating_ips"][0]["params"]["fixed_ip_address"] == (
        "10.0.0.5"
    )


def test_apply_nic_overrides_leaves_a_floating_ip_on_an_address_the_port_keeps():
    """SDD §7.2, review NC-06: a floating IP stays on its address when the override keeps it, also
    beside other addresses of its version - os-migrate attaches it by that address; only one that
    must move needs exactly one new address of its version."""
    from seamless_migrate.domain.models import NicOverride
    from seamless_migrate.executors.ansible import apply_nic_overrides

    for doc, entry in (
        # a network-only entry on a port with two IPv4 addresses
        (fip_doc(("10.0.0.5", "10.0.1.5")), NicOverride(network="rhoso-dmz")),
        # an entry whose addresses list the floating IP's among another
        (fip_doc(), NicOverride(fixed_ips=["10.0.1.5", "10.0.0.5"])),
    ):
        out = apply_nic_overrides(doc, {"fa:16:3e:00:00:01": entry})
        [fip] = out["resources"][0]["params"]["floating_ips"]
        assert fip["params"]["fixed_ip_address"] == "10.0.0.5"


def test_apply_nic_overrides_refuses_what_it_cannot_place_before_the_stop():
    """With no new address of the floating IP's version, with several, or with an entry whose MAC
    no exported port has, the cutover stops after the export, before the source stops."""
    from seamless_migrate.domain.models import NicOverride
    from seamless_migrate.executors.ansible import apply_nic_overrides

    for addresses, said in (
        (["fd00:10::5"], "no IPv4 address"),
        (["192.168.10.5", "192.168.10.6"], "2 IPv4 addresses"),
    ):
        entry = NicOverride(fixed_ips=addresses)
        with pytest.raises(PermanentStepError, match=said):
            apply_nic_overrides(fip_doc(), {"fa:16:3e:00:00:01": entry})
    with pytest.raises(PermanentStepError, match="fa:16:3e:00:00:99"):
        apply_nic_overrides(fip_doc(), {"fa:16:3e:00:00:99": NicOverride(network="rhoso-dmz")})


async def test_an_override_the_export_cannot_take_stops_the_cutover_before_the_stop(
    env, monkeypatch
):
    """The VM's NICs changed since validation: the cutover stops after the export, with only the
    planned server written for a rollback (never a same-named twin), and the import (which stops
    the source) never runs."""
    monkeypatch.setenv("ANSIBLE_FAKE_TWIN_ID", "srv-twin")
    cold, rec = nic_ctx(env, Strategy.cold, {"fa:16:3e:00:00:99": {"network": "rhoso-dmz"}})
    with pytest.raises(PermanentStepError, match="fa:16:3e:00:00:99"):
        await env.executor.run(StepName.CUTOVER, cold)
    assert [c["playbook"] for c in read_log(env.log)] == ["export_workloads.yml"]
    assert rec.downtime_marks == 0
    doc = yaml.safe_load((run_dir(env) / "osm" / "workloads.yml").read_text())
    servers = [r for r in doc["resources"] if r["type"] == "openstack.compute.Server"]
    assert [r["_info"]["id"] for r in servers] == ["srv-1"]


async def test_a_cutover_writes_the_vms_nic_overrides_into_workloads(env):
    """SDD §7.2: after the export the planned server's overridden port is rewritten."""
    entry = {"fa:16:3e:00:00:01": {"network": "rhoso-dmz", "fixed_ips": ["192.168.10.5"]}}
    cold, _ = nic_ctx(env, Strategy.cold, entry)
    await env.executor.run(StepName.CUTOVER, cold)
    [port] = exported_server(env)["params"]["ports"]
    assert port["params"]["network_ref"]["name"] == "rhoso-dmz"
    assert port["params"]["fixed_ips_refs"] == [{"ip_address": "192.168.10.5"}]
    # the marker a warm cutover exports again for, should the entry go (SDD §7.2)
    assert port["_migration_params"]["nic_override"] is True


async def test_a_warm_cutover_exports_again_for_the_vms_nic_overrides(env):
    """The export of the first pre-copy predates the entry: the cutover exports again and the
    overridden port goes where the entry says."""
    entry = {"fa:16:3e:00:00:01": {"network": "rhoso-dmz", "fixed_ips": ["192.168.10.5"]}}
    plain, _ = nic_ctx(env, Strategy.warm, None)
    await env.executor.run(StepName.PRECOPY, plain)
    warm, _ = nic_ctx(env, Strategy.warm, entry)
    await env.executor.run(StepName.CUTOVER, warm)
    assert [c["playbook"] for c in read_log(env.log)][-2:] == [
        "export_workloads.yml",
        "import_workloads_cutover.yml",
    ]
    assert exported_server(env)["params"]["ports"][0]["params"]["fixed_ips_refs"] == [
        {"ip_address": "192.168.10.5"}
    ]


async def test_a_warm_cutover_exports_again_when_an_override_was_removed(env):
    """An entry removed after the export left address-only references: the cutover exports the
    workload again, so the port goes back to its mapped network and subnet."""
    entry = {"fa:16:3e:00:00:01": {"network": "rhoso-dmz", "fixed_ips": ["192.168.10.5"]}}
    with_entry, _ = nic_ctx(env, Strategy.warm, entry)
    await env.executor.run(StepName.PRECOPY, with_entry)
    removed, _ = nic_ctx(env, Strategy.warm, None)
    await env.executor.run(StepName.CUTOVER, removed)
    assert [c["playbook"] for c in read_log(env.log)][-2:] == [
        "export_workloads.yml",
        "import_workloads_cutover.yml",
    ]
    port = exported_server(env)["params"]["ports"][0]
    # back to its mapped network with its own address, unmarked
    assert port["params"]["network_ref"]["name"] == "rhoso-app"
    assert port["params"]["fixed_ips_refs"] == [{"ip_address": "10.0.0.5"}]
    assert not (port.get("_migration_params") or {}).get("nic_override")


async def test_rewrite_workloads_keeps_only_the_planned_server(env, monkeypatch):
    """os-migrate exports workloads by name: a same-named server of the project that is not in
    the plan must never be imported, stopped or migrated (SDD §7.2)."""
    monkeypatch.setenv("ANSIBLE_FAKE_TWIN_ID", "srv-twin")
    ctx, _ = ctx_for(env)
    await env.executor.run(StepName.PRECOPY, ctx)
    exported = yaml.safe_load((run_dir(env) / "osm" / "workloads.yml").read_text())
    servers = [r for r in exported["resources"] if r["type"] == "openstack.compute.Server"]
    assert [s["_info"]["id"] for s in servers] == ["srv-1"]


async def test_rewrite_workloads_fails_without_the_planned_server(env, monkeypatch):
    """The export holds only a same-named server with another id: nothing of it is migrated."""
    monkeypatch.setenv("ANSIBLE_FAKE_SERVER_ID", "srv-other")
    ctx, _ = ctx_for(env)
    with pytest.raises(PermanentStepError, match="srv-1"):
        await env.executor.run(StepName.PRECOPY, ctx)
    assert [c["playbook"] for c in read_log(env.log)] == ["export_workloads.yml"]


async def test_warm_cutover_reports_destination_from_warm_state(env):
    ctx, rec = ctx_for(env)
    await env.executor.run(StepName.PRECOPY, ctx)
    result = await env.executor.run(StepName.CUTOVER, ctx)
    assert read_log(env.log)[-1]["playbook"] == "import_workloads_cutover.yml"
    assert result.destination_server_id == "dst-srv-1"
    assert result.sync_pass.kind == SyncPassKind.final
    assert rec.downtime_marks == 1


#: tags a scalar may carry in generated Ansible input: ``!unsafe`` strings and YAML's non-strings
NEVER_TEMPLATED = {
    "!unsafe",
    "tag:yaml.org,2002:bool",
    "tag:yaml.org,2002:int",
    "tag:yaml.org,2002:float",
    "tag:yaml.org,2002:null",
}
JINJA_NAME = "{{ lookup('pipe', 'id') }}{% if true %}-x{% endif %}"


def data_scalars(node, nested=False):
    """The scalar nodes of a YAML document that hold data: every value, and every key of a nested
    mapping (``dict2items`` turns a key into a value); the top-level keys are the variable names."""
    if isinstance(node, yaml.MappingNode):
        for key, value in node.value:
            if nested:
                yield from data_scalars(key, True)
            yield from data_scalars(value, True)
    elif isinstance(node, yaml.SequenceNode):
        for item in node.value:
            yield from data_scalars(item, True)
    elif isinstance(node, yaml.ScalarNode):
        yield node


@pytest.mark.parametrize(
    ("strategy", "source"), [(Strategy.vmware_warm, VCENTER), (Strategy.warm, SRC)]
)
async def test_generated_ansible_input_is_never_templated(env, strategy, source):
    """Security.md C1-05, SDD §7.2: Ansible templates every string it reads, so a VM name (chosen
    by whoever runs the source VM), a mapping value or a password holding Jinja would run on the
    control plane (``lookup('pipe', …)``). Every string in vars.yml, secrets.yml and the
    inventory — the values and the keys of nested mappings, such as a source network's name in
    ``network_map`` — is written ``!unsafe``, and the playbook still receives it unchanged."""
    networks = {**MAPPINGS.networks, JINJA_NAME: "rhoso-app"}
    mappings = MAPPINGS.model_copy(update={"networks": networks})
    # a port group named by whoever runs vCenter keys the kit's network_map (SDD §7.2)
    nics = [
        Nic(
            network=JINJA_NAME,
            mac="00:50:56:00:00:01",
            fixed_ips=["10.0.0.5"],
            network_key=JINJA_NAME,
        )
    ]
    ctx, _ = ctx_for(
        env, strategy=strategy, source=source, name=JINJA_NAME, mappings=mappings, nics=nics
    )
    await env.executor.run(StepName.PRECOPY, ctx)
    calls = read_log(env.log)
    assert calls
    for call in calls:
        assert {"vars.yml", "secrets.yml", "inventory.yml"} <= set(call["raw"])
        for name, text in call["raw"].items():
            root = yaml.compose(text)
            # only the top-level keys stay plain: variable names (group names), never data
            for key, _ in root.value:
                assert key.tag == "tag:yaml.org,2002:str", (call["playbook"], name, key.value)
                assert key.value.isidentifier(), (call["playbook"], name, key.value)
            for node in data_scalars(root):
                assert node.tag in NEVER_TEMPLATED, (call["playbook"], name, node.value)
    if strategy is Strategy.vmware_warm:
        assert calls[-1]["vars"]["vms_list"] == [JINJA_NAME]
        assert calls[-1]["vars"]["network_map"] == {
            JINJA_NAME: "net-rhoso-app-mine,sub-net-rhoso-app-mine"
        }
    else:
        [query] = calls[-1]["vars"]["os_migrate_workloads_filter"]
        assert re.search(query["regex"], JINJA_NAME)


def test_ansible_reads_generated_input_verbatim(tmp_path):
    """The real ``ansible-playbook`` keeps a Jinja VM name, network name and password literal
    instead of running them — a network name keys ``network_map``, and ``dict2items`` makes that
    key a value — and the run still works: modules execute through the ``!unsafe`` connection
    variables of the executor's inventory, its host variables resolve, and the nested secrets
    arrive unchanged."""
    playbook_bin = Path(sys.executable).with_name("ansible-playbook")
    if not playbook_bin.exists():
        pytest.skip("ansible-core is not installed (the collection extra)")
    marker = "INJECTED-BY-VM-NAME"
    name = "{{ lookup('pipe', 'echo " + marker + "') }}"
    (tmp_path / "vars.yml").write_text(
        ansible_yaml({"vms_list": [name], "network_map": {"a": name, name: "rhoso-app"}})
    )
    (tmp_path / "secrets.yml").write_text(
        ansible_yaml({"os_migrate_src_auth": {"username": "svc", "password": name + "%{x}"}})
    )
    # the executor's own inventory: local connection and interpreter are !unsafe strings, and so
    # are the keys below the group names (a conversion host, its variables)
    conversion = {"conv.example.invalid": {"ansible_ssh_user": "cloud-user"}}
    (tmp_path / "inventory.yml").write_text(build_inventory_doc(conversion))
    (tmp_path / "play.yml").write_text(
        "- hosts: migrator\n  gather_facts: false\n  tasks:\n"
        "    - ansible.builtin.debug:\n"
        '        msg: "name={{ vms_list[0] }} net={{ network_map.a }}"\n'
        "    - ansible.builtin.debug:\n"
        '        msg: "src={{ item.key }} dst={{ item.value }}"\n'
        '      loop: "{{ network_map | dict2items }}"\n'
        "    - ansible.builtin.debug:\n"
        "        msg: \"user={{ hostvars[groups['conversion_host'][0]].ansible_ssh_user }}\"\n"
        "    - ansible.builtin.debug:\n"
        '        msg: "pw={{ os_migrate_src_auth.password }}"\n'
        "    - ansible.builtin.ping:\n"
        "        data: '{{ os_migrate_src_auth.password }}'\n"
        "      register: pong\n"
        "    - ansible.builtin.assert:\n"
        "        that: pong.ping == os_migrate_src_auth.password\n"
    )
    run = subprocess.run(
        [
            str(playbook_bin),
            "-i",
            str(tmp_path / "inventory.yml"),
            str(tmp_path / "play.yml"),
            "-e",
            f"@{tmp_path / 'vars.yml'}",
            "-e",
            f"@{tmp_path / 'secrets.yml'}",
        ],
        capture_output=True,
        text=True,
        timeout=180,
        cwd=tmp_path,
        env={
            **os.environ,
            "ANSIBLE_LOCAL_TEMP": str(tmp_path / "tmp"),
            "ANSIBLE_NOCOLOR": "1",
            "ANSIBLE_STDOUT_CALLBACK": "default",
        },
    )
    assert run.returncode == 0, run.stdout + run.stderr
    assert f"name={name} net={name}" in run.stdout
    assert f"src={name} dst=rhoso-app" in run.stdout
    assert "user=cloud-user" in run.stdout
    assert marker not in run.stdout.replace(name, "")
    assert f"pw={name}%{{x}}" in run.stdout
    assert "ok=6" in run.stdout and "failed=0" in run.stdout


async def test_vmware_runs_give_the_kit_every_nic_by_its_key_and_refuse_before_it(env):
    """SDD §7.2: every kit run gets used_mapped_networks true and network_map[the kit's key] =
    "{network id},{subnet id}" of the RHOSO network each NIC's port group resolves to - mapped or
    same-named, a distributed port group by its key. A NIC that cannot go there stops the run
    before the kit, which powers the VM off at the cutover."""
    std = Nic(
        network="app-net",
        mac="00:50:56:00:00:01",
        fixed_ips=["10.0.0.5", "fe80::1"],
        network_key="app-net",
    )
    dvs = Nic(
        network="DC2-DMZ",
        mac="00:50:56:00:00:02",
        fixed_ips=["10.0.0.6"],
        network_key="dvportgroup-7",
    )
    # DC2-DMZ is mapped; app-net is not, and a RHOSO network of that name takes it
    maps = Mappings(networks={"DC2-DMZ": "rhoso-dmz"})
    ctx, rec = ctx_for(
        env, strategy=Strategy.vmware_cold, source=VCENTER, mappings=maps, nics=[std, dvs]
    )
    await env.executor.run(StepName.CUTOVER, ctx)
    v = read_log(env.log)[-1]["vars"]
    assert v["used_mapped_networks"] is True
    assert v["network_map"] == {
        "app-net": "net-app-net-mine,sub-net-app-net-mine",
        "dvportgroup-7": "net-rhoso-dmz-mine,sub-net-rhoso-dmz-mine",
    }

    async def refused(nics, error=PermanentStepError, roles=None):
        ran = len(read_log(env.log))
        destination = DST.model_copy(update={"capabilities": {"roles": roles}} if roles else {})
        plan = make_plan(id="plan-00c0ffee", mappings=maps)
        # another VM: the cutover above created a server named web-01
        mig = make_migration(
            id="mig-00000000aa",
            plan_id=plan.id,
            vm=make_vm(source_id="srv-2", name="db-01", nics=nics),
            strategy=Strategy.vmware_cold,
        )
        c, r = make_ctx(plan, mig, VCENTER, destination, env.settings)
        with pytest.raises(error) as exc:
            await env.executor.run(StepName.CUTOVER, c)
        # nothing ran: the VM keeps running
        assert len(read_log(env.log)) == ran and r.downtime_marks == 0
        return str(exc.value)

    gone = Nic(network="gone-net", mac="00:50:56:00:00:09", fixed_ips=["10.0.0.9"])
    assert await refused([std, gone]) == (
        "the NIC 00:50:56:00:00:09 of db-01 cannot go to RHOSO network 'gone-net': there is no "
        "network named 'gone-net'"
    )
    outside = std.model_copy(update={"fixed_ips": ["192.0.2.5"]})
    assert await refused([outside]) == (
        "the NIC 00:50:56:00:00:01 of db-01 on 'app-net': 192.0.2.5 is no host address of any "
        "subnet of the port's network"
    )
    # two NICs the kit maps by one key but that need different networks
    twin = dvs.model_copy(update={"network": "app-net", "mac": "00:50:56:00:00:03"})
    assert "shares the migration kit's key 'dvportgroup-7' with another NIC" in await refused(
        [dvs, twin]
    )
    # another project's shared network, from a member's token (SDD §6.6)
    env.registry.networks.append(dst_net("shared-net", own=False))
    on_shared = Nic(network="shared-net", mac="00:50:56:00:00:04", fixed_ips=["10.0.0.7"])
    assert "Neutron lets only the admin or advsvc role" in await refused(
        [on_shared], roles=["member", "reader"]
    )
    # the kit keeps the guest's network settings: a NIC override never moves a VMware NIC
    ran = len(read_log(env.log))
    moved = make_plan(
        id="plan-00c0ffee",
        mappings=maps,
        nic_overrides={"srv-2": {"00:50:56:00:00:01": {"network": "rhoso-dmz"}}},
    )
    mig = make_migration(
        id="mig-00000000aa",
        plan_id=moved.id,
        strategy=Strategy.vmware_warm,
        vm=make_vm(source_id="srv-2", name="db-01", nics=[std]),
    )
    c, _ = make_ctx(moved, mig, VCENTER, DST, env.settings)
    await env.executor.run(StepName.PRECOPY, c)
    assert len(read_log(env.log)) == ran + 1
    assert read_log(env.log)[-1]["vars"]["network_map"] == {
        "app-net": "net-app-net-mine,sub-net-app-net-mine"
    }
    # the destination's networks cannot be read: transient, before the kit
    env.registry.inventory_error = True
    assert "could not read the destination's networks" in await refused(
        [std], error=TransientStepError
    )
    assert rec.downtime_marks == 1


async def test_vmware_refuses_a_nic_validated_before_its_kit_key_was_recorded(env):
    """SDD §7.2: the kit looks a NIC on a distributed port group up by the group's key and drops
    one the map lacks. A NIC record without the key (a VM validated before it was recorded) is
    refused before the kit, never keyed by its port group's name (review RT-12)."""
    old = Nic(network="app-net", mac="00:50:56:00:00:01", fixed_ips=["10.0.0.5"])
    kit = {"strategy": Strategy.vmware_cold, "source": VCENTER, "mappings": Mappings()}
    ctx, rec = ctx_for(env, nics=[old], **kit)
    with pytest.raises(PermanentStepError) as exc:
        await env.executor.run(StepName.CUTOVER, ctx)
    assert str(exc.value) == (
        "the NIC 00:50:56:00:00:01 of web-01 was validated before the migration kit's key of its "
        "port group was recorded: validate the plan again"
    )
    # nothing ran: the VM keeps running
    assert read_log(env.log) == [] and rec.downtime_marks == 0
    # validated again, the record carries the key
    ctx, _ = ctx_for(env, nics=[old.model_copy(update={"network_key": "dvportgroup-7"})], **kit)
    await env.executor.run(StepName.CUTOVER, ctx)
    assert read_log(env.log)[-1]["vars"]["network_map"] == {
        "dvportgroup-7": "net-app-net-mine,sub-net-app-net-mine"
    }


@pytest.mark.parametrize("bare_first", [True, False])
async def test_vmware_nics_of_one_key_take_the_subnet_the_one_with_an_address_needs(
    env, bare_first
):
    """SDD §7.2: the kit gives every NIC of one key one value. A NIC with no IPv4 address needs no
    particular subnet, so it takes the one another NIC of its key needs, in either order; the run
    stops before the kit only when NICs of the key need different values (review RT-06)."""
    net = next(n for n in env.registry.networks if n["name"] == "app-net")
    env.registry.subnets.append({**dst_subnet(net, "10.1.0.0/24"), "id": "sub-b", "name": "b"})
    bare = Nic(
        network="app-net",
        mac="00:50:56:00:00:01",
        fixed_ips=["fe80::1"],
        network_key="dvportgroup-7",
    )
    in_b = Nic(
        network="app-net",
        mac="00:50:56:00:00:02",
        fixed_ips=["10.1.0.5"],
        network_key="dvportgroup-7",
    )
    nics = [bare, in_b] if bare_first else [in_b, bare]
    # app-net unmapped: the RHOSO network of that name takes it
    kit = {"strategy": Strategy.vmware_cold, "source": VCENTER, "mappings": Mappings()}
    ctx, _ = ctx_for(env, nics=nics, **kit)
    await env.executor.run(StepName.CUTOVER, ctx)
    assert read_log(env.log)[-1]["vars"]["network_map"] == {"dvportgroup-7": f"{net['id']},sub-b"}

    async def refused(nics):
        ran = len(read_log(env.log))
        c, r = ctx_for(env, name="db-01", nics=nics, **kit)
        with pytest.raises(PermanentStepError) as exc:
            await env.executor.run(StepName.CUTOVER, c)
        # nothing ran: the VM keeps running
        assert len(read_log(env.log)) == ran and r.downtime_marks == 0
        return str(exc.value)

    # two addresses of one key in different subnets: the kit's one value cannot take both
    in_a = in_b.model_copy(update={"mac": "00:50:56:00:00:03", "fixed_ips": ["10.0.0.5"]})
    assert await refused([in_a, in_b] if bare_first else [in_b, in_a]) == (
        "the NIC 00:50:56:00:00:02 of db-01 shares the migration kit's key 'dvportgroup-7' with "
        f"another NIC that goes to {net['id']},sub-{net['id']}, not {net['id']},sub-b"
        if bare_first
        else "the NIC 00:50:56:00:00:03 of db-01 shares the migration kit's key 'dvportgroup-7' "
        f"with another NIC that goes to {net['id']},sub-b, not {net['id']},sub-{net['id']}"
    )
    # a NIC with no IPv4 address on another network of the key: no subnet of app-net suits it
    elsewhere = bare.model_copy(update={"network": "rhoso-dmz"})
    dmz = next(n for n in env.registry.networks if n["name"] == "rhoso-dmz")
    assert await refused([elsewhere, in_b] if bare_first else [in_b, elsewhere]) == (
        "the NIC 00:50:56:00:00:01 of db-01 shares the migration kit's key 'dvportgroup-7' with "
        f"another NIC that goes to {net['id']},sub-b, not {dmz['id']},sub-{dmz['id']}"
    )


async def test_vmware_warm_flags(env):
    ctx, rec = ctx_for(env, strategy=Strategy.vmware_warm, source=VCENTER, nics=VMW_NICS)
    await env.executor.run(StepName.PRECOPY, ctx)
    first = read_log(env.log)[-1]
    assert first["playbook"] == KIT_PLAYBOOK == "os_migrate.vmware_migration_kit.migration"
    v = first["vars"]
    assert (v["cbt_sync"], v["cutover"]) == (True, False)
    assert v["vms_list"] == ["web-01"]
    assert v["vcenter_hostname"] == "vcenter.dc2.example"
    # SDD §7.2: every NIC by the kit's key to the RHOSO network and subnet Seamless resolved
    assert v["used_mapped_networks"] is True
    assert v["network_map"] == {"app-net": "net-rhoso-app-mine,sub-net-rhoso-app-mine"}
    assert v["use_fixed_ips"] is True and v["already_deploy_conversion_host"] is True
    assert v["cinder_volume_type"] == "rbd-hdd"
    assert v["os_migrate_vmw_data_dir"] == str(run_dir(env))
    assert {"vcenter_username", "vcenter_password", "vcenter_datacenter", "dst_cloud"} <= set(
        first["secret_keys"]
    )
    assert "auth" in first["dst_cloud_keys"]
    assert "conversion_host" in first["inventory"] and "192.0.2.10" in first["inventory"]
    assert rec.downtime_marks == 0

    await env.executor.run(StepName.CUTOVER, ctx)
    cut = read_log(env.log)[-1]["vars"]
    assert (cut["cbt_sync"], cut["cutover"]) == (True, True)
    assert rec.downtime_marks == 1

    # the warm cutover above created the server: a cold cutover of the same VM is a resume
    cold_ctx, _ = ctx_for(
        env,
        strategy=Strategy.vmware_cold,
        source=VCENTER,
        nics=VMW_NICS,
        downtime_started_at=utcnow(),
    )
    await env.executor.run(StepName.CUTOVER, cold_ctx)
    cold = read_log(env.log)[-1]["vars"]
    assert (cold["cbt_sync"], cold["cutover"]) == (False, True)


async def test_vmware_rollback_deletes_destination_and_powers_on_source(env):
    ctx, _ = ctx_for(
        env, strategy=Strategy.vmware_cold, source=VCENTER, destination_server_id="dst-9"
    )
    await env.executor.run(StepName.ROLLBACK, ctx)
    assert env.registry.calls == [("delete_server", "dst-9"), ("power_on", "srv-1")]
    assert read_log(env.log) == []


async def test_vmware_rollback_deletes_the_kits_ports_of_the_vm(env):
    """SDD §7.2: the kit creates each port and gives Nova its id, and Nova only unbinds such a port
    when it deletes the server - it would keep the NIC's MAC and addresses and refuse the next
    attempt. The rollback deletes, after the server, the unbound ports with one of the VM's NIC
    MACs and the kit's name form; any other port is kept (review RT-11)."""
    mac = "00:50:56:00:00:01"
    nic = Nic(network="app-net", mac=mac, fixed_ips=["10.0.0.5"], network_key="app-net")

    def port(port_id, name, mac=mac, device_id=""):
        return {
            "id": port_id,
            "name": name,
            "network_id": "net-app-net-mine",
            "device_id": device_id,
            "mac_address": mac,
            "fixed_ips": ["10.0.0.5"],
        }

    env.registry.ports = [
        port("kit", "web-01-NIC-0-VLAN-app-net"),
        port("bound", "web-01-NIC-0-VLAN-app-net", device_id="srv-other"),
        port("named-otherwise", "my-port"),
        port("other-mac", "web-01-NIC-1-VLAN-app-net", mac="00:50:56:00:00:99"),
    ]
    ctx, _ = ctx_for(
        env,
        strategy=Strategy.vmware_cold,
        source=VCENTER,
        nics=[nic],
        destination_server_id="dst-9",
    )
    result = await env.executor.run(StepName.ROLLBACK, ctx)
    assert env.registry.calls == [
        ("delete_server", "dst-9"),
        ("delete_port", "kit"),
        ("power_on", "srv-1"),
    ]
    assert result.details["deleted_ports"] == ["kit"]
    assert [p["id"] for p in env.registry.ports] == ["bound", "named-otherwise", "other-mac"]
    # ports that cannot be read: the rollback is retried, the source not yet started
    env.registry.calls.clear()
    env.registry.ports_error = True
    with pytest.raises(TransientStepError, match="rollback: ports: HTTP 503"):
        await env.executor.run(StepName.ROLLBACK, ctx)
    assert ("power_on", "srv-1") not in env.registry.calls


async def test_vmware_runs_refuse_a_nic_whose_mac_or_address_another_port_holds(env):
    """SDD §7.2: the kit asks Neutron for each NIC's MAC and IPv4 addresses after it powered the VM
    off; another port of the NIC's network holding either is refused before the kit, naming the
    port (review RT-11)."""
    net = next(n for n in env.registry.networks if n["name"] == "app-net")
    nic = Nic(
        network="app-net", mac="00:50:56:00:00:01", fixed_ips=["10.0.0.5"], network_key="app-net"
    )
    kit = {"strategy": Strategy.vmware_cold, "source": VCENTER, "mappings": Mappings()}

    async def refused(error=PermanentStepError):
        ran = len(read_log(env.log))
        ctx, rec = ctx_for(env, nics=[nic], **kit)
        with pytest.raises(error) as exc:
            await env.executor.run(StepName.CUTOVER, ctx)
        # nothing ran: the VM keeps running
        assert len(read_log(env.log)) == ran and rec.downtime_marks == 0
        return str(exc.value)

    held = {
        "id": "port-old",
        "name": "web-01-NIC-0-VLAN-app-net",
        "network_id": net["id"],
        "device_id": "",
        "mac_address": "00:50:56:00:00:01",
        "fixed_ips": ["10.0.0.9"],
    }
    env.registry.ports.append(held)
    assert await refused() == (
        "the NIC 00:50:56:00:00:01 of web-01 on 'app-net': port port-old holds its MAC "
        "00:50:56:00:00:01"
    )
    held.update(mac_address="fa:16:3e:00:00:09", fixed_ips=["10.0.0.5"])
    assert await refused() == (
        "the NIC 00:50:56:00:00:01 of web-01 on 'app-net': port port-old holds 10.0.0.5"
    )
    # ports that cannot be read: transient, before the kit
    env.registry.ports_error = True
    assert "could not read the destination's ports" in await refused(TransientStepError)
    env.registry.ports_error = False
    # a port of another network holds nothing the kit asks this network for
    held.update(network_id="net-elsewhere", mac_address="00:50:56:00:00:01")
    ctx, _ = ctx_for(env, nics=[nic], **kit)
    await env.executor.run(StepName.CUTOVER, ctx)
    assert read_log(env.log)[-1]["vars"]["network_map"] == {
        "app-net": f"{net['id']},sub-{net['id']}"
    }


async def test_a_warm_pass_returns_the_refusals_its_destination_check_recorded(env, monkeypatch):
    """SDD §6.6, §7.2: the warm role writes what the destination check found that the cutover
    will refuse beside the warm state; the pass returns it for the migration, a later pass that
    meets none returns none, and a run that left no such file is not read as the last one's."""
    problem = "Destination prerequisites not met: port p2 on network 'gone' does not resolve"
    monkeypatch.setenv("ANSIBLE_FAKE_REFUSALS", json.dumps([problem, problem]))
    ctx, _ = ctx_for(env)
    # once each
    assert (await env.executor.run(StepName.PRECOPY, ctx)).cutover_refusals == [problem]
    monkeypatch.delenv("ANSIBLE_FAKE_REFUSALS")
    assert (await env.executor.run(StepName.SYNC, ctx)).cutover_refusals == []
    # a stale file from an earlier pass is removed before the run
    stale = run_dir(env) / "osm" / "workload_warm" / "srv-1.refusals.json"
    stale.write_text(json.dumps(["stale"]))
    monkeypatch.setenv("ANSIBLE_FAKE_NO_REFUSALS_FILE", "1")
    assert (await env.executor.run(StepName.SYNC, ctx)).cutover_refusals == []
    # not a pass: nothing to record
    assert (await env.executor.run(StepName.CUTOVER, ctx)).cutover_refusals is None


async def test_a_vmware_warm_pass_returns_what_the_cutover_will_refuse(env):
    """SDD §7.2: a vmware_warm pass returns, once each, the problems it logs instead of refusing -
    and a pass after they are fixed returns none."""
    net = next(n for n in env.registry.networks if n["name"] == "app-net")
    nic = Nic(
        network="app-net", mac="00:50:56:00:00:01", fixed_ips=["10.0.0.5"], network_key="app-net"
    )
    kit = {"strategy": Strategy.vmware_warm, "source": VCENTER, "mappings": Mappings()}
    holder = {
        "id": "port-old",
        "name": "web-01-NIC-0-VLAN-app-net",
        "network_id": net["id"],
        "device_id": "",
        "mac_address": "00:50:56:00:00:01",
        "fixed_ips": ["10.0.0.9"],
    }
    env.registry.ports.append(holder)
    held = (
        "the NIC 00:50:56:00:00:01 of web-01 on 'app-net': port port-old holds its MAC "
        "00:50:56:00:00:01"
    )
    ctx, _ = ctx_for(env, nics=[nic], **kit)
    assert (await env.executor.run(StepName.PRECOPY, ctx)).cutover_refusals == [held]
    env.registry.ports.remove(holder)
    ctx, _ = ctx_for(env, nics=[nic], **kit)
    assert (await env.executor.run(StepName.SYNC, ctx)).cutover_refusals == []


async def test_a_vmware_warm_pass_logs_what_only_the_cutovers_ports_meet(env):
    """SDD §7.2: the kit makes the ports only at the cutover, after the power-off. A vmware_warm
    pre-copy or sync run maps a NIC whose network the credential may not use, or whose MAC or
    address another port holds, as usual and logs the problem, as it logs ports that cannot be
    read; the cutover refuses it. A NIC the map cannot take still stops every run (review
    NC-07)."""
    net = next(n for n in env.registry.networks if n["name"] == "app-net")
    nic = Nic(
        network="app-net", mac="00:50:56:00:00:01", fixed_ips=["10.0.0.5"], network_key="app-net"
    )
    kit = {"strategy": Strategy.vmware_warm, "source": VCENTER, "mappings": Mappings()}
    later = "the cutover will refuse this before the source stops: "
    env.registry.ports.append(
        {
            "id": "port-old",
            "name": "web-01-NIC-0-VLAN-app-net",
            "network_id": net["id"],
            "device_id": "",
            "mac_address": "00:50:56:00:00:01",
            "fixed_ips": ["10.0.0.9"],
        }
    )
    held = (
        "the NIC 00:50:56:00:00:01 of web-01 on 'app-net': port port-old holds its MAC "
        "00:50:56:00:00:01"
    )
    for step in (StepName.PRECOPY, StepName.SYNC):
        ctx, rec = ctx_for(env, nics=[nic], **kit)
        await env.executor.run(step, ctx)
        assert read_log(env.log)[-1]["vars"]["network_map"] == {
            "app-net": f"{net['id']},sub-{net['id']}"
        }
        assert rec.logs.count(later + held) == 1
        # logged before the kit ran
        assert rec.logs.index(later + held) < rec.logs.index(f"running {KIT_PLAYBOOK}")

    # the cutover refuses it before the power-off
    ran = len(read_log(env.log))
    ctx, rec = ctx_for(env, nics=[nic], **kit)
    with pytest.raises(PermanentStepError) as exc:
        await env.executor.run(StepName.CUTOVER, ctx)
    assert str(exc.value) == held
    assert len(read_log(env.log)) == ran and rec.downtime_marks == 0

    # ports that cannot be read: logged in a pass, transient at the cutover
    env.registry.ports_error = True
    ctx, rec = ctx_for(env, nics=[nic], **kit)
    await env.executor.run(StepName.SYNC, ctx)
    assert (
        "the NIC 00:50:56:00:00:01 of web-01 on 'app-net': could not read the destination's "
        "ports: ports: HTTP 503; the cutover reads them again before the power-off"
    ) in rec.logs
    assert not any(ln.startswith(later) for ln in rec.logs)
    ctx, _ = ctx_for(env, nics=[nic], **kit)
    with pytest.raises(TransientStepError, match="could not read the destination's ports"):
        await env.executor.run(StepName.CUTOVER, ctx)
    env.registry.ports_error = False

    # another project's shared network, from a member's token: mapped and logged in a pass
    env.registry.networks.append(dst_net("shared-net", own=False))
    shared = Nic(
        network="shared-net",
        mac="00:50:56:00:00:04",
        fixed_ips=["10.0.0.7"],
        network_key="dvportgroup-9",
    )
    member = DST.model_copy(update={"capabilities": {"roles": ["member", "reader"]}})
    plan = make_plan(id="plan-00c0ffee", mappings=Mappings())
    vm = make_vm(source_id="srv-1", name="web-01", nics=[shared])
    mig = make_migration(id="mig-00000000aa", plan_id=plan.id, vm=vm, strategy=Strategy.vmware_warm)
    ctx, rec = make_ctx(plan, mig, VCENTER, member, env.settings)
    await env.executor.run(StepName.PRECOPY, ctx)
    assert read_log(env.log)[-1]["vars"]["network_map"] == {
        "dvportgroup-9": "net-shared-net-theirs,sub-net-shared-net-theirs"
    }
    [refused] = [ln for ln in rec.logs if ln.startswith(later)]
    assert refused.startswith(
        later + "the NIC 00:50:56:00:00:04 of web-01 cannot go to RHOSO network 'shared-net' "
        "(project admin): "
    )
    assert "Neutron lets only the admin or advsvc role" in refused
    ctx, _ = make_ctx(plan, mig, VCENTER, member, env.settings)
    with pytest.raises(PermanentStepError, match="Neutron lets only the admin or advsvc role"):
        await env.executor.run(StepName.CUTOVER, ctx)

    # a NIC the map cannot take still stops a pass, before the kit
    ran = len(read_log(env.log))
    gone = Nic(
        network="gone-net", mac="00:50:56:00:00:09", fixed_ips=["10.0.0.9"], network_key="gone-net"
    )
    ctx, _ = ctx_for(env, nics=[nic, gone], **kit)
    with pytest.raises(PermanentStepError) as exc:
        await env.executor.run(StepName.PRECOPY, ctx)
    assert str(exc.value) == (
        "the NIC 00:50:56:00:00:09 of web-01 cannot go to RHOSO network 'gone-net': there is no "
        "network named 'gone-net'"
    )
    assert len(read_log(env.log)) == ran


async def test_progress_tail_reports_pct(env, monkeypatch):
    monkeypatch.setenv("ANSIBLE_FAKE_SLEEP", "0.5")
    ctx, rec = ctx_for(env, strategy=Strategy.cold)
    await env.executor.run(StepName.CUTOVER, ctx)
    pcts = [p[0] for p in rec.progress]
    assert 75.0 in pcts, pcts  # mean of {"/dev/vda": 50, "/dev/vdb": 100}
    assert any(line.startswith("TASK [") for line in rec.logs)


async def test_progress_tail_reads_the_state_file_named_by_the_escaped_stem(env, monkeypatch):
    """SDD §7.2: the progress file of a VM whose name holds a / is workload_logs/{stem}.state,
    as import_workload_prelim names it — never a path the tenant's name chose."""
    name = "../x/web%01"
    monkeypatch.setenv("ANSIBLE_FAKE_SLEEP", "0.5")
    monkeypatch.setenv("ANSIBLE_FAKE_VM_NAME", name)
    ctx, rec = ctx_for(env, strategy=Strategy.cold, name=name)
    await env.executor.run(StepName.CUTOVER, ctx)
    assert 75.0 in [p[0] for p in rec.progress]
    osm = run_dir(env) / "osm"
    assert (osm / "workload_logs" / "..%2Fx%2Fweb%2501.state").exists()
    assert not (osm / "x").exists()


def test_workload_file_stem_matches_the_collection():
    """SDD §7.2: the executor reads the file import_workload_prelim writes, so both apply the
    collection's rule (plugins/module_utils/filesystem.py, loaded without its imports)."""
    source = (
        Path(__file__).resolve().parents[2] / "plugins/module_utils/filesystem.py"
    ).read_text()
    tree = ast.parse(source)
    func = next(n for n in tree.body if getattr(n, "name", None) == "workload_file_stem")
    namespace: dict = {}
    exec(compile(ast.Module(body=[func], type_ignores=[]), "filesystem.py", "exec"), namespace)
    names = ["web-01", "web 01", "../../x", "/etc/shadow", "50%/x", "a\0b", "a%2Fb", "%%//"]
    assert [workload_file_stem(n) for n in names] == [
        namespace["workload_file_stem"](n) for n in names
    ]
    assert all("/" not in workload_file_stem(n) for n in names)


def test_the_collections_conversion_host_failures_are_classified_as_the_sdd_says():
    """SDD §6.6, QASuite R-07: a lock wait that cannot reach the conversion host is retried - the
    host may be rebooting - and a detach that never ends is not (review RV6-01). The messages are
    read from the collection's source, so rewording one cannot move it out of its class."""
    source = (
        Path(__file__).resolve().parents[2] / "plugins/module_utils/volume_common.py"
    ).read_text()
    templates = re.findall(r'"([^"]*%[sd][^"]*)"', source)
    unreachable = next(t for t in templates if "unreachable over SSH" in t)
    stuck = next(t for t in templates if "waiting to detach volume" in t)

    lock = "/var/lock/v2v-destination-volume-lock"
    assert classify_failure(unreachable % ("10.1.0.10", 66, lock)) is TransientStepError
    assert classify_failure(stuck % (1800, "vol-1", "srv-1")) is PermanentStepError


def test_transient_vs_permanent_failure_classifier():
    assert classify_failure("fatal: Timeout when waiting for 192.0.2.10:22") is TransientStepError
    assert classify_failure("keystoneauth: HTTP 503 Service Unavailable") is TransientStepError
    assert classify_failure("ssh: Connection reset by peer") is TransientStepError
    assert classify_failure("ERROR! couldn't resolve module/action") is PermanentStepError
    assert classify_failure("") is PermanentStepError


async def test_transient_vs_permanent_failure(env, monkeypatch):
    ctx, _ = ctx_for(env, strategy=Strategy.cold)
    monkeypatch.setenv("ANSIBLE_FAKE_FAIL_ON", "import_workloads.yml")
    monkeypatch.setenv("ANSIBLE_FAKE_FAIL", "Timeout when waiting for 192.0.2.10:22")
    with pytest.raises(TransientStepError, match="import_workloads.yml"):
        await env.executor.run(StepName.CUTOVER, ctx)
    monkeypatch.setenv("ANSIBLE_FAKE_FAIL", "Could not find a flavor named rhoso.small")
    with pytest.raises(PermanentStepError, match="flavor"):
        await env.executor.run(StepName.CUTOVER, ctx)
    assert not (run_dir(env) / "secrets.yml").exists()


async def test_a_failed_prestage_logs_a_redacted_tail(env, monkeypatch, caplog):
    """SDD §13.4: a failed pre-stage has no migration to log to, so the service log keeps its
    output's last 4000 characters — redacted over the last 4000 + 16384 first, so a PEM block
    whose BEGIN line lies before the kept part is still found."""
    pem = "-----BEGIN RSA PRIVATE KEY-----" + "K" * 5000 + "-----END RSA PRIVATE KEY-----"
    monkeypatch.setenv("ANSIBLE_FAKE_FAIL", '{"msg": "key rejected: ' + pem + '"}')
    plan = make_plan(id="plan-00c0ffee", prestage_resources=["networks"])
    with caplog.at_level(logging.WARNING, logger="seamless_migrate.executors.ansible"):
        with pytest.raises(PermanentStepError):
            await env.executor.prestage(plan, SRC, DST)
    [record] = [r for r in caplog.records if "last lines" in r.getMessage()]
    tail = record.getMessage().split("last lines:\n", 1)[1]
    assert "[REDACTED PEM]" in tail and "KKKK" not in tail and len(tail) <= 4000


async def test_prestage_runs_resources_in_order(env):
    plan = make_plan(id="plan-00c0ffee", prestage_resources=["networks", "security_groups"])
    await env.executor.prestage(plan, SRC, DST)
    calls = read_log(env.log)
    assert [c["playbook"] for c in calls] == [
        "export_networks.yml",
        "import_networks.yml",
        "export_security_groups.yml",
        "import_security_groups.yml",
        "deploy_conversion_hosts.yml",
    ]
    plan_osm = env.settings.data_dir / "plans" / "plan-00c0ffee" / "osm"
    assert all(c["vars"]["os_migrate_data_dir"] == str(plan_osm) for c in calls)
    assert not (plan_osm.parent / "secrets.yml").exists()

    before = len(calls)
    await env.executor.prestage(plan, SRC, DST, deploy_conversion_hosts=False)
    assert "deploy_conversion_hosts.yml" not in [c["playbook"] for c in read_log(env.log)[before:]]
    before = len(read_log(env.log))
    await env.executor.prestage(plan, VCENTER, DST)
    assert len(read_log(env.log)) == before, "VMware sources need no prestage"


async def test_prestage_scopes_flavors_to_the_plan(env):
    """SDD §7.2: only the flavors the orchestrator names (the plan's, which RHOSO lacks) are
    exported and imported, each anchored and escaped; without any, the flavor playbooks are
    skipped - os-migrate's default filter is every flavor, and its import rewrites the extra
    specs of a same-named destination flavor."""
    plan = make_plan(id="plan-00c0ffee", prestage_resources=["networks", "flavors"])
    await env.executor.prestage(
        plan, SRC, DST, deploy_conversion_hosts=False, prestage_flavors=["m1.custom", "c4.x(1)"]
    )
    calls = read_log(env.log)
    assert [c["playbook"] for c in calls] == [
        "export_networks.yml",
        "import_networks.yml",
        "export_flavors.yml",
        "import_flavors.yml",
    ]
    assert all(
        c["vars"]["os_migrate_flavors_filter"]
        == [{"regex": "^m1\\.custom$"}, {"regex": "^c4\\.x\\(1\\)$"}]
        for c in calls
    )
    for flavors in ([], None):
        before = len(read_log(env.log))
        kwargs = {} if flavors is None else {"prestage_flavors": flavors}
        await env.executor.prestage(plan, SRC, DST, deploy_conversion_hosts=False, **kwargs)
        assert [c["playbook"] for c in read_log(env.log)[before:]] == [
            "export_networks.yml",
            "import_networks.yml",
        ]


async def test_prestage_scopes_keypairs_to_the_plan(env):
    """SDD §7.2: keypairs are scoped like flavors - only the key_names the orchestrator names;
    without any, the keypair playbooks are skipped."""
    plan = make_plan(id="plan-00c0ffee", prestage_resources=["keypairs"])
    await env.executor.prestage(
        plan, SRC, DST, deploy_conversion_hosts=False, prestage_keypairs=["ops.key"]
    )
    calls = read_log(env.log)
    assert [c["playbook"] for c in calls] == ["export_keypairs.yml", "import_keypairs.yml"]
    assert all(c["vars"]["os_migrate_keypairs_filter"] == [{"regex": "^ops\\.key$"}] for c in calls)
    before = len(calls)
    await env.executor.prestage(plan, SRC, DST, deploy_conversion_hosts=False)
    assert read_log(env.log)[before:] == []


def test_build_vars_and_inventory_are_pure(env):
    ctx, _ = ctx_for(env, strategy=Strategy.cold)
    v1 = build_vars(StepName.CUTOVER, ctx)
    assert build_vars(StepName.CUTOVER, ctx) == v1
    assert v1["os_migrate_workload_stop_before_migration"] is True
    dumped = yaml.safe_dump(v1)  # no credentials: those only ever reach secrets.yml
    assert not any(k.endswith("_auth") for k in v1) and "spw" not in dumped
    assert "dpw" not in dumped and "must-not-leak" not in dumped
    inventory = load_ansible_yaml(build_inventory(ctx))
    assert inventory["migrator"]["hosts"]["localhost"]["ansible_connection"] == "local"


def test_build_vars_safety_pins_and_volume_types(env):
    """SDD §7.2: password SSH pinned off, SEC-03 CIDR, preserved volume types, cold rollback."""
    ctx, _ = ctx_for(env, strategy=Strategy.cold)
    v = build_vars(StepName.CUTOVER, ctx)
    assert v["os_migrate_conversion_host_ssh_user_enable_password_access"] is False
    assert "os_migrate_conversion_secgroup_remote_ip_prefix" not in v
    assert v["os_migrate_workloads_preserve_volume_type"] is True  # MAPPINGS maps ceph-hdd
    assert "os_migrate_rollback_match_by_name" not in v
    stopped, _ = ctx_for(env, strategy=Strategy.cold, downtime_started_at=utcnow())
    rollback = build_vars(StepName.ROLLBACK, stopped)
    assert rollback["os_migrate_rollback_match_by_name"] is True
    assert rollback["os_migrate_workload_stop_before_migration"] is False

    warm, _ = ctx_for(env, strategy=Strategy.warm)
    assert "os_migrate_rollback_match_by_name" not in build_vars(StepName.ROLLBACK, warm)
    plain = make_plan(id="plan-00c0ffee")
    bare = make_ctx(
        plain,
        make_migration(plan_id=plain.id, vm=make_vm(), strategy=Strategy.warm),
        SRC,
        DST,
        env.settings,
    )[0]
    assert build_vars(StepName.PRECOPY, bare)["os_migrate_workloads_preserve_volume_type"] is False

    cidr_src = make_provider(
        cloud="src",
        conversion_host=ConversionHostConfig(name="conv-src", ssh_allowed_cidr="10.1.0.0/24"),
    )
    cidr_dst = DST.model_copy(
        update={
            "conversion_host": ConversionHostConfig(
                name="conv-dst", address="192.0.2.10", ssh_allowed_cidr="10.2.0.0/24"
            )
        }
    )
    with_cidr = make_ctx(
        plain,
        make_migration(plan_id=plain.id, vm=make_vm(), strategy=Strategy.warm),
        cidr_src,
        cidr_dst,
        env.settings,
    )[0]
    assert (
        build_vars(StepName.PRECOPY, with_cidr)["os_migrate_conversion_secgroup_remote_ip_prefix"]
        == "10.2.0.0/24"
    )
    src_only = make_ctx(
        plain,
        make_migration(plan_id=plain.id, vm=make_vm(), strategy=Strategy.warm),
        cidr_src,
        DST,
        env.settings,
    )[0]
    assert (
        build_vars(StepName.PRECOPY, src_only)["os_migrate_conversion_secgroup_remote_ip_prefix"]
        == "10.1.0.0/24"
    )


def test_effective_mappings_overlay():
    resolved = Mappings(flavors={"custom.4x8": "m2.medium", "m1.small": "ignored"})
    merged = effective_mappings(MAPPINGS, resolved)
    assert merged.flavors == {"custom.4x8": "m2.medium", "m1.small": "rhoso.small"}
    assert merged.networks == MAPPINGS.networks and merged.volume_types == MAPPINGS.volume_types
    assert effective_mappings(MAPPINGS, None) == MAPPINGS
    assert effective_mappings(MAPPINGS, Mappings()) == MAPPINGS


def test_stop_task_pattern():
    hits = [
        "TASK [os_migrate.os_migrate.import_workloads_warm : Stop the source server] *****",
        "TASK [os_migrate.os_migrate.import_workloads : Perform workload stop if "
        "os_migrate_workload_stop_before_migration is true] ****",
        "TASK [os_migrate.vmware_migration_kit.migration : Power off the VM] ****",
        "TASK [migration : Poweroff source VM] ****",
        "TASK [migration : Shutdown VM before cutover] ****",
    ]
    assert all(STOP_TASK.match(line) for line in hits), hits
    misses = [
        "TASK [os_migrate.os_migrate.import_workloads_warm : Snapshot the stopped workload] ***",
        "ok: [localhost] => (item=Stop the source server)",
        "TASK [conversion_host : Stop the previous export of port 10809] ****",
    ]
    assert not any(STOP_TASK.match(line) for line in misses)


async def test_resolved_flavor_is_applied_to_the_export(env):
    plan = make_plan(id="plan-00c0ffee", mappings=Mappings(networks={"app-net": "rhoso-app"}))
    vm = make_vm(source_id="srv-1", name="web-01", flavor="m1.small")
    mig = make_migration(
        id="mig-00000000aa",
        plan_id=plan.id,
        vm=vm,
        strategy=Strategy.warm,
        resolved_mappings=Mappings(flavors={"m1.small": "auto.small"}),
    )
    ctx, _ = make_ctx(plan, mig, SRC, DST, env.settings)
    await env.executor.run(StepName.PRECOPY, ctx)
    exported = yaml.safe_load((run_dir(env) / "osm" / "workloads.yml").read_text())
    params = exported["resources"][0]["params"]
    assert params["flavor_ref"]["name"] == "auto.small"
    assert params["ports"][0]["params"]["network_ref"]["name"] == "rhoso-app"


async def test_downtime_clock_starts_at_the_stop_task(env, monkeypatch):
    """The mark happens when the stop task is printed, not when the playbook starts."""
    import time

    mark_file = env.settings.data_dir / "stop-mark"
    env.settings.data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ANSIBLE_FAKE_STOP_MARK", str(mark_file))
    monkeypatch.setenv("ANSIBLE_FAKE_SLEEP", "0.3")
    marks: list[float] = []

    ctx, rec = ctx_for(env, strategy=Strategy.warm)

    async def mark(at=None):
        marks.append(at.timestamp() if at is not None else time.time())
        rec.downtime_marks += 1

    ctx.mark_downtime_start = mark
    await env.executor.run(StepName.PRECOPY, ctx)
    assert marks == []  # the pre-copy playbook never stops the source
    await env.executor.run(StepName.CUTOVER, ctx)
    assert len(marks) == 1 and rec.downtime_marks == 1
    printed_at = float(mark_file.read_text())
    assert marks[0] >= printed_at - 0.01
    # …and not later either: a mark taken when the playbook ended (0.3 s of fake sleep after
    # the stop task) would under-report the downtime
    assert marks[0] <= printed_at + 0.2
    # pre-copy prints no stop task: the only candidate was the cutover playbook
    assert [c["playbook"] for c in read_log(env.log)][-1] == "import_workloads_cutover.yml"


async def test_vmware_conversion_host_key_secret(env, monkeypatch):
    """ssh_key_secret: the key is written 0600 for the run, used by the inventory, removed."""
    from seamless_migrate.security.secrets import SecretNotFound, resolve_private_key

    secret = env.settings.secrets_dir / "conv-key"
    secret.mkdir()
    (secret / "private_key").write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END\n")
    keyed = DST.model_copy(
        update={
            "conversion_host": ConversionHostConfig(
                name="conv-dst", address="192.0.2.10", ssh_key_secret="conv-key"
            )
        }
    )
    plan = make_plan(id="plan-00c0ffee", mappings=MAPPINGS)
    mig = make_migration(
        id="mig-00000000aa",
        plan_id=plan.id,
        vm=make_vm(source_id="srv-1", name="web-01", nics=VMW_NICS),
        strategy=Strategy.vmware_warm,
    )
    ctx, _ = make_ctx(plan, mig, VCENTER, keyed, env.settings)
    key_path = run_dir(env) / CONVERSION_KEY_FILE
    inventory = load_ansible_yaml(build_inventory(ctx))
    host = inventory["conversion_host"]["hosts"]["192.0.2.10"]
    assert host["ansible_ssh_private_key_file"] == str(key_path)
    assert host["ansible_ssh_user"] == "cloud-user"

    seen: dict[str, object] = {}
    original = env.executor._run_playbook

    async def spy(playbook, *args, **kw):
        seen["mode"] = oct(key_path.stat().st_mode & 0o777)
        seen["content"] = key_path.read_text()
        return await original(playbook, *args, **kw)

    monkeypatch.setattr(env.executor, "_run_playbook", spy)
    await env.executor.run(StepName.PRECOPY, ctx)
    assert seen["mode"] == "0o600" and seen["content"].startswith("-----BEGIN OPENSSH")
    assert not key_path.exists(), "the key file is removed after the run"

    missing = keyed.model_copy(
        update={
            "conversion_host": ConversionHostConfig(
                name="conv-dst", address="192.0.2.10", ssh_key_secret="no-such-key"
            )
        }
    )
    ctx_missing, _ = make_ctx(plan, mig, VCENTER, missing, env.settings)
    with pytest.raises(PermanentStepError, match="conversion host key"):
        await env.executor.run(StepName.PRECOPY, ctx_missing)
    with pytest.raises(SecretNotFound):
        resolve_private_key("no-such-key", env.settings, env={})
    env_key = resolve_private_key(
        "conv-key2", env.settings, env={"SEAMLESS_SECRET_CONV_KEY2_PRIVATE_KEY": "zzz"}
    )
    assert env_key == "zzz\n"
    with pytest.raises(SecretNotFound):
        resolve_private_key("../etc", env.settings, env={})


async def test_cancelled_step_kills_playbook_and_removes_secrets(env, monkeypatch):
    import asyncio

    monkeypatch.setenv("ANSIBLE_FAKE_SLEEP", "30")
    ctx, _ = ctx_for(env, strategy=Strategy.cold)
    task = asyncio.ensure_future(env.executor.run(StepName.CUTOVER, ctx))
    for _ in range(200):  # wait until the long-running import playbook started
        await asyncio.sleep(0.05)
        if any(c["playbook"] == "import_workloads.yml" for c in read_log(env.log)):
            break
    await asyncio.sleep(0.2)
    started = asyncio.get_running_loop().time()
    pid = int(next(c["pid"] for c in read_log(env.log) if c["playbook"] == "import_workloads.yml"))
    os.kill(pid, 0)  # alive while the step runs
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert asyncio.get_running_loop().time() - started < 15
    assert not (run_dir(env) / "secrets.yml").exists()
    assert not (run_dir(env) / "osm" / "clouds.yaml").exists()
    # the data mover must be dead, not merely abandoned
    for _ in range(100):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.05)
    else:
        raise AssertionError("the playbook process survived the cancel")


class FailingImpl:
    """Provider implementation whose every call fails with ProviderError."""

    def __init__(self, calls):
        self.calls = calls

    async def find_server(self, name):
        raise ProviderError(f"find {name}: connection refused")

    async def delete_server(self, server_id):
        raise ProviderError(f"delete {server_id}: HTTP 503")

    async def power_on(self, source_id):
        raise ProviderError(f"power on {source_id}: timeout")

    async def inventory(self):
        raise ProviderError("inventory: connection refused")

    async def ports(self, **filters):
        raise ProviderError("ports: connection refused")

    async def delete_port(self, port_id):
        raise ProviderError(f"delete port {port_id}: HTTP 503")


class FailingRegistry(FakeRegistry):
    def get(self, provider):
        return FailingImpl(self.calls)


async def test_finalize_variants(env):
    # nothing requested: nothing happens
    ctx, _ = ctx_for(env, strategy=Strategy.cold)
    assert (await env.executor.run(StepName.FINALIZE, ctx)).details == {"source_deleted": False}
    # delete_source on OpenStack deletes the source server
    plan = make_plan(id="plan-00c0ffee")
    mig = make_migration(plan_id=plan.id, vm=make_vm(source_id="srv-1"), strategy=Strategy.warm)
    ctx, _ = make_ctx(plan, mig, SRC, DST, env.settings, delete_source=True)
    assert (await env.executor.run(StepName.FINALIZE, ctx)).details == {"source_deleted": True}
    assert ("delete_server", "srv-1") in env.registry.calls
    # VMware sources are never deleted automatically in 0.1.0
    vmw = make_migration(
        plan_id=plan.id, vm=make_vm(source_id="vm-9"), strategy=Strategy.vmware_cold
    )
    ctx, _ = make_ctx(plan, vmw, VCENTER, DST, env.settings, delete_source=True)
    details = (await env.executor.run(StepName.FINALIZE, ctx)).details
    assert details["source_deleted"] is False and "not automated" in details["reason"]
    # a provider failure is transient (retried)
    failing = AnsibleExecutor(env.settings, providers=FailingRegistry(), poll_s=0.02)
    ctx, _ = make_ctx(plan, mig, SRC, DST, env.settings, delete_source=True)
    with pytest.raises(TransientStepError, match="finalize"):
        await failing.run(StepName.FINALIZE, ctx)


async def test_cutover_refuses_a_pre_existing_destination_server(env):
    """A same-named server the migration did not create blocks the cutover before any stop."""
    executor = AnsibleExecutor(
        env.settings, providers=FakeRegistry(existing={"web-01"}), poll_s=0.02
    )
    for strategy, source in ((Strategy.cold, SRC), (Strategy.vmware_cold, VCENTER)):
        ctx, rec = ctx_for(env, strategy=strategy, source=source)
        with pytest.raises(PermanentStepError, match="already exists in the destination"):
            await executor.run(StepName.CUTOVER, ctx)
        assert rec.downtime_marks == 0
    assert read_log(env.log) == []  # no playbook ran
    # a retry after the stop (a resume) or with a recorded server is not a collision
    ctx, rec = ctx_for(env, strategy=Strategy.cold, downtime_started_at=utcnow())
    result = await executor.run(StepName.CUTOVER, ctx)
    assert result.destination_server_id == "dst-web-01"


def test_cold_rollback_before_any_stop_never_matches_by_name(env):
    """SDD §7.2: the cold role creates the destination server after it stops the source, so
    before a stop a same-named server is not this migration's — a cutover refused or skipped
    for a name collision is rolled back without deleting it. A cancel's cleanup never matches by
    name, stopped or not (SDD §5.1, review RV-01): the migration's rollback already removed its
    server, and the VM it released may since be another plan's."""
    ctx, _ = ctx_for(env, strategy=Strategy.cold)
    assert ctx.migration.downtime_started_at is None
    assert "os_migrate_rollback_match_by_name" not in build_vars(StepName.ROLLBACK, ctx)
    stopped, _ = ctx_for(env, strategy=Strategy.cold, downtime_started_at=utcnow())
    assert build_vars(StepName.ROLLBACK, stopped)["os_migrate_rollback_match_by_name"] is True
    for cleanup in (ctx, stopped):
        cleanup.options["delete_dest_volumes"] = True
        assert "os_migrate_rollback_match_by_name" not in build_vars(StepName.ROLLBACK, cleanup)


def test_a_cancels_cleanup_touches_only_the_migrations_own_data_path(env):
    """SDD §5.1 (review RV-01, RV-02): the cleanup after a cancel - the rollback step with
    delete_dest_volumes - matches no server by name, deletes no port and leaves the source as it
    is: its rollback removed its server and ports and started its source, and the VM it released
    may since be another plan's. A rollback deletes its ports and starts its source as before."""
    for strategy in (Strategy.cold, Strategy.warm):
        cleanup, _ = ctx_for(
            env, strategy=strategy, downtime_started_at=utcnow(), downtime_ended_at=utcnow()
        )
        cleanup.options["delete_dest_volumes"] = True
        v = build_vars(StepName.ROLLBACK, cleanup)
        assert v["os_migrate_rollback_delete_dest_volumes"] is True, strategy
        assert v["os_migrate_rollback_start_source"] is False, strategy
        assert v["os_migrate_rollback_delete_ports"] is False, strategy
        assert "os_migrate_rollback_match_by_name" not in v, strategy
        rollback, _ = ctx_for(env, strategy=strategy, downtime_started_at=utcnow())
        v = build_vars(StepName.ROLLBACK, rollback)
        assert "os_migrate_rollback_start_source" not in v, strategy
        assert "os_migrate_rollback_delete_ports" not in v, strategy


def test_a_cold_rollback_deletes_the_volumes_the_cold_import_recorded(env):
    """SDD §6.3/§7 (the owner's choice, 2026-10-14): a cold retry never reuses the volumes the
    cold import created - its transfer creates new ones - so a cold rollback deletes exactly the
    volumes the import recorded; without such a record it deletes the server alone, as before,
    never a volume it cannot tell is the migration's. A warm rollback keeps its volumes for a
    retry."""
    stopped, _ = ctx_for(env, strategy=Strategy.cold, downtime_started_at=utcnow())
    assert "os_migrate_rollback_delete_dest_volumes" not in build_vars(StepName.ROLLBACK, stopped)
    path = run_dir(env) / "osm" / "workload_warm" / "srv-1.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"server_id": "srv-1", "server_name": "web-01", "dest_volumes": {}, "passes": []}
    # the server alone: the import copied no disk (data_copy false), it created no volume
    path.write_text(json.dumps({**record, "destination_server_id": "dst-1"}))
    assert "os_migrate_rollback_delete_dest_volumes" not in build_vars(StepName.ROLLBACK, stopped)
    record["dest_volumes"] = {"/dev/vdb": {"dest_id": "dvol-1"}}
    path.write_text(json.dumps(record))
    assert build_vars(StepName.ROLLBACK, stopped)["os_migrate_rollback_delete_dest_volumes"] is True
    # with no server recorded, a cold re-run removes them first: it never reuses them (SDD §7)
    assert build_vars(StepName.CUTOVER, stopped)["os_migrate_rollback_delete_dest_volumes"] is True
    warm, _ = ctx_for(env, strategy=Strategy.warm, downtime_started_at=utcnow())
    assert "os_migrate_rollback_delete_dest_volumes" not in build_vars(StepName.ROLLBACK, warm)


def _state_file(env, state):
    path = run_dir(env) / "osm" / "workload_warm" / "srv-1.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"server_id": "srv-1", "server_name": "web-01", **state}))
    return path


async def test_a_cold_cutover_first_removes_what_an_earlier_warm_pre_copy_left(env):
    """SDD §7 (COLD-02, the owner's choice, 2026-10-14): a warm migration rolled back keeps its
    pre-copy volumes for a retry; switched to cold, its cutover first runs the rollback playbook
    with the volumes deleted and the source left as it is (a retry may keep it stopped), matching
    no server by name - before the export and the stop."""
    _state_file(
        env,
        {
            "dest_volumes": {"/dev/vda": {"dest_id": "dvol-1", "size": 20, "bootable": True}},
            "passes": [{"number": 1, "kind": "full"}],
            "pending_snapshot": None,
            "destination_server_id": None,
        },
    )
    ctx, rec = ctx_for(env, strategy=Strategy.cold)
    await env.executor.run(StepName.CUTOVER, ctx)
    calls = read_log(env.log)
    assert [c["playbook"] for c in calls] == [
        "export_workloads.yml",
        "rollback_workloads.yml",
        "import_workloads.yml",
    ]
    cleanup = calls[1]["vars"]
    assert cleanup["os_migrate_rollback_delete_dest_volumes"] is True
    assert cleanup["os_migrate_rollback_start_source"] is False
    assert "os_migrate_rollback_match_by_name" not in cleanup
    assert rec.downtime_marks == 1  # the import stopped the source, the cleanup did not


async def test_a_cold_cutover_first_removes_the_volumes_an_earlier_cold_attempt_recorded(env):
    """SDD §7 (review lead B): a cold transfer that failed or was cut short recorded the volumes it
    created and no server; a cold re-run never reuses them, and its own record would replace
    theirs - so the cutover deletes them first, as a warm pre-copy's, leaving the source as it is
    (a retry keeps it stopped, its clock open) and matching no server by name."""
    _state_file(
        env,
        {
            "dest_volumes": {
                "/dev/vda": {"dest_id": "dvol-1", "size": 20, "bootable": True, "name": "web-01"}
            },
            "passes": [],
            "pending_snapshot": None,
            "destination_server_id": None,
        },
    )
    ctx, _ = ctx_for(env, strategy=Strategy.cold, downtime_started_at=utcnow())
    await env.executor.run(StepName.CUTOVER, ctx)
    calls = read_log(env.log)
    assert [c["playbook"] for c in calls] == [
        "export_workloads.yml",
        "rollback_workloads.yml",
        "import_workloads.yml",
    ]
    cleanup = calls[1]["vars"]
    assert cleanup["os_migrate_rollback_delete_dest_volumes"] is True
    assert cleanup["os_migrate_rollback_start_source"] is False
    assert "os_migrate_rollback_match_by_name" not in cleanup


# the export module's failure as Ansible prints it when a detach timed out (review RV6-01)
EXPORT_TIMED_OUT = (
    '{"changed": false, "module_stderr": "Traceback (most recent call last):\\n'
    '  File \\"volume_common.py\\", line 75, in detach_volume_bounded\\n'
    "openstack.exceptions.ResourceTimeout: Timeout waiting for Volume:vol-quorum to transition "
    'to available\\n", "msg": "MODULE FAILURE\\nSee stdout/stderr for the exact error", "rc": 1}'
)


async def test_a_retried_cold_cutover_refuses_an_export_without_a_disk_the_vm_was_validated_with(
    env, monkeypatch
):
    """SDD §7.2 (review RV6-01): attempt 1 stopped the VM and its export detached the shared disk,
    then failed transiently; the retry exports the VM again from Nova's attachments, without that
    disk - the step fails permanently, naming it, before any import playbook runs, rather than
    migrate the VM one disk short."""
    from tests.executor_support import make_ctx
    from tests.factories import make_disk, make_migration, make_plan, make_vm

    vm = make_vm(
        source_id="srv-1",
        name="web-01",
        disks=[
            make_disk(id="vol-1"),
            make_disk(id="vol-quorum", bootable=False, device="/dev/vdb", multiattach=True),
            make_disk(id="vol-data", bootable=False, device="/dev/vdc"),
        ],
    )
    plan = make_plan(id="plan-00c0ffee", mappings=MAPPINGS)
    mig = make_migration(id="mig-00000000aa", plan_id=plan.id, vm=vm, strategy=Strategy.cold)
    ctx, _ = make_ctx(plan, mig, SRC, DST, env.settings)

    # attempt 1: the export lists all three; the import then fails as a detach timeout does
    monkeypatch.setenv("ANSIBLE_FAKE_VOLUME_IDS", "vol-1,vol-quorum,vol-data")
    monkeypatch.setenv("ANSIBLE_FAKE_FAIL_ON", "import_workloads.yml")
    monkeypatch.setenv("ANSIBLE_FAKE_FAIL", EXPORT_TIMED_OUT)
    with pytest.raises(TransientStepError):
        await env.executor.run(StepName.CUTOVER, ctx)

    # the retry: Nova no longer lists the shared disk on the source VM
    monkeypatch.setenv("ANSIBLE_FAKE_VOLUME_IDS", "vol-1,vol-data")
    monkeypatch.delenv("ANSIBLE_FAKE_FAIL")
    before = len(read_log(env.log))
    with pytest.raises(PermanentStepError, match="vol-quorum"):
        await env.executor.run(StepName.CUTOVER, ctx)
    retried = [c["playbook"] for c in read_log(env.log)[before:]]
    assert "import_workloads.yml" not in retried
    assert retried[-1] == "export_workloads.yml"


async def test_the_export_is_checked_against_the_vms_volumes_only(env, monkeypatch):
    """SDD §7.2 (review RV6-01): an image-booted VM's root and ephemeral disks are no Cinder
    volumes and never appear among the exported volumes - only the validated volumes must."""
    from tests.executor_support import make_ctx
    from tests.factories import make_disk, make_migration, make_plan, make_vm

    vm = make_vm(
        source_id="srv-1",
        name="web-01",
        disks=[
            make_disk(id="srv-1-root", kind="image_root", volume_type=None),
            make_disk(id="srv-1-ephemeral", kind="ephemeral", bootable=False, device=None),
            make_disk(id="vol-data", bootable=False, device="/dev/vdb"),
        ],
    )
    plan = make_plan(id="plan-00c0ffee", mappings=MAPPINGS)
    mig = make_migration(id="mig-00000000aa", plan_id=plan.id, vm=vm, strategy=Strategy.cold)
    ctx, _ = make_ctx(plan, mig, SRC, DST, env.settings)
    monkeypatch.setenv("ANSIBLE_FAKE_VOLUME_IDS", "vol-data")

    await env.executor.run(StepName.CUTOVER, ctx)

    assert "import_workloads.yml" in [c["playbook"] for c in read_log(env.log)]


def test_a_cold_cutover_writes_the_workloads_record_before_its_playbooks(env, monkeypatch):
    """SDD §6.3, §7 (review RV3-03): with no record yet, the control plane writes one - no volume,
    pass or server - before a cold cutover's playbooks; a record already there is left as it is."""
    import asyncio

    path = run_dir(env) / "osm" / "workload_warm" / "srv-1.json"
    ctx, _ = ctx_for(env, strategy=Strategy.cold)
    asyncio.run(env.executor.run(StepName.CUTOVER, ctx))
    assert json.loads(path.read_text()) == {
        "server_id": "srv-1",
        "server_name": "web-01",
        "transfer_uuid": None,
        "dest_volumes": {},
        "passes": [],
        "pending_snapshot": None,
        "destination_server_id": None,
    }
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    recorded = {"server_id": "srv-1", "server_name": "web-01", "destination_server_id": "dst-1"}
    path.write_text(json.dumps(recorded))
    resumed, _ = ctx_for(env, strategy=Strategy.cold, downtime_started_at=utcnow())
    asyncio.run(env.executor.run(StepName.CUTOVER, resumed))
    assert json.loads(path.read_text()) == recorded
    # a warm cutover's record is the warm path's own: the control plane writes none for it
    path.unlink()
    written = []
    monkeypatch.setattr(
        "seamless_migrate.executors.ansible._ensure_record", lambda *args: written.append(args)
    )
    warm, _ = ctx_for(env, strategy=Strategy.warm, downtime_started_at=utcnow())
    asyncio.run(env.executor.run(StepName.CUTOVER, warm))
    assert written == []
    asyncio.run(env.executor.run(StepName.CUTOVER, resumed))
    assert written == [(run_dir(env) / "osm", "srv-1", "web-01")]


def test_the_workloads_record_is_synced_before_and_after_its_rename(tmp_path, monkeypatch):
    """SDD §6.3, §7 (review RV4-07): the record is synced whole before it is renamed into place,
    as WarmState.save syncs its file, and its directory after the rename - a crash right after the
    write leaves neither an empty nor a missing record, which would let a later rollback take a
    same-named server by its name."""
    import stat

    from seamless_migrate.executors.ansible import _ensure_record

    events = []
    real_fsync, real_replace = os.fsync, os.replace

    def fsync(fd):
        st = os.fstat(fd)
        events.append(("fsync", stat.S_ISDIR(st.st_mode), st.st_ino, st.st_size))
        real_fsync(fd)

    def replace(src, dst):
        events.append(("replace", os.stat(src).st_ino))
        real_replace(src, dst)

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "replace", replace)
    _ensure_record(tmp_path / "osm", "srv-1", "web-01")

    path = tmp_path / "osm" / "workload_warm" / "srv-1.json"
    record, directory = path.stat(), path.parent.stat()
    assert [event[:3] for event in events] == [
        ("fsync", False, record.st_ino),
        ("replace", record.st_ino),
        ("fsync", True, directory.st_ino),
    ]
    assert events[0][3] == record.st_size  # the whole record was written when it was synced


def test_a_directory_that_cannot_be_synced_still_gets_the_workloads_record(
    tmp_path, monkeypatch, caplog
):
    """Review RV4-07: some filesystems (network or FUSE mounts) refuse to sync a directory; the
    record is written and synced all the same, and the cutover goes on with a warning - failing
    every cold cutover there would gain nothing."""
    import errno
    import stat

    from seamless_migrate.executors.ansible import _ensure_record

    real_fsync = os.fsync

    def fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, "Invalid argument")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    with caplog.at_level(logging.WARNING, logger="seamless_migrate.executors.ansible"):
        _ensure_record(tmp_path / "osm", "srv-1", "web-01")

    path = tmp_path / "osm" / "workload_warm" / "srv-1.json"
    assert json.loads(path.read_text())["server_id"] == "srv-1"
    assert "could not sync" in caplog.text and "Invalid argument" in caplog.text


def test_a_workloads_record_that_cannot_be_synced_is_an_error(tmp_path, monkeypatch):
    """Review RV4-07: only the directory's sync is best-effort - a record file that cannot be
    synced fails its write, and so the cutover, before its playbooks stop the source."""
    import errno
    import stat

    from seamless_migrate.executors.ansible import _ensure_record

    real_fsync = os.fsync

    def fsync(fd):
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EIO, "Input/output error")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    with pytest.raises(OSError, match="Input/output error"):
        _ensure_record(tmp_path / "osm", "srv-1", "web-01")
    assert not (tmp_path / "osm" / "workload_warm" / "srv-1.json").exists()


def test_the_workloads_record_tells_whether_the_migration_keeps_copied_data(env):
    """SDD §5.1, §12 (review RV3-05, RV3-07): destination volumes or a pending source snapshot in
    the record are copied data; an emptied record is none; no record cannot tell; a record that
    cannot be read counts as data kept - its cleanup reports what it finds."""
    from seamless_migrate.executors.ansible import recorded_copied_data

    ctx, _ = ctx_for(env, strategy=Strategy.warm)
    m, settings = ctx.migration, env.executor.settings
    assert recorded_copied_data(settings, ctx.plan.id, m) is None
    path = _state_file(env, {"dest_volumes": {"/dev/vda": {"dest_id": "dvol-1"}}})
    assert recorded_copied_data(settings, ctx.plan.id, m) is True
    _state_file(env, {"dest_volumes": {}, "pending_snapshot": {"transfer_uuid": "uuid-1"}})
    assert recorded_copied_data(settings, ctx.plan.id, m) is True
    _state_file(env, {"dest_volumes": {}, "passes": [], "pending_snapshot": None})
    assert recorded_copied_data(settings, ctx.plan.id, m) is False
    path.write_text("{not json")
    assert recorded_copied_data(settings, ctx.plan.id, m) is True
    # a source id the collection would refuse as a file name is never made into a path: with
    # "../srv-1" the record would be read from outside workload_warm
    (path.parent.parent / "srv-1.json").write_text(json.dumps({"dest_volumes": {"x": {}}}))
    outside = m.model_copy(update={"vm": m.vm.model_copy(update={"source_id": "../srv-1"})})
    assert recorded_copied_data(settings, ctx.plan.id, outside) is None


@pytest.fixture
def collection(tmp_path_factory, monkeypatch):
    """The repository's collection, importable as ansible_collections.os_migrate.os_migrate."""
    layout = tmp_path_factory.mktemp("coll")
    namespace = layout / "ansible_collections" / "os_migrate"
    namespace.mkdir(parents=True)
    (namespace / "os_migrate").symlink_to(find_repo_root())
    monkeypatch.syspath_prepend(str(layout))
    importlib.invalidate_caches()
    package = "ansible_collections.os_migrate.os_migrate"
    wm = importlib.import_module(f"{package}.plugins.module_utils.warm_migration")
    fake = importlib.import_module(f"{package}.tests.unit.fake_openstack")
    return wm, fake


def role_rollback(wm, cloud, state_dir, variables):
    """What rollback.yml makes import_workload_rollback do with the variables the control plane
    passed (the role's defaults where it passed none)."""
    return wm.WarmRollback(
        cloud,
        wm.WarmState.load(str(state_dir), "srv-1"),
        server_name="web-01",
        match_by_name=bool(variables.get("os_migrate_rollback_match_by_name", False)),
        timeout=5,
        conversion_host=variables.get("os_migrate_dst_conversion_host_name"),
    ).run(delete_volumes=bool(variables.get("os_migrate_rollback_delete_dest_volumes", False)))


def _foreign_server(cloud):
    """Another plan's VM named web-01 in the destination project, with its boot volume."""
    cloud.add_server("plan-b-web-01", "web-01")
    cloud.add_volume(
        "plan-b-boot", "web-01", 20, bootable=True, server_id="plan-b-web-01", device="/dev/vda"
    )


async def test_after_a_resumed_cold_cutovers_cleanup_no_rollback_deletes_a_same_named_server(
    env, collection, monkeypatch
):
    """SDD §6.3, §7 (review RV3-03): the cleanup before a resumed cold cutover deletes the volumes
    an earlier transfer recorded and empties the record instead of removing it; when the cold role
    then skips the VM for a same-named server that appeared meanwhile, the rollback after the stop
    - match_by_name - still finds the record and never deletes that server."""
    wm, fake = collection
    path = _state_file(
        env,
        {
            "dest_volumes": {
                "/dev/vda": {"dest_id": "dvol-1", "size": 20, "bootable": True, "name": "web-01"}
            },
            "passes": [],
            "pending_snapshot": None,
            "destination_server_id": None,
        },
    )
    cloud = fake.FakeCloud("dst")
    cloud.add_conversion_host("conv-dst", "192.0.2.10")
    cloud.add_volume("dvol-1", "web-01", 20, bootable=True)
    _foreign_server(cloud)
    monkeypatch.setenv("ANSIBLE_FAKE_STOP_SKIPPED", "1")  # prelim skips web-01
    ctx, _ = ctx_for(env, strategy=Strategy.cold, downtime_started_at=utcnow())
    with pytest.raises(PermanentStepError, match="roll the migration back"):
        await env.executor.run(StepName.CUTOVER, ctx)
    calls = read_log(env.log)
    assert calls[1]["playbook"] == "rollback_workloads.yml"
    cleanup = role_rollback(wm, cloud, path.parent, calls[1]["vars"])
    assert cleanup["deleted_volume_ids"] == ["dvol-1"] and path.exists()
    env.log.write_text("")
    await env.executor.run(StepName.ROLLBACK, ctx)
    (rollback,) = read_log(env.log)
    assert rollback["vars"]["os_migrate_rollback_match_by_name"] is True
    role_rollback(wm, cloud, path.parent, rollback["vars"])
    assert "plan-b-web-01" in cloud.servers and "plan-b-boot" in cloud.volumes


async def test_a_cold_cutover_that_recorded_nothing_never_rolls_back_by_name(
    env, collection, monkeypatch
):
    """SDD §6.3, §7: a cold cutover that failed after the stop before its transfer recorded a
    volume still leaves the record the control plane wrote before its playbooks, so its rollback
    never takes a same-named server that appeared meanwhile for the migration's."""
    wm, fake = collection
    cloud = fake.FakeCloud("dst")
    cloud.add_conversion_host("conv-dst", "192.0.2.10")
    _foreign_server(cloud)
    monkeypatch.setenv("ANSIBLE_FAKE_STOP_SKIPPED", "1")
    ctx, _ = ctx_for(env, strategy=Strategy.cold, downtime_started_at=utcnow())
    with pytest.raises(PermanentStepError, match="roll the migration back"):
        await env.executor.run(StepName.CUTOVER, ctx)
    env.log.write_text("")
    await env.executor.run(StepName.ROLLBACK, ctx)
    (rollback,) = read_log(env.log)
    result = role_rollback(wm, cloud, run_dir(env) / "osm" / "workload_warm", rollback["vars"])
    assert result["deleted_server_id"] is None
    assert "plan-b-web-01" in cloud.servers and "plan-b-boot" in cloud.volumes


async def test_a_cold_cutover_keeps_a_cold_record_and_a_warm_record_with_its_server(env):
    """SDD §7: a cold record (volumes and a server, no pass) is a resumed cutover's own, and a
    warm record that names a destination server is not a pre-copy's leftover: neither is removed."""
    for state in (
        # the cold import's record of an interrupted attempt
        {
            "dest_volumes": {"/dev/vda": {"dest_id": "dvol-1"}},
            "passes": [],
            "destination_server_id": "dst-1",
        },
        # a warm cutover's server
        {
            "dest_volumes": {"/dev/vda": {"dest_id": "dvol-1"}},
            "passes": [{"number": 1}],
            "destination_server_id": "dst-1",
        },
    ):
        path = _state_file(env, state)
        if env.log.exists():
            env.log.write_text("")  # this case's runs only
        ctx, _ = ctx_for(env, strategy=Strategy.cold, downtime_started_at=utcnow())
        await env.executor.run(StepName.CUTOVER, ctx)
        assert [c["playbook"] for c in read_log(env.log)] == [
            "export_workloads.yml",
            "import_workloads.yml",
        ], state
        assert "os_migrate_rollback_start_source" not in build_vars(StepName.CUTOVER, ctx)
        path.unlink()
    # a pending source snapshot alone (a first pass cut short) is a leftover too
    _state_file(env, {"dest_volumes": {}, "passes": [], "pending_snapshot": {"transfer_uuid": "u"}})
    cold, _ = ctx_for(env, strategy=Strategy.cold)
    assert build_vars(StepName.CUTOVER, cold)["os_migrate_rollback_start_source"] is False
    warm, _ = ctx_for(env, strategy=Strategy.warm)
    assert "os_migrate_rollback_start_source" not in build_vars(StepName.CUTOVER, warm)


def _finished_cutover_state(env, record_server=True):
    """The warm state an attempt leaves when it ran the final pass and created the server, then
    failed (a timeout while the server booted, an error adding its floating IPs)."""
    path = run_dir(env) / "osm" / "workload_warm" / "srv-1.json"
    state = json.loads(path.read_text())
    state["passes"].append(dict(state["passes"][-1], number=2, kind="final"))
    if record_server:
        state["destination_server_id"] = "dst-srv-1"
    path.write_text(json.dumps(state))


async def test_warm_cutover_rerun_on_a_recorded_server_is_a_finished_cutover(env, monkeypatch):
    """SDD §7.2 (audit COL-05): the role finished the recorded server (§6.5); the re-run returns
    it and the final pass the failed attempt ran, instead of a permanent failure whose rollback
    would delete the good server."""
    ctx, _ = ctx_for(env, strategy=Strategy.warm)
    first = await env.executor.run(StepName.PRECOPY, ctx)
    _finished_cutover_state(env)
    monkeypatch.setenv("ANSIBLE_FAKE_SKIP_PASS", "1")  # the role finishes the server, no pass
    retry, _ = ctx_for(
        env, strategy=Strategy.warm, sync_passes=[first.sync_pass], downtime_started_at=utcnow()
    )
    result = await env.executor.run(StepName.CUTOVER, retry)
    assert result.destination_server_id == "dst-srv-1"
    assert result.sync_pass is not None
    assert (result.sync_pass.number, result.sync_pass.kind) == (2, SyncPassKind.final)

    # the migration already lists the final pass: it is not reported twice
    listed, _ = ctx_for(
        env,
        strategy=Strategy.warm,
        sync_passes=[first.sync_pass, result.sync_pass],
        downtime_started_at=utcnow(),
    )
    again = await env.executor.run(StepName.CUTOVER, listed)
    assert again.destination_server_id == "dst-srv-1" and again.sync_pass is None


async def test_warm_cutover_rerun_without_a_recorded_server_stays_a_permanent_failure(
    env, monkeypatch
):
    ctx, _ = ctx_for(env, strategy=Strategy.warm)
    first = await env.executor.run(StepName.PRECOPY, ctx)
    _finished_cutover_state(env, record_server=False)
    monkeypatch.setenv("ANSIBLE_FAKE_SKIP_PASS", "1")
    retry, _ = ctx_for(env, strategy=Strategy.warm, sync_passes=[first.sync_pass])
    with pytest.raises(PermanentStepError, match="recorded no new pass"):
        await env.executor.run(StepName.CUTOVER, retry)


async def test_a_vmware_cancels_cleanup_deletes_only_a_recorded_server(env):
    """SDD §7.2 (review RV-01, RV-02): a cancel's cleanup on VMware looks up no server by name,
    deletes no kit port and powers nothing on - another plan may migrate the VM it released, with
    a same-named server, kit ports of the VM's MACs and a source its cutover stopped."""
    mac = "00:50:56:00:00:01"
    nic = Nic(network="app-net", mac=mac, fixed_ips=["10.0.0.5"], network_key="app-net")
    env.registry.existing = {"web-01"}  # another plan's server of the VM
    env.registry.ports = [
        {
            "id": "kit-of-plan-b",
            "name": "web-01-NIC-0-VLAN-app-net",
            "network_id": "net-app-net-mine",
            "device_id": "",
            "mac_address": mac,
            "fixed_ips": ["10.0.0.5"],
        }
    ]
    for strategy in (Strategy.vmware_warm, Strategy.vmware_cold):
        env.registry.calls.clear()
        ctx, _ = ctx_for(
            env,
            strategy=strategy,
            source=VCENTER,
            nics=[nic],
            downtime_started_at=utcnow(),
            downtime_ended_at=utcnow(),
        )
        ctx.options["delete_dest_volumes"] = True
        await env.executor.run(StepName.ROLLBACK, ctx)
        assert env.registry.calls == [], strategy
    assert [p["id"] for p in env.registry.ports] == ["kit-of-plan-b"]
    # a server the migration recorded is its own: the cleanup deletes it
    recorded, _ = ctx_for(
        env,
        strategy=Strategy.vmware_warm,
        source=VCENTER,
        nics=[nic],
        destination_server_id="dst-9",
    )
    recorded.options["delete_dest_volumes"] = True
    await env.executor.run(StepName.ROLLBACK, recorded)
    assert env.registry.calls == [("delete_server", "dst-9")]


async def test_vmware_rollback_deletes_by_name_only_after_a_stop(env):
    """Without a recorded destination server, only a cutover that powered the VM off can have
    created a same-named server; an earlier failure leaves any such server alone."""
    registry = FakeRegistry(existing={"web-01"})
    executor = AnsibleExecutor(env.settings, providers=registry, poll_s=0.02)
    ctx, _ = ctx_for(env, strategy=Strategy.vmware_cold, source=VCENTER)
    result = await executor.run(StepName.ROLLBACK, ctx)
    assert registry.calls == [("power_on", "srv-1")]
    assert result.details["deleted_server"] is None
    registry.calls.clear()
    ctx, _ = ctx_for(
        env, strategy=Strategy.vmware_cold, source=VCENTER, downtime_started_at=utcnow()
    )
    result = await executor.run(StepName.ROLLBACK, ctx)
    assert registry.calls == [
        ("find_server", "web-01"),
        ("delete_server", "dst-web-01"),
        ("power_on", "srv-1"),
    ]
    assert result.details["deleted_server"] == "dst-web-01"


async def test_warm_pass_without_a_new_state_entry_is_a_permanent_failure(env, monkeypatch):
    ctx, _ = ctx_for(env, strategy=Strategy.warm)
    first = await env.executor.run(StepName.PRECOPY, ctx)
    assert first.sync_pass is not None and first.sync_pass.number == 1
    monkeypatch.setenv("ANSIBLE_FAKE_SKIP_PASS", "1")  # the role skipped the workload
    ctx, _ = ctx_for(env, strategy=Strategy.warm, sync_passes=[first.sync_pass])
    with pytest.raises(PermanentStepError, match="recorded no new pass"):
        await env.executor.run(StepName.SYNC, ctx)
    cut, _ = ctx_for(env, strategy=Strategy.warm, sync_passes=[first.sync_pass])
    with pytest.raises(PermanentStepError, match="recorded no new pass"):
        await env.executor.run(StepName.CUTOVER, cut)


async def test_a_huge_output_line_does_not_fail_the_step(env, monkeypatch):
    monkeypatch.setenv("ANSIBLE_FAKE_LONG_LINE", str(5 * 2**22))  # 20 MiB on one line
    ctx, rec = ctx_for(env, strategy=Strategy.warm)
    result = await env.executor.run(StepName.PRECOPY, ctx)
    assert result.sync_pass is not None
    assert any(line.startswith("ok: [localhost] => xxx") for line in rec.logs)


async def test_a_failing_steps_error_is_redacted_before_it_is_cut(env, monkeypatch):
    """SDD §13.4: the failing output line in a step's error is redacted over its cut plus 16384
    characters before it is cut to 500 — a PEM block that the cut splits would otherwise reach
    Migration.error, as the orchestrator's later redaction no longer recognizes it."""
    pem = "-----BEGIN RSA PRIVATE KEY-----" + "K" * 900 + "-----END RSA PRIVATE KEY-----"
    monkeypatch.setenv("ANSIBLE_FAKE_FAIL", '{"msg": "key rejected: ' + pem + '"}')
    ctx, _ = ctx_for(env, strategy=Strategy.warm)
    with pytest.raises(PermanentStepError) as failed:
        await env.executor.run(StepName.PRECOPY, ctx)
    message = str(failed.value)
    assert message.startswith("export_workloads.yml failed (exit 2): fatal: [localhost]: FAILED!")
    assert "[REDACTED PEM]" in message and "KKKK" not in message


async def test_output_lines_reach_the_log_whole(env, monkeypatch):
    """SDD §4, §7.1: the executor hands every output line to ``StepContext.log`` whole — the
    orchestrator redacts it before the cut to 2000 characters, so a cut here first could split a
    secret (a PEM block) before redaction sees its end."""
    monkeypatch.setenv("ANSIBLE_FAKE_LONG_LINE", "5000")
    ctx, rec = ctx_for(env, strategy=Strategy.warm)
    await env.executor.run(StepName.PRECOPY, ctx)
    assert "ok: [localhost] => " + "x" * 5000 in rec.logs


async def test_ansible_output_and_config_variables_are_not_forwarded(env, monkeypatch):
    for key in (
        "ANSIBLE_VERBOSITY",
        "ANSIBLE_LOG_PATH",
        "ANSIBLE_STDOUT_CALLBACK",
        "ANSIBLE_CONFIG",
    ):
        monkeypatch.setenv(key, "x")
    monkeypatch.setenv("ANSIBLE_SSH_ARGS", "-o ServerAliveInterval=30")
    ctx, _ = ctx_for(env, strategy=Strategy.warm)
    await env.executor.run(StepName.PRECOPY, ctx)
    keys = set(read_log(env.log)[-1]["env_keys"])
    assert {
        "ANSIBLE_VERBOSITY",
        "ANSIBLE_LOG_PATH",
        "ANSIBLE_STDOUT_CALLBACK",
        "ANSIBLE_CONFIG",
    }.isdisjoint(keys)
    assert {"ANSIBLE_SSH_ARGS", "ANSIBLE_DISPLAY_SKIPPED_HOSTS", "ANSIBLE_NOCOLOR"} <= keys
    # module temp directories live under the data dir (the image's passwd home is read-only)
    assert {"ANSIBLE_LOCAL_TEMP", "ANSIBLE_REMOTE_TEMP"} <= keys
    child = env.executor._env()
    assert child["ANSIBLE_REMOTE_TEMP"] == child["ANSIBLE_LOCAL_TEMP"]
    assert child["ANSIBLE_REMOTE_TEMP"].startswith(str(env.settings.data_dir))


async def test_vmware_rollback_and_lookup_failures(env):
    failing = AnsibleExecutor(env.settings, providers=FailingRegistry(), poll_s=0.02)
    ctx, rec = ctx_for(env, strategy=Strategy.vmware_warm, source=VCENTER)
    with pytest.raises(TransientStepError, match="rollback"):
        await failing.run(StepName.ROLLBACK, ctx)
    # a failed destination lookup after a cold cutover is logged, not fatal (a resumed cutover —
    # the source was already stopped — skips the collision check that a first attempt runs)
    cold, rec = ctx_for(env, strategy=Strategy.cold, downtime_started_at=utcnow())
    result = await failing.run(StepName.CUTOVER, cold)
    assert result.destination_server_id is None
    assert any("could not look up the destination server" in line for line in rec.logs)


async def test_cutover_refused_when_destination_lookup_fails(env):
    """The name-collision check fails closed: when the destination lookup errors, a first cutover
    attempt stops before any playbook runs — the VMware kit would power the source off first and
    then fail on a same-named server (Security.md R-12)."""
    failing = AnsibleExecutor(env.settings, providers=FailingRegistry(), poll_s=0.02)
    for strategy, source in ((Strategy.vmware_cold, VCENTER), (Strategy.cold, SRC)):
        ctx, rec = ctx_for(env, strategy=strategy, source=source)
        with pytest.raises(TransientStepError, match="could not check the destination"):
            await failing.run(StepName.CUTOVER, ctx)
        assert rec.downtime_marks == 0
    assert read_log(env.log) == []  # no playbook ran


async def test_run_rejects_unsupported_strategy_and_steps(env):
    ctx, _ = ctx_for(env, strategy=Strategy.storage_handover)
    with pytest.raises(PermanentStepError, match="does not handle"):
        await env.executor.run(StepName.CUTOVER, ctx)
    cold, _ = ctx_for(env, strategy=Strategy.cold)
    with pytest.raises(PermanentStepError, match="does not apply"):
        await env.executor.run(StepName.PRECOPY, cold)
    assert env.executor._playbooks(StepName.SYNC, Strategy.vmware_cold, run_dir(env)) == []


async def test_missing_ansible_playbook_binary_is_permanent(env):
    broken = Settings(
        data_dir=env.settings.data_dir,
        ansible_playbook=str(env.settings.data_dir / "no-such-ansible-playbook"),
        clouds_yaml=env.settings.clouds_yaml,
        secrets_dir=env.settings.secrets_dir,
        collection_root=env.settings.collection_root,
    )
    executor = AnsibleExecutor(broken, providers=env.registry, poll_s=0.02)
    plan = make_plan(id="plan-00c0ffee", mappings=MAPPINGS)
    mig = make_migration(id="mig-00000000aa", plan_id=plan.id, vm=make_vm(), strategy=Strategy.warm)
    ctx, _ = make_ctx(plan, mig, SRC, DST, broken)
    with pytest.raises(PermanentStepError, match="cannot run"):
        await executor.run(StepName.PRECOPY, ctx)
    assert not (run_dir(env) / "secrets.yml").exists()


def test_rewrite_workloads_and_warm_state_guards(tmp_path):
    from seamless_migrate.executors.ansible import _read_warm_state

    osm = tmp_path / "osm"
    osm.mkdir()
    with pytest.raises(PermanentStepError, match="did not produce"):
        AnsibleExecutor._rewrite_workloads(osm, MAPPINGS)
    (osm / "workloads.yml").write_text("resources:\n  - type: openstack.network.Network\n")
    with pytest.raises(PermanentStepError, match="contains no server"):
        AnsibleExecutor._rewrite_workloads(osm, MAPPINGS)
    assert _read_warm_state(osm, "srv-1") is None
    (osm / "workload_warm").mkdir()
    (osm / "workload_warm" / "srv-1.json").write_text("{not json")
    assert _read_warm_state(osm, "srv-1") is None


def test_secret_vars_auth_type_region_and_missing_cloud(env, tmp_path):
    from seamless_migrate.executors.ansible import build_secret_vars

    clouds = tmp_path / "clouds2.yaml"
    clouds.write_text(
        "clouds:\n"
        "  src:\n    auth_type: v3applicationcredential\n"
        "    auth: {auth_url: 'https://src/v3', application_credential_id: id, "
        "application_credential_secret: s}\n    region_name: regionOne\n"
        "  dst:\n    auth: {auth_url: 'https://dst/v3', username: du, password: dpw, "
        "project_name: finance}\n"
    )
    settings = Settings(data_dir=tmp_path / "data", clouds_yaml=clouds, secrets_dir=tmp_path / "s")
    out = build_secret_vars(SRC, DST, settings)
    assert out["os_migrate_src_auth_type"] == "v3applicationcredential"
    assert out["os_migrate_src_region_name"] == "regionOne"
    assert out["os_migrate_dst_region_name"] == "regionTwo", "Provider.region wins over clouds.yaml"
    assert "os_migrate_dst_auth_type" not in out
    missing = make_provider(cloud="nope")
    with pytest.raises(PermanentStepError, match="credentials unavailable"):
        build_secret_vars(missing, DST, settings)
    with pytest.raises(PermanentStepError, match="credentials unavailable"):
        build_secret_vars(VCENTER.model_copy(update={"credentials_secret": None}), DST, settings)


def test_lazy_provider_registry(env):
    from seamless_migrate.providers.registry import ProviderRegistry

    executor = AnsibleExecutor(env.settings)
    assert isinstance(executor.providers, ProviderRegistry)
    assert executor.providers is executor.providers


async def test_skipped_stop_task_starts_no_downtime_clock(env, monkeypatch):
    monkeypatch.setenv("ANSIBLE_FAKE_STOP_SKIPPED", "1")
    ctx, rec = ctx_for(env, strategy=Strategy.cold)
    # the cold role skips everything when the destination already has the server: nothing was
    # migrated, so the step fails instead of returning the same-named server as the result
    with pytest.raises(PermanentStepError, match="nothing was migrated"):
        await env.executor.run(StepName.CUTOVER, ctx)
    assert rec.downtime_marks == 0, "a skipped stop task does not stop the source"
    monkeypatch.delenv("ANSIBLE_FAKE_STOP_SKIPPED")
    ctx2, rec2 = ctx_for(env, strategy=Strategy.cold)
    await env.executor.run(StepName.CUTOVER, ctx2)
    assert rec2.downtime_marks == 1 and rec2.downtime_at[0] is not None
    assert (utcnow() - rec2.downtime_at[0]).total_seconds() < 60


def server_doc(*ports, floating=()):
    return {
        "resources": [
            {
                "type": "openstack.compute.Server",
                "params": {
                    "name": "web-01",
                    "ports": [
                        {
                            "params": {
                                "mac_address": mac,
                                "network_ref": {
                                    "name": network,
                                    "project_name": "finance",
                                    "domain_name": "Default",
                                },
                                "fixed_ips_refs": [
                                    {
                                        "ip_address": address,
                                        "subnet_ref": {
                                            "name": "app-subnet",
                                            "project_name": "finance",
                                        },
                                    }
                                ],
                            },
                            "_migration_params": {},
                        }
                        for mac, network, address in ports
                    ],
                    "floating_ips": [
                        {
                            "params": {
                                "fixed_ip_address": "10.0.0.5",
                                "floating_ip_address": address,
                                "floating_network_ref": {
                                    "name": network,
                                    "project_name": "admin",
                                    "domain_name": "Default",
                                },
                            }
                        }
                        for address, network in floating
                    ],
                },
            }
        ]
    }


def test_resolve_network_refs_points_each_port_at_the_network_the_cutover_finds():
    """SDD §7.2: the credential's own network keeps %auth% as the reference's project and domain;
    another project's network, the only one of its name, gets neither (os-migrate looks it up by
    name); every fixed IP reference becomes address-only."""
    networks = [
        dst_net("rhoso-app"),
        dst_net("rhoso-app", own=False),
        dst_net("provider-vlan9", own=False),
    ]
    doc = server_doc(
        ("fa:16:3e:00:00:01", "rhoso-app", "10.0.0.5"),
        ("fa:16:3e:00:00:02", "provider-vlan9", "172.16.9.5"),
        floating=[("203.0.113.10", "provider-vlan9")],
    )
    out = resolve_network_refs(doc, networks)
    own, shared = (p["params"] for p in out["resources"][0]["params"]["ports"])
    assert own["network_ref"] == {
        "name": "rhoso-app",
        "project_name": "%auth%",
        "domain_name": "%auth%",
    }
    assert shared["network_ref"] == {
        "name": "provider-vlan9",
        "project_name": None,
        "domain_name": None,
    }
    assert own["fixed_ips_refs"] == [{"ip_address": "10.0.0.5"}]
    assert shared["fixed_ips_refs"] == [{"ip_address": "172.16.9.5"}]
    [fip] = out["resources"][0]["params"]["floating_ips"]
    assert fip["params"]["floating_network_ref"] == {
        "name": "provider-vlan9",
        "project_name": None,
        "domain_name": None,
    }
    # a copy: the export itself is unchanged
    assert (
        doc["resources"][0]["params"]["ports"][0]["params"]["network_ref"]["project_name"]
        == "finance"
    )


def test_resolve_network_refs_refuses_a_name_it_cannot_resolve():
    """SDD §7.2: a port network the rule cannot resolve stops the cutover before the source stops,
    naming the port."""
    two = [dst_net("rhoso-app"), dst_net("rhoso-app", project_id="finance-rhoso-2")]
    two[1]["own"] = True
    doc = server_doc(("fa:16:3e:00:00:01", "rhoso-app", "10.0.0.5"))
    with pytest.raises(
        PermanentStepError, match="fa:16:3e:00:00:01.*2 networks of the credential's project"
    ):
        resolve_network_refs(doc, two)
    with pytest.raises(PermanentStepError, match="fa:16:3e:00:00:01.*no network named 'rhoso-app'"):
        resolve_network_refs(doc, [dst_net("other")])


def test_resolve_network_refs_keeps_a_floating_network_it_cannot_resolve():
    """SDD §7.2, review NC-10: a floating IP's network that does not resolve keeps its reference
    as exported - in auto mode a detached floating IP with the address needs no network, and the
    destination check refuses a network the import needs before the stop - while the port's is
    resolved as always."""
    doc = server_doc(
        ("fa:16:3e:00:00:01", "rhoso-app", "10.0.0.5"),
        floating=[("203.0.113.10", "gone"), ("203.0.113.11", "twice")],
    )
    networks = [dst_net("rhoso-app"), dst_net("twice"), {**dst_net("twice"), "id": "net-twice-2"}]
    out = resolve_network_refs(doc, networks)
    fips = out["resources"][0]["params"]["floating_ips"]
    assert [f["params"]["floating_network_ref"] for f in fips] == [
        {"name": "gone", "project_name": "admin", "domain_name": "Default"},
        {"name": "twice", "project_name": "admin", "domain_name": "Default"},
    ]
    port = out["resources"][0]["params"]["ports"][0]["params"]
    assert port["network_ref"] == {
        "name": "rhoso-app",
        "project_name": "%auth%",
        "domain_name": "%auth%",
    }


async def test_a_cutover_keeps_a_floating_network_it_cannot_resolve(env, monkeypatch):
    """SDD §7.2, review NC-10: RHOSO has no network named like the floating IP's - the cutover
    runs with the reference as exported, for the destination check to judge by the mode."""
    monkeypatch.setenv("ANSIBLE_FAKE_FLOATING", "203.0.113.7@public")
    cold, _ = ctx_for(env, strategy=Strategy.cold)
    await env.executor.run(StepName.CUTOVER, cold)
    assert [c["playbook"] for c in read_log(env.log)] == [
        "export_workloads.yml",
        "import_workloads.yml",
    ]
    [fip] = exported_server(env)["params"]["floating_ips"]
    assert fip["params"]["floating_network_ref"] == {
        "name": "public",
        "project_name": "admin",
        "domain_name": "Default",
    }


async def test_a_cutover_resolves_the_network_references_before_the_import(env):
    """SDD §7.2: after the export the port of a mapped network points at the RHOSO network the rule
    resolves - a shared one of another project by name alone - with address-only fixed IPs."""
    env.registry.networks = [dst_net("rhoso-app", own=False, project_id="admin")]
    cold, rec = ctx_for(env, strategy=Strategy.cold)
    await env.executor.run(StepName.CUTOVER, cold)
    [port] = exported_server(env)["params"]["ports"]
    assert port["params"]["network_ref"] == {
        "name": "rhoso-app",
        "project_name": None,
        "domain_name": None,
    }
    assert port["params"]["fixed_ips_refs"] == [{"ip_address": "10.0.0.5"}]
    assert read_log(env.log)[-1]["playbook"] == "import_workloads.yml"


async def test_a_cutover_stops_before_the_import_when_a_port_network_cannot_be_resolved(env):
    """Two networks of the credential's project carry the mapped name: the cutover stops after the
    export, before anything is stopped."""
    env.registry.networks = [dst_net("rhoso-app"), dst_net("rhoso-app", project_id="finance-rhoso")]
    env.registry.networks[1]["id"] = "net-rhoso-app-second"
    cold, rec = ctx_for(env, strategy=Strategy.cold)
    with pytest.raises(PermanentStepError, match="2 networks of the credential's project"):
        await env.executor.run(StepName.CUTOVER, cold)
    assert [c["playbook"] for c in read_log(env.log)] == ["export_workloads.yml"]
    assert rec.downtime_marks == 0


async def test_a_warm_cutover_resolves_its_export_again_without_exporting(env):
    """SDD §7.2: each run resolves the references of the existing export again - here the mapped
    network is now another project's - and only an override marker exports again."""
    warm, _ = ctx_for(env)
    await env.executor.run(StepName.PRECOPY, warm)
    assert (
        exported_server(env)["params"]["ports"][0]["params"]["network_ref"]["project_name"]
        == "%auth%"
    )
    env.registry.networks = [dst_net("rhoso-app", own=False, project_id="admin")]
    later, _ = ctx_for(env)
    await env.executor.run(StepName.CUTOVER, later)
    assert [c["playbook"] for c in read_log(env.log)][-1:] == ["import_workloads_cutover.yml"]
    assert "export_workloads.yml" not in [c["playbook"] for c in read_log(env.log)][1:]
    assert (
        exported_server(env)["params"]["ports"][0]["params"]["network_ref"]["project_name"] is None
    )


async def test_a_warm_pass_logs_a_port_network_it_cannot_resolve_and_the_cutover_refuses_it(env):
    """SDD §7.2: a warm pre-copy or sync run makes no port. A port's network name that does not
    resolve is logged, and the port keeps its references as exported; the cutover resolves them
    again and stops there, before the source stops (review NC-07)."""
    env.registry.networks = [dst_net("rhoso-app"), dst_net("rhoso-app", project_id="finance-rhoso")]
    env.registry.networks[1]["id"] = "net-rhoso-app-second"
    problem = (
        "the cutover will refuse this before the source stops: the port fa:16:3e:00:00:01 of "
        "web-01 cannot go to RHOSO network 'rhoso-app': "
    )
    precopy, rec = ctx_for(env)
    result = await env.executor.run(StepName.PRECOPY, precopy)
    assert result.sync_pass is not None
    assert [c["playbook"] for c in read_log(env.log)] == [
        "export_workloads.yml",
        "import_workloads_precopy.yml",
    ]
    [line] = [ln for ln in rec.logs if ln.startswith(problem)]
    assert "2 networks of the credential's project" in line
    # logged before the pass ran
    assert rec.logs.index(line) < rec.logs.index("running import_workloads_precopy.yml")
    # and returned for the migration's cutover_refusals (SDD §7.2)
    lead = "the cutover will refuse this before the source stops: "
    assert result.cutover_refusals == [line[len(lead) :]]
    # kept as exported, the mapping applied: the destination check (SDD §6.6) warns about it
    [port] = exported_server(env)["params"]["ports"]
    assert port["params"]["network_ref"] == {
        "name": "rhoso-app",
        "project_name": "finance-rhoso",
        "domain_name": "Default",
    }
    assert port["params"]["fixed_ips_refs"][0]["subnet_ref"]["name"] == "app-subnet"
    # the rewrite finished: the next run does not export again
    assert (run_dir(env) / "osm" / "rewrite.json").exists()

    sync, rec = ctx_for(env)
    synced = await env.executor.run(StepName.SYNC, sync)
    assert [c["playbook"] for c in read_log(env.log)][2:] == ["import_workloads_precopy.yml"]
    assert sum(ln.startswith(problem) for ln in rec.logs) == 1
    assert synced.cutover_refusals == [line[len(lead) :]]

    cutover, rec = ctx_for(env)
    with pytest.raises(PermanentStepError, match="2 networks of the credential's project"):
        await env.executor.run(StepName.CUTOVER, cutover)
    assert [c["playbook"] for c in read_log(env.log)][3:] == []
    assert rec.downtime_marks == 0
    assert not any(ln.startswith(problem) for ln in rec.logs)

    # the operator maps the network to one that resolves: the retry exports again, resolved
    retry, rec = ctx_for(env, mappings=_edited(networks={"app-net": "rhoso-dmz"}))
    env.registry.networks.append(dst_net("rhoso-dmz"))
    await env.executor.run(StepName.CUTOVER, retry)
    assert [c["playbook"] for c in read_log(env.log)][3:] == [
        "export_workloads.yml",
        "import_workloads_cutover.yml",
    ]
    assert exported_server(env)["params"]["ports"][0]["params"]["network_ref"] == {
        "name": "rhoso-dmz",
        "project_name": "%auth%",
        "domain_name": "%auth%",
    }


async def test_a_pass_logs_each_problem_it_defers_once():
    """SDD §7.2: what a warm pass does not refuse is logged once, however many places look."""
    logged: list[str] = []

    async def log(line):
        logged.append(line)

    ctx = SimpleNamespace(log=log)
    problem = "the port fa:16:3e:00:00:01 of web-01 cannot go to RHOSO network 'gone'"
    deferred = Deferred()
    deferred.append(problem)
    await AnsibleExecutor._log_deferred(ctx, deferred)
    await AnsibleExecutor._log_deferred(ctx, deferred)
    # a cutover defers nothing
    await AnsibleExecutor._log_deferred(ctx, None)
    assert logged == [
        "the cutover will refuse this before the source stops: the port fa:16:3e:00:00:01 of "
        "web-01 cannot go to RHOSO network 'gone'"
    ]
    # logged once, and kept for the pass's cutover_refusals (SDD §7.2)
    assert list(deferred) == [problem]


async def test_an_unreadable_destination_keeps_the_exported_network_references(env):
    """SDD §7.2: the destination's networks cannot be read - the run logs it and keeps the
    references as exported; the destination check (§6.6) still looks them up before the stop."""
    env.registry.inventory_error = True
    cold, rec = ctx_for(env, strategy=Strategy.cold)
    await env.executor.run(StepName.CUTOVER, cold)
    [port] = exported_server(env)["params"]["ports"]
    assert port["params"]["fixed_ips_refs"][0]["subnet_ref"]["name"] == "app-subnet"
    assert any("could not read the destination's networks" in line for line in rec.logs)


async def test_a_rollback_never_resolves_network_references(env):
    """SDD §7.2: the references are resolved for the runs that import; a rollback must not be held
    up by a network that no longer resolves."""
    warm, _ = ctx_for(env)
    await env.executor.run(StepName.PRECOPY, warm)
    env.registry.networks = [dst_net("rhoso-app"), dst_net("rhoso-app", project_id="other")]
    env.registry.networks[1]["own"] = True
    env.registry.networks[1]["id"] = "net-rhoso-app-second"
    reads = env.registry.calls.count(("inventory",))
    rollback, _ = ctx_for(env)
    await env.executor.run(StepName.ROLLBACK, rollback)
    assert read_log(env.log)[-1]["playbook"] == "rollback_workloads.yml"
    assert env.registry.calls.count(("inventory",)) == reads


async def test_a_cutover_that_exports_again_resolves_only_the_new_export(env):
    """SDD §7.2: when the cutover exports again (here for a new NIC override), the stale export is
    not resolved first - its network may no longer resolve, the new export's does."""
    warm, _ = ctx_for(env)
    await env.executor.run(StepName.PRECOPY, warm)
    # rhoso-app no longer resolves; the override sends the port to rhoso-dmz
    env.registry.networks = [
        dst_net("rhoso-app"),
        {**dst_net("rhoso-app"), "id": "net-rhoso-app-second"},
        dst_net("rhoso-dmz"),
    ]
    entry = {"fa:16:3e:00:00:01": {"network": "rhoso-dmz", "fixed_ips": ["192.168.10.5"]}}
    cutover, _ = nic_ctx(env, Strategy.warm, entry)
    await env.executor.run(StepName.CUTOVER, cutover)
    assert [c["playbook"] for c in read_log(env.log)][-2:] == [
        "export_workloads.yml",
        "import_workloads_cutover.yml",
    ]
    [port] = exported_server(env)["params"]["ports"]
    assert port["params"]["network_ref"] == {
        "name": "rhoso-dmz",
        "project_name": "%auth%",
        "domain_name": "%auth%",
    }


def _edited(**changes):
    """MAPPINGS as the operator edited them after a failed attempt (SDD §12 PATCH /plans/{id})."""
    return Mappings(**{**MAPPINGS.model_dump(), **changes})


def _ambiguous(env, name):
    """``name`` no longer resolves: two networks of the credential's project carry it."""
    env.registry.networks = [n for n in env.registry.networks if n["name"] != name] + [
        dst_net(name),
        {**dst_net(name), "id": f"net-{name}-second"},
    ]


async def test_a_cold_retry_with_a_fixed_mapping_never_resolves_the_export_it_replaces(env):
    """SDD §7.2, review NC-02: the mapped name did not resolve; the operator maps the network
    elsewhere and retries. The cutover exports again, so the name in the previous export must not
    hold it up."""
    _ambiguous(env, "rhoso-app")
    first, _ = ctx_for(env, strategy=Strategy.cold)
    with pytest.raises(PermanentStepError, match="2 networks of the credential's project"):
        await env.executor.run(StepName.CUTOVER, first)
    fixed = _edited(networks={"app-net": "rhoso-dmz", "DC2-Prod": "dc2-prod"})
    retry, _ = ctx_for(env, strategy=Strategy.cold, mappings=fixed)
    await env.executor.run(StepName.CUTOVER, retry)
    assert [c["playbook"] for c in read_log(env.log)][1:] == [
        "export_workloads.yml",
        "import_workloads.yml",
    ]
    port = exported_server(env)["params"]["ports"][0]["params"]
    assert port["network_ref"]["name"] == "rhoso-dmz"


async def test_a_cold_cutover_never_resolves_the_export_it_replaces(env, monkeypatch):
    """SDD §7.2: the cold cutover always exports again, so the export it replaces is never
    resolved - even when its record matches the plan, a name in it that no longer resolves must
    not hold up the new export."""
    monkeypatch.setenv("ANSIBLE_FAKE_FAIL_ON", "import_workloads.yml")
    monkeypatch.setenv("ANSIBLE_FAKE_FAIL", "Could not find a flavor named rhoso.small")
    first, _ = ctx_for(env, strategy=Strategy.cold)
    with pytest.raises(PermanentStepError, match="flavor"):
        await env.executor.run(StepName.CUTOVER, first)
    monkeypatch.delenv("ANSIBLE_FAKE_FAIL")
    osm = run_dir(env) / "osm"
    assert (osm / "rewrite.json").exists()
    doc = yaml.safe_load((osm / "workloads.yml").read_text())
    doc["resources"][0]["params"]["ports"][0]["params"]["network_ref"]["name"] = "gone-net"
    (osm / "workloads.yml").write_text(yaml.safe_dump(doc, sort_keys=False))
    retry, _ = ctx_for(env, strategy=Strategy.cold)
    await env.executor.run(StepName.CUTOVER, retry)
    assert [c["playbook"] for c in read_log(env.log)][2:] == [
        "export_workloads.yml",
        "import_workloads.yml",
    ]
    port = exported_server(env)["params"]["ports"][0]["params"]
    assert port["network_ref"]["name"] == "rhoso-app"


async def test_a_warm_run_exports_again_once_the_plans_mappings_changed(env):
    """SDD §7.2, review NC-02: a warm pass on an unchanged plan works on the export as it is; once
    the mappings differ from the export record (a plan edited after a failed attempt), the next
    pass exports again and the new mapping reaches the export."""
    warm, _ = ctx_for(env)
    first = await env.executor.run(StepName.PRECOPY, warm)
    same, _ = ctx_for(env, sync_passes=[first.sync_pass])
    await env.executor.run(StepName.SYNC, same)
    assert [c["playbook"] for c in read_log(env.log)] == [
        "export_workloads.yml",
        "import_workloads_precopy.yml",
        "import_workloads_precopy.yml",
    ]
    fixed = _edited(networks={"app-net": "rhoso-dmz", "DC2-Prod": "dc2-prod"})
    edited, _ = ctx_for(env, mappings=fixed, sync_passes=[first.sync_pass])
    await env.executor.run(StepName.SYNC, edited)
    assert [c["playbook"] for c in read_log(env.log)][3:] == [
        "export_workloads.yml",
        "import_workloads_precopy.yml",
    ]
    port = exported_server(env)["params"]["ports"][0]["params"]
    assert port["network_ref"]["name"] == "rhoso-dmz"


async def test_a_warm_cutover_exports_again_for_a_flavor_matched_anew(env):
    """SDD §7.2: the record holds the mappings the rewrite applied - the plan's overlaid with the
    migration's resolved ones - so a flavor that a re-validation matched anew reaches the
    cutover."""
    unmapped = _edited(flavors={})
    matched = Mappings(flavors={"m1.small": "rhoso.small"})
    warm, _ = ctx_for(env, mappings=unmapped, resolved_mappings=matched)
    first = await env.executor.run(StepName.PRECOPY, warm)
    assert exported_server(env)["params"]["flavor_ref"]["name"] == "rhoso.small"
    # the same match: the export as it is
    same, _ = ctx_for(
        env, mappings=unmapped, resolved_mappings=matched, sync_passes=[first.sync_pass]
    )
    await env.executor.run(StepName.SYNC, same)
    later, _ = ctx_for(
        env, mappings=unmapped, resolved_mappings=Mappings(flavors={"m1.small": "rhoso.medium"})
    )
    await env.executor.run(StepName.CUTOVER, later)
    assert [c["playbook"] for c in read_log(env.log)][2:] == [
        "import_workloads_precopy.yml",
        "export_workloads.yml",
        "import_workloads_cutover.yml",
    ]
    assert exported_server(env)["params"]["flavor_ref"]["name"] == "rhoso.medium"


async def test_the_export_record_holds_what_the_rewrite_applied(env):
    """SDD §7.2: beside the export, the mappings the rewrite applied and the VM's NIC override
    entries; removed before each export, so an export whose rewrite fails has none."""
    entry = {"fa:16:3e:00:00:01": {"network": "rhoso-dmz", "fixed_ips": ["192.168.10.5"]}}
    warm, _ = nic_ctx(
        env, Strategy.warm, entry, resolved_mappings=Mappings(flavors={"m1.large": "rhoso.large"})
    )
    await env.executor.run(StepName.PRECOPY, warm)
    record = run_dir(env) / "osm" / "rewrite.json"
    assert json.loads(record.read_text()) == {
        "mappings": {
            "networks": MAPPINGS.networks,
            "flavors": {"m1.large": "rhoso.large", "m1.small": "rhoso.small"},
            "volume_types": MAPPINGS.volume_types,
            "projects": MAPPINGS.projects,
        },
        "nic_overrides": {
            "fa:16:3e:00:00:01": {"network": "rhoso-dmz", "fixed_ips": ["192.168.10.5"]}
        },
        "name": "web-01",
    }
    # an entry whose MAC no exported port has: the rewrite fails after the export, before the stop
    gone, _ = nic_ctx(env, Strategy.warm, {"fa:16:3e:00:00:99": {"network": "rhoso-dmz"}})
    with pytest.raises(PermanentStepError, match="fa:16:3e:00:00:99"):
        await env.executor.run(StepName.CUTOVER, gone)
    assert not record.exists()


async def test_a_renamed_vm_is_exported_again_before_its_next_run(env, monkeypatch):
    """SDD §7.2 (review lead A): the export record holds the exported server's name - a source VM
    renamed since (and validated again) is exported again before its next run, which selects its
    workload by its name of now; the old export would match nothing."""
    first, _ = ctx_for(env, strategy=Strategy.warm)
    result = await env.executor.run(StepName.PRECOPY, first)
    env.log.write_text("")
    monkeypatch.setenv("ANSIBLE_FAKE_VM_NAME", "web-01-renamed")
    renamed, _ = ctx_for(env, strategy=Strategy.warm, sync_passes=[result.sync_pass])
    renamed.migration.vm = renamed.migration.vm.model_copy(update={"name": "web-01-renamed"})
    await env.executor.run(StepName.SYNC, renamed)
    assert [c["playbook"] for c in read_log(env.log)] == [
        "export_workloads.yml",
        "import_workloads_precopy.yml",
    ]
    assert json.loads((run_dir(env) / "osm" / "rewrite.json").read_text())["name"] == (
        "web-01-renamed"
    )


async def test_a_cold_cutovers_cleanup_selects_the_workload_by_its_name_of_now(env, monkeypatch):
    """SDD §7 (review lead A): the cleanup before a cold cutover runs after the cutover's export,
    so a source VM renamed since the earlier attempt is selected by its new name."""
    _state_file(
        env,
        {"dest_volumes": {"/dev/vda": {"dest_id": "dvol-1"}}, "passes": [{"number": 1}]},
    )
    monkeypatch.setenv("ANSIBLE_FAKE_VM_NAME", "web-01-renamed")
    ctx, _ = ctx_for(env, strategy=Strategy.cold)
    ctx.migration.vm = ctx.migration.vm.model_copy(update={"name": "web-01-renamed"})
    await env.executor.run(StepName.CUTOVER, ctx)
    calls = read_log(env.log)
    assert [c["playbook"] for c in calls] == [
        "export_workloads.yml",
        "rollback_workloads.yml",
        "import_workloads.yml",
    ]
    assert calls[1]["vars"]["os_migrate_workloads_filter"] == [{"regex": "^web\\-01\\-renamed$"}]
    assert calls[1]["servers"] == ["web-01-renamed"]  # what the cleanup reads: the new export


async def test_an_export_without_a_record_is_exported_again(env):
    """SDD §7.2, review NC-03: an export written before the record - here also before the
    nic_override marker - whose override was removed since. The next warm run exports again, so
    the port goes back to its mapped network."""
    entry = {"fa:16:3e:00:00:01": {"network": "rhoso-dmz", "fixed_ips": ["192.168.10.5"]}}
    with_entry, _ = nic_ctx(env, Strategy.warm, entry)
    await env.executor.run(StepName.PRECOPY, with_entry)
    osm = run_dir(env) / "osm"
    doc = yaml.safe_load((osm / "workloads.yml").read_text())
    for port in doc["resources"][0]["params"]["ports"]:
        port.get("_migration_params", {}).pop("nic_override", None)
    (osm / "workloads.yml").write_text(yaml.safe_dump(doc, sort_keys=False))
    (osm / "rewrite.json").unlink(missing_ok=True)
    removed, _ = nic_ctx(env, Strategy.warm, None)
    await env.executor.run(StepName.CUTOVER, removed)
    assert [c["playbook"] for c in read_log(env.log)][2:] == [
        "export_workloads.yml",
        "import_workloads_cutover.yml",
    ]
    port = exported_server(env)["params"]["ports"][0]["params"]
    assert port["network_ref"]["name"] == "rhoso-app"
    assert port["fixed_ips_refs"] == [{"ip_address": "10.0.0.5"}]


async def test_a_cutover_rerun_on_a_recorded_server_neither_exports_nor_resolves(env, monkeypatch):
    """SDD §7.2, review NC-07: the re-run only finishes the server the failed attempt created -
    its ports exist, and the plan cannot change while the source is stopped. It runs on the export
    as it is: the VM's NIC override does not export it again, and a network name that stopped
    resolving does not fail it (§8's automatic rollback would undo a finished cutover)."""
    entry = {"fa:16:3e:00:00:01": {"network": "rhoso-dmz", "fixed_ips": ["192.168.10.5"]}}
    warm, _ = nic_ctx(env, Strategy.warm, entry)
    first = await env.executor.run(StepName.PRECOPY, warm)
    _finished_cutover_state(env)
    monkeypatch.setenv("ANSIBLE_FAKE_SKIP_PASS", "1")  # the role finishes the server, no pass
    _ambiguous(env, "rhoso-dmz")
    reads = env.registry.calls.count(("inventory",))
    retry, _ = nic_ctx(
        env,
        Strategy.warm,
        entry,
        sync_passes=[first.sync_pass],
        downtime_started_at=utcnow(),
    )
    result = await env.executor.run(StepName.CUTOVER, retry)
    assert result.destination_server_id == "dst-srv-1"
    assert [c["playbook"] for c in read_log(env.log)][2:] == ["import_workloads_cutover.yml"]
    assert env.registry.calls.count(("inventory",)) == reads
    port = exported_server(env)["params"]["ports"][0]["params"]
    assert port["network_ref"] == {
        "name": "rhoso-dmz",
        "project_name": "%auth%",
        "domain_name": "%auth%",
    }


def test_resolve_network_refs_keeps_a_port_without_addresses_and_a_floating_ip_without_network():
    """A port without a fixed IP stays without one; a floating IP that names no network is left
    to os-migrate (SDD §7.2)."""
    doc = server_doc(
        ("fa:16:3e:00:00:01", "rhoso-app", "10.0.0.5"), floating=[("203.0.113.10", "x")]
    )
    server = doc["resources"][0]["params"]
    server["ports"][0]["params"]["fixed_ips_refs"] = []
    server["floating_ips"][0]["params"]["floating_network_ref"] = None
    out = resolve_network_refs(doc, [dst_net("rhoso-app")])
    params = out["resources"][0]["params"]
    assert params["ports"][0]["params"]["fixed_ips_refs"] == []
    assert params["floating_ips"][0]["params"]["floating_network_ref"] is None


async def test_a_destination_without_network_details_keeps_the_exported_references(env):
    """An inventory that lists no networks with their owners (an older provider) resolves
    nothing: the references stay as exported rather than refusing every port."""
    env.registry.networks = []
    cold, _ = ctx_for(env, strategy=Strategy.cold)
    await env.executor.run(StepName.CUTOVER, cold)
    [port] = exported_server(env)["params"]["ports"]
    assert port["params"]["fixed_ips_refs"][0]["subnet_ref"]["name"] == "app-subnet"
    assert read_log(env.log)[-1]["playbook"] == "import_workloads.yml"
