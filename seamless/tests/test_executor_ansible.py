import copy
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
from seamless_migrate.domain.models import ConversionHostConfig, Mappings, utcnow
from seamless_migrate.executors.ansible import (
    CONVERSION_KEY_FILE,
    KIT_PLAYBOOK,
    STOP_TASK,
    AnsibleExecutor,
    ansible_yaml,
    apply_mappings,
    build_inventory,
    build_vars,
    classify_failure,
    effective_mappings,
    workload_filter,
)
from seamless_migrate.executors.ansible import _inventory as build_inventory_doc
from seamless_migrate.executors.base import PermanentStepError, StepName, TransientStepError
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


class FakeRegistry:
    def __init__(self, existing=()):
        self.calls = []
        self.existing = set(existing)

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


def value_scalars(node):
    """The scalar nodes in value position (mapping values, sequence items) of a YAML node."""
    if isinstance(node, yaml.MappingNode):
        for _key, value in node.value:
            yield from value_scalars(value)
    elif isinstance(node, yaml.SequenceNode):
        for item in node.value:
            yield from value_scalars(item)
    elif isinstance(node, yaml.ScalarNode):
        yield node


@pytest.mark.parametrize(
    ("strategy", "source"), [(Strategy.vmware_warm, VCENTER), (Strategy.warm, SRC)]
)
async def test_generated_ansible_input_is_never_templated(env, strategy, source):
    """Security.md C1-05, SDD §7.2: Ansible templates every string it reads, so a VM name (chosen
    by whoever runs the source VM), a mapping value or a password holding Jinja would run on the
    control plane (``lookup('pipe', …)``). Every string in vars.yml, secrets.yml and the
    inventory is written ``!unsafe``, and the playbook still receives it unchanged."""
    ctx, _ = ctx_for(env, strategy=strategy, source=source, name=JINJA_NAME)
    await env.executor.run(StepName.PRECOPY, ctx)
    calls = read_log(env.log)
    assert calls
    for call in calls:
        assert {"vars.yml", "secrets.yml", "inventory.yml"} <= set(call["raw"])
        for name, text in call["raw"].items():
            for node in value_scalars(yaml.compose(text)):
                assert node.tag in NEVER_TEMPLATED, (call["playbook"], name, node.value)
    if strategy is Strategy.vmware_warm:
        assert calls[-1]["vars"]["vms_list"] == [JINJA_NAME]
    else:
        [query] = calls[-1]["vars"]["os_migrate_workloads_filter"]
        assert re.search(query["regex"], JINJA_NAME)


def test_ansible_reads_generated_input_verbatim(tmp_path):
    """The real ``ansible-playbook`` keeps a Jinja VM name and password literal instead of running
    them, and the run still works: modules execute through the ``!unsafe`` connection variables of
    the executor's inventory and receive the nested secrets unchanged."""
    playbook_bin = Path(sys.executable).with_name("ansible-playbook")
    if not playbook_bin.exists():
        pytest.skip("ansible-core is not installed (the collection extra)")
    marker = "INJECTED-BY-VM-NAME"
    name = "{{ lookup('pipe', 'echo " + marker + "') }}"
    (tmp_path / "vars.yml").write_text(
        ansible_yaml({"vms_list": [name], "network_map": {"a": name}})
    )
    (tmp_path / "secrets.yml").write_text(
        ansible_yaml({"os_migrate_src_auth": {"username": "svc", "password": name + "%{x}"}})
    )
    # the executor's own inventory: local connection and interpreter are !unsafe strings
    (tmp_path / "inventory.yml").write_text(build_inventory_doc())
    (tmp_path / "play.yml").write_text(
        "- hosts: migrator\n  gather_facts: false\n  tasks:\n"
        "    - ansible.builtin.debug:\n"
        '        msg: "name={{ vms_list[0] }} net={{ network_map.a }}"\n'
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
    assert marker not in run.stdout.replace(name, "")
    assert f"pw={name}%{{x}}" in run.stdout
    assert "ok=4" in run.stdout and "failed=0" in run.stdout


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

    # the warm cutover above created the server: a cold cutover of the same VM is a resume
    cold_ctx, _ = ctx_for(
        env, strategy=Strategy.vmware_cold, source=VCENTER, downtime_started_at=utcnow()
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
        vm=make_vm(source_id="srv-1", name="web-01"),
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
    # a failed destination lookup after a cold cutover is logged, not fatal
    cold, rec = ctx_for(env, strategy=Strategy.cold)
    result = await failing.run(StepName.CUTOVER, cold)
    assert result.destination_server_id is None
    assert any("could not look up the destination server" in line for line in rec.logs)


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
