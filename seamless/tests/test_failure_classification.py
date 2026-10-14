"""The workload includes of the cold and warm roles print only the VM id (review RV7-02): the
executor classifies a failure by its playbook output's tail (classify_failure), and an include
line carrying the whole workload - names, metadata, tags, a boot volume's image properties such as
Nova's os_shutdown_timeout - turned a stuck detach, meant to be permanent (SDD §6.6), transient.
Real ansible-playbook runs each role's own include task over the real collection code."""

from __future__ import annotations

import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

from seamless_migrate.config import find_repo_root
from seamless_migrate.executors.ansible import SERVER_TYPE, classify_failure
from seamless_migrate.executors.base import PermanentStepError

#: a module that runs the cold export's detach of a data volume (OpenstackVolumeExport.
#: _detach_data_volumes_from_source, the real detach_volume_bounded) against a cloud whose
#: detach never ends: Cinder keeps the source VM's attachment, so it fails "Timed out after 1 s"
PROBE = textwrap.dedent(
    """\
    #!/usr/bin/python
    from ansible.module_utils.basic import AnsibleModule
    from ansible_collections.os_migrate.os_migrate.plugins.module_utils.volume_common import (
        OpenstackVolumeExport,
    )


    class _Obj(dict):
        def __getattr__(self, key):
            try:
                return self[key]
            except KeyError:
                raise AttributeError(key)


    class Conn:
        def get_server_by_id(self, sid):
            return _Obj(id=sid, name=sid, image=_Obj(id="img-1"), access_ipv4="192.0.2.1")

        def get_volume_by_id(self, vid):
            return _Obj(id=vid, status="detaching",
                        attachments=[{"server_id": "srv-1", "device": "/dev/vdb"}])

        def detach_volume(self, server, volume, wait):
            pass


    class Shell:
        def __init__(self, *args):
            pass

        def test_ssh_connection(self):
            pass


    class SerServer:
        def migration_params(self):
            return {"boot_disk_copy": False}


    class SourceVolume(OpenstackVolumeExport):
        def __init__(self, conn, budget):
            self.source_instance_id = "srv-1"
            self.ser_server = SerServer()
            super().__init__(conn, "conv-1", "/nonexistent", "cloud-user", "uuid-1",
                             shell_factory=Shell, timeout=budget)
            self.volume_map = {"/dev/vdb": {"source_id": "vol-data"}}


    def main():
        module = AnsibleModule(argument_spec=dict(budget=dict(type="int", default=1)))
        host = SourceVolume(Conn(), module.params["budget"])
        host._detach_data_volumes_from_source()
        module.exit_json(changed=True)


    if __name__ == "__main__":
        main()
    """
)

#: roles/import_workloads: main.yml includes workload.yml for each workload (its loop item is the
#: whole server resource), whose block exposes the volumes and whose rescue ends in a fail
#: the included tasks: expose the volumes, and the rescue's fail - naming the workload by id here,
#: so that only the include line can carry its data
WORKLOAD = textwrap.dedent(
    """\
    - name: Import a workload
      block:
        - name: Expose source volumes
          detach_probe:
            budget: 1
          register: exports
      rescue:
        - name: Fail if there was a problem in the migration
          ansible.builtin.fail:
            msg: "Failed to import {{ item['_info']['id'] }}."
    """
)

#: each role's task that includes the per-workload tasks, by its file and name
INCLUDES = {
    "import_workloads": ("roles/import_workloads/tasks/main.yml", "Import workloads"),
    "import_workloads_warm": (
        "roles/import_workloads_warm/tasks/main.yml",
        "Run the warm action for each workload",
    ),
}


def _include_task(role: str) -> dict:
    """The role's own include task, its loop over the test's workloads and its target the test's
    workload.yml - everything else, loop_control included, as the role has it."""
    path, name = INCLUDES[role]
    task = next(
        t for t in yaml.safe_load((find_repo_root() / path).read_text()) if t.get("name") == name
    )
    task = {k: v for k, v in task.items() if k not in ("when", "tags")}
    task["ansible.builtin.include_tasks"] = "workload.yml"
    task["loop"] = "{{ workloads }}"
    return task


def _import_output(tmp_path: Path, role: str, workload: dict) -> str:
    playbook = shutil.which("ansible-playbook", path=str(Path(sys.executable).parent))
    if playbook is None:
        pytest.skip("needs ansible-core next to the interpreter")
    work = tmp_path / "work"
    (work / "library").mkdir(parents=True)
    (work / "library" / "detach_probe.py").write_text(PROBE)
    (work / "workload.yml").write_text(WORKLOAD)
    (work / "play.yml").write_text(
        yaml.safe_dump(
            [
                {
                    "name": "Import workloads",
                    "hosts": "localhost",
                    "connection": "local",
                    "gather_facts": False,
                    "vars": {"workloads": [workload]},
                    "tasks": [_include_task(role)],
                }
            ],
            sort_keys=False,
        )
    )
    namespace = tmp_path / "collections" / "ansible_collections" / "os_migrate"
    namespace.mkdir(parents=True)
    (namespace / "os_migrate").symlink_to(find_repo_root(), target_is_directory=True)
    home = tmp_path / "home"
    env = {
        "PATH": f"{Path(playbook).parent}:/usr/bin:/bin",
        "HOME": str(home),
        "ANSIBLE_COLLECTIONS_PATH": str(tmp_path / "collections"),
        "ANSIBLE_HOME": str(home / ".ansible"),
        "ANSIBLE_LOCAL_TEMP": str(home / ".ansible" / "tmp"),
        "ANSIBLE_REMOTE_TEMP": str(home / ".ansible" / "tmp"),
        "ANSIBLE_RETRY_FILES_ENABLED": "0",
        "ANSIBLE_NOCOLOR": "1",
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    run = subprocess.run(
        [playbook, "play.yml"],
        cwd=work,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,  # as AnsibleExecutor._run_playbook reads it
        text=True,
        timeout=180,
        check=False,
    )
    assert run.returncode != 0, run.stdout
    assert "Timed out after 1 s waiting to detach volume vol-data from server srv-1" in run.stdout
    return run.stdout


def _workload(name: str, image_properties: dict) -> dict:
    return {
        "type": SERVER_TYPE,
        "_info": {"id": "srv-1"},
        "_migration_params": {"boot_disk_copy": False, "data_copy": True},
        "params": {
            "name": name,
            "metadata": {"owner": "finance"},
            # os-migrate's ServerVolume params: the boot volume carries the Glance properties of
            # the image it was made from (volume_image_metadata)
            "volumes": [
                {
                    "_info": {"id": "vol-root"},
                    "params": {"name": "root", "volume_image_metadata": image_properties},
                },
                {"_info": {"id": "vol-data"}, "params": {"name": "data"}},
            ],
        },
    }


@pytest.mark.parametrize("role", sorted(INCLUDES))
def test_a_stuck_detach_fails_permanently_whatever_the_workload_data_holds(tmp_path, role):
    """A VM whose name and boot image properties say "timeout" - Nova's standard
    os_shutdown_timeout - still fails permanently on a stuck detach: the include line names the
    workload by its id only."""
    workload = _workload("web-timeout-01", {"os_distro": "rhel", "os_shutdown_timeout": "60"})

    output = _import_output(tmp_path, role, workload)

    included = [line for line in output.splitlines() if line.startswith("included:")]
    assert included and all(line.endswith("(item=srv-1)") for line in included), included
    assert classify_failure(output) is PermanentStepError
