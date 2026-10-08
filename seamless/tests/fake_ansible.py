"""A fake ``ansible-playbook`` for executor tests.

The script records every invocation (argv, cwd, the vars it received, which secret keys were
present and the secrets file mode) as a JSON line, and simulates the artefacts the os-migrate
playbooks produce: ``workloads.yml`` (export), the per-workload progress ``state_file``, the warm
state file, and the ``clouds.yaml`` that ``prelude_common`` writes into the data dir.

Behaviour is driven by ``ANSIBLE_FAKE_*`` environment variables (passed through by the executor).
"""

from __future__ import annotations

import json
import stat
import sys
import textwrap
from pathlib import Path

KIT_PLAYBOOK = "os_migrate.vmware_migration_kit.migration"

SCRIPT = textwrap.dedent(
    """\
    #!{python}
    import json, os, stat, sys, time
    from pathlib import Path
    import yaml

    argv = sys.argv[1:]
    record = {{"argv": argv, "cwd": os.getcwd(), "env_keys": sorted(os.environ)}}
    inventory = argv[argv.index("-i") + 1]
    playbook = argv[argv.index("-i") + 2]
    files = [a[1:] for a in argv if a.startswith("@")]
    record["playbook"] = os.path.basename(playbook) if playbook.endswith(".yml") else playbook
    record["inventory"] = Path(inventory).read_text()
    merged = {{}}
    for f in files:
        data = yaml.safe_load(Path(f).read_text()) or {{}}
        if os.path.basename(f) == "secrets.yml":
            record["secret_keys"] = sorted(data)
            record["secrets_mode"] = oct(stat.S_IMODE(os.stat(f).st_mode))
            record["dst_cloud_keys"] = sorted((data.get("dst_cloud") or {{}}).keys())
        else:
            record["vars"] = data
        merged.update(data)
    log = Path(os.environ["ANSIBLE_FAKE_LOG"])
    with log.open("a") as fh:
        fh.write(json.dumps(record) + "\\n")

    osm = Path(merged.get("os_migrate_data_dir") or merged.get("os_migrate_vmw_data_dir") or ".")
    osm.mkdir(parents=True, exist_ok=True)
    (osm / "clouds.yaml").write_text("clouds: {{src: {{auth: {{password: leak}}}}}}\\n")
    name = os.environ.get("ANSIBLE_FAKE_VM_NAME", "web-01")
    server_id = os.environ.get("ANSIBLE_FAKE_SERVER_ID", "srv-1")
    base = record["playbook"]
    print("PLAY [migrator] ****")
    print("TASK [" + base + "] ****")
    long_line = int(os.environ.get("ANSIBLE_FAKE_LONG_LINE", "0"))
    if long_line:
        print("ok: [localhost] => " + "x" * long_line)
    sys.stdout.flush()

    if base == "export_workloads.yml":
        doc = {{"os_migrate_version": "1.0.5", "resources": [{{
            "type": "openstack.compute.Server",
            "params": {{
                "name": name,
                "flavor_ref": {{"name": "m1.small", "project_name": "finance",
                               "domain_name": "Default"}},
                "ports": [{{"params": {{
                    "network_ref": {{"name": "app-net", "project_name": "finance",
                                    "domain_name": "Default"}},
                    "fixed_ips_refs": [{{"ip_address": "10.0.0.5", "subnet_ref": {{
                        "name": "app-subnet", "project_name": "finance",
                        "domain_name": "Default"}}}}]}}}}],
                "volumes": [{{"params": {{"name": "data", "volume_type": "ceph-hdd"}}}}],
                "security_group_refs": [{{"name": "default", "project_name": "finance",
                                          "domain_name": "Default"}}],
            }},
            "_info": {{"id": server_id}},
            "_migration_params": {{"boot_volume_params": {{"volume_type": "ceph-ssd"}}}},
        }}]}}
        (osm / "workloads.yml").write_text(yaml.safe_dump(doc))

    stop_task = {{
        "import_workloads.yml": "os_migrate.os_migrate.import_workloads : Perform workload stop "
                                "if os_migrate_workload_stop_before_migration is true",
        "import_workloads_cutover.yml": "os_migrate.os_migrate.import_workloads_warm : "
                                        "Stop the source server",
    }}.get(base)
    if base == "{kit}" and merged.get("cutover"):
        stop_task = "os_migrate.vmware_migration_kit.migration : Power off the VM"
    if stop_task:
        print("TASK [" + stop_task + "] ****")
        skipped = os.environ.get("ANSIBLE_FAKE_STOP_SKIPPED")
        print("skipping: [localhost]" if skipped else "ok: [localhost]")
        sys.stdout.flush()
        Path(os.environ.get("ANSIBLE_FAKE_STOP_MARK", "/dev/null")).write_text(str(time.time()))

    if base in ("import_workloads.yml", "import_workloads_precopy.yml",
                "import_workloads_cutover.yml"):
        logs = osm / "workload_logs"
        logs.mkdir(exist_ok=True)
        (logs / (name + ".state")).write_text(json.dumps({{"/dev/vda": 50.0, "/dev/vdb": 100.0}}))
        time.sleep(float(os.environ.get("ANSIBLE_FAKE_SLEEP", "0.3")))

    if base in ("import_workloads_precopy.yml", "import_workloads_cutover.yml"):
        warm = osm / "workload_warm"
        warm.mkdir(exist_ok=True)
        path = warm / (server_id + ".json")
        state = json.loads(path.read_text()) if path.exists() else {{
            "server_id": server_id, "server_name": name, "transfer_uuid": "t-1",
            "dest_volumes": {{}}, "passes": [], "pending_snapshot": None,
            "destination_server_id": None}}
        n = len(state["passes"]) + 1
        kind = "final" if base == "import_workloads_cutover.yml" else (
            "full" if n == 1 else "delta")
        skipped_pass = bool(os.environ.get("ANSIBLE_FAKE_SKIP_PASS"))  # the role skipped it
        if not skipped_pass:
            state["passes"].append({{"number": n, "kind": kind,
                "started_at": "2026-10-08T10:00:00Z", "ended_at": "2026-10-08T10:05:00Z",
                "bytes_scanned": 1000, "bytes_changed": 1000 // n,
                "bytes_transferred": 900 // n, "duration_s": 300.0 / n}})
        if kind == "final" and not skipped_pass:
            state["destination_server_id"] = "dst-" + server_id
        path.write_text(json.dumps(state))

    failure = os.environ.get("ANSIBLE_FAKE_FAIL", "")
    if failure and (os.environ.get("ANSIBLE_FAKE_FAIL_ON", base) == base):
        print("fatal: [localhost]: FAILED! => " + failure)
        sys.exit(2)
    # a successful import/cutover created the destination server (a skipped stop means the
    # role skipped the workload and created nothing)
    created = os.environ.get("ANSIBLE_FAKE_CREATED")
    if created and stop_task and not os.environ.get("ANSIBLE_FAKE_STOP_SKIPPED"):
        with open(created, "a") as fh:
            fh.write(name + "\\n")
    print("PLAY RECAP ****")
    """
)


def install(directory: Path) -> Path:
    """Write the fake script into ``directory`` and return its path."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "ansible-playbook"
    path.write_text(SCRIPT.format(python=sys.executable, kit=KIT_PLAYBOOK))
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
    return path


def read_log(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
