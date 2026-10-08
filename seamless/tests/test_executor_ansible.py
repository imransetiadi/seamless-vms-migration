import copy
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from seamless_migrate.config import Settings, find_repo_root
from seamless_migrate.domain.enums import ProviderKind, ProviderRole, Strategy, SyncPassKind
from seamless_migrate.domain.models import ConversionHostConfig, Mappings
from seamless_migrate.executors.ansible import (
    CONVERSION_KEY_FILE,
    KIT_PLAYBOOK,
    STOP_TASK,
    AnsibleExecutor,
    apply_mappings,
    build_inventory,
    build_vars,
    classify_failure,
    effective_mappings,
    workload_filter,
)
from seamless_migrate.executors.base import PermanentStepError, StepName, TransientStepError
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
MAPPINGS = Mappings(
    networks={"app-net": "rhoso-app", "DC2-Prod": "dc2-prod"},
    flavors={"m1.small": "rhoso.small"},
    volume_types={"ceph-hdd": "rbd-hdd"},
    projects={"finance": "finance-rhoso"},
)


class FakeImpl:
    def __init__(self, calls):
        self.calls = calls

    async def find_server(self, name):
        self.calls.append(("find_server", name))
        return f"dst-{name}"

    async def delete_server(self, server_id):
        self.calls.append(("delete_server", server_id))

    async def power_on(self, source_id):
        self.calls.append(("power_on", source_id))


class FakeRegistry:
    def __init__(self):
        self.calls = []

    def get(self, provider):
        return FakeImpl(self.calls)


@pytest.fixture
def env(tmp_path, monkeypatch):
    fake = install(tmp_path / "bin")
    log = tmp_path / "ansible.log"
    monkeypatch.setenv("ANSIBLE_FAKE_LOG", str(log))
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


def ctx_for(env, strategy=Strategy.warm, source=SRC, name="web-01", **mig_kw):
    plan = make_plan(id="plan-00c0ffee", mappings=MAPPINGS)
    vm = make_vm(source_id="srv-1", name=name)
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
    assert read_log(env.log)[-1]["playbook"] == "rollback_workloads.yml"
    assert not env.executor.supports(Strategy.storage_handover)


async def test_warm_cutover_reports_destination_from_warm_state(env):
    ctx, rec = ctx_for(env)
    await env.executor.run(StepName.PRECOPY, ctx)
    result = await env.executor.run(StepName.CUTOVER, ctx)
    assert read_log(env.log)[-1]["playbook"] == "import_workloads_cutover.yml"
    assert result.destination_server_id == "dst-srv-1"
    assert result.sync_pass.kind == SyncPassKind.final
    assert rec.downtime_marks == 1


async def test_vmware_warm_flags(env):
    ctx, rec = ctx_for(env, strategy=Strategy.vmware_warm, source=VCENTER)
    await env.executor.run(StepName.PRECOPY, ctx)
    first = read_log(env.log)[-1]
    assert first["playbook"] == KIT_PLAYBOOK == "os_migrate.vmware_migration_kit.migration"
    v = first["vars"]
    assert (v["cbt_sync"], v["cutover"]) == (True, False)
    assert v["vms_list"] == ["web-01"]
    assert v["vcenter_hostname"] == "vcenter.dc2.example"
    assert v["network_map"] == {"app-net": "rhoso-app", "DC2-Prod": "dc2-prod"}
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

    cold_ctx, _ = ctx_for(env, strategy=Strategy.vmware_cold, source=VCENTER)
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


async def test_progress_tail_reports_pct(env, monkeypatch):
    monkeypatch.setenv("ANSIBLE_FAKE_SLEEP", "0.5")
    ctx, rec = ctx_for(env, strategy=Strategy.cold)
    await env.executor.run(StepName.CUTOVER, ctx)
    pcts = [p[0] for p in rec.progress]
    assert 75.0 in pcts, pcts  # mean of {"/dev/vda": 50, "/dev/vdb": 100}
    assert any(line.startswith("TASK [") for line in rec.logs)


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


def test_build_vars_and_inventory_are_pure(env):
    ctx, _ = ctx_for(env, strategy=Strategy.cold)
    v1 = build_vars(StepName.CUTOVER, ctx)
    assert build_vars(StepName.CUTOVER, ctx) == v1
    assert v1["os_migrate_workload_stop_before_migration"] is True
    dumped = yaml.safe_dump(v1)  # no credentials: those only ever reach secrets.yml
    assert not any(k.endswith("_auth") for k in v1) and "spw" not in dumped
    assert "dpw" not in dumped and "must-not-leak" not in dumped
    inventory = yaml.safe_load(build_inventory(ctx))
    assert inventory["migrator"]["hosts"]["localhost"]["ansible_connection"] == "local"


def test_build_vars_safety_pins_and_volume_types(env):
    """SDD §7.2: password SSH pinned off, SEC-03 CIDR, preserved volume types, cold rollback."""
    ctx, _ = ctx_for(env, strategy=Strategy.cold)
    v = build_vars(StepName.CUTOVER, ctx)
    assert v["os_migrate_conversion_host_ssh_user_enable_password_access"] is False
    assert "os_migrate_conversion_secgroup_remote_ip_prefix" not in v
    assert v["os_migrate_workloads_preserve_volume_type"] is True  # MAPPINGS maps ceph-hdd
    assert "os_migrate_rollback_match_by_name" not in v
    rollback = build_vars(StepName.ROLLBACK, ctx)
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

    async def mark():
        marks.append(time.time())
        rec.downtime_marks += 1

    ctx.mark_downtime_start = mark
    await env.executor.run(StepName.PRECOPY, ctx)
    assert marks == []  # the pre-copy playbook never stops the source
    await env.executor.run(StepName.CUTOVER, ctx)
    assert len(marks) == 1 and rec.downtime_marks == 1
    printed_at = float(mark_file.read_text())
    assert marks[0] >= printed_at - 0.01
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
        vm=make_vm(source_id="srv-1", name="web-01"),
        strategy=Strategy.vmware_warm,
    )
    ctx, _ = make_ctx(plan, mig, VCENTER, keyed, env.settings)
    key_path = run_dir(env) / CONVERSION_KEY_FILE
    inventory = yaml.safe_load(build_inventory(ctx))
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
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert asyncio.get_running_loop().time() - started < 15
    assert not (run_dir(env) / "secrets.yml").exists()
    assert not (run_dir(env) / "osm" / "clouds.yaml").exists()
