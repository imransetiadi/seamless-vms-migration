"""The workload includes of the cold and warm roles print only the VM id (review RV7-02), and a
failed cold export's volume map no volume name (review RV8-05): the executor classifies a failure
by its playbook output's tail (classify_failure), and an include line carrying the whole workload -
names, metadata, tags, a boot volume's image properties such as Nova's os_shutdown_timeout - or a
failure result carrying the tenant's volume names turned a stuck detach, meant to be permanent
(SDD §6.6), transient. Real ansible-playbook runs over the real collection code."""

from __future__ import annotations

import ast
import builtins
import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

from seamless_migrate.config import find_repo_root
from seamless_migrate.executors.ansible import SERVER_TYPE, TRANSIENT, classify_failure
from seamless_migrate.executors.base import PermanentStepError, TransientStepError

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


def _playbook() -> str:
    playbook = shutil.which("ansible-playbook", path=str(Path(sys.executable).parent))
    if playbook is None:
        pytest.skip("needs ansible-core next to the interpreter")
    return playbook


def _collections(tmp_path: Path) -> Path:
    """A collections path whose os_migrate.os_migrate is this repository."""
    root = tmp_path / "collections"
    if not root.exists():
        namespace = root / "ansible_collections" / "os_migrate"
        namespace.mkdir(parents=True)
        (namespace / "os_migrate").symlink_to(find_repo_root(), target_is_directory=True)
    return root


def _run_playbook(tmp_path: Path, playbook: str, work: Path) -> str:
    """Run work/play.yml; its output as AnsibleExecutor._run_playbook reads it."""
    home = tmp_path / "home"
    env = {
        "PATH": f"{Path(playbook).parent}:/usr/bin:/bin",
        "HOME": str(home),
        "ANSIBLE_COLLECTIONS_PATH": str(_collections(tmp_path)),
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
    return run.stdout


def _import_output(tmp_path: Path, role: str, workload: dict) -> str:
    playbook = _playbook()
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
    output = _run_playbook(tmp_path, playbook, work)
    assert "Timed out after 1 s waiting to detach volume vol-data from server srv-1" in output
    return output


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
    """The include line names the workload by its id only: a VM whose name and boot image
    properties say "timeout" - Nova's standard os_shutdown_timeout - adds nothing transient to it,
    and the stuck detach fails permanently through this stand-in rescue. The roles' own rescue
    messages and progress lines still name the VM - owner question q8 (review RV36A-01)."""
    workload = _workload("web-timeout-01", {"os_distro": "rhel", "os_shutdown_timeout": "60"})

    output = _import_output(tmp_path, role, workload)

    included = [line for line in output.splitlines() if line.startswith("included:")]
    assert included and all(line.endswith("(item=srv-1)") for line in included), included
    assert classify_failure(output) is PermanentStepError


#: the real import_workload_export_volumes on the collection's fakes, its data volume named by
#: argv[1]: a cold export whose detach of that volume never ends (Cinder keeps the source VM's
#: attachment) fails; prints the module's failure result
EXPORT_FAILURE = textwrap.dedent(
    """\
    import json
    import sys
    from unittest import mock

    from ansible_collections.os_migrate.os_migrate.plugins.module_utils import volume_common
    from ansible_collections.os_migrate.os_migrate.plugins.modules import (
        import_workload_export_volumes as export,
    )
    from ansible_collections.os_migrate.os_migrate.tests.unit import fake_openstack as fake

    name = sys.argv[1]
    src = fake.FakeCloud("src")
    src.add_conversion_host("src-conv", "10.0.0.10")
    src.add_server("srv-1", "vm1")
    src.servers["srv-1"].status = "SHUTOFF"
    src.add_volume("vol-root", "vm1-root", 10, bootable=True, server_id="srv-1", device="/dev/vda")
    src.add_volume("vol-data", name, 10, server_id="srv-1", device="/dev/vdb")
    src.detach_volume = lambda server, volume, wait=True, timeout=None: None
    data = fake.workload_data(volumes=[
        fake.server_volume_data("vol-root", "vm1-root", 10, "/dev/vda", "srv-1", bootable=True),
        fake.server_volume_data("vol-data", name, 10, "/dev/vdb", "srv-1"),
    ])
    args = {"conversion_host": {"id": "src-conv"}, "ssh_key_path": "/keys/k",
            "ssh_user": "cloud-user", "timeout": 2, "data": data}
    with mock.patch.object(volume_common.time, "sleep"):
        result = fake.run_module(export, args, src, fake.ShellFactory([src]))
    for key in ("_exit_code", "_connected", "failed", "invocation"):
        result.pop(key, None)
    print(json.dumps(result))
    """
)

#: a module failing with the result a real module run failed with
FAIL_AS = textwrap.dedent(
    """\
    #!/usr/bin/python
    import json

    from ansible.module_utils.basic import AnsibleModule


    def main():
        module = AnsibleModule(argument_spec=dict(result_file=dict(type="path", required=True)))
        with open(module.params["result_file"]) as handle:
            module.fail_json(**json.load(handle))


    if __name__ == "__main__":
        main()
    """
)


def _export_failure(tmp_path: Path, volume_name: str) -> Path:
    """The real export module's failure result for the stuck detach, in a file."""
    run = subprocess.run(
        [sys.executable, "-c", EXPORT_FAILURE, volume_name],
        env={
            **os.environ,
            "PYTHONPATH": str(_collections(tmp_path)),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert run.returncode == 0, run.stderr
    result = json.loads(run.stdout)
    assert (
        result["msg"] == "Timed out after 2 s waiting to detach volume vol-data from server srv-1"
    )
    path = tmp_path / "result.json"
    path.write_text(json.dumps(result))
    return path


@pytest.mark.parametrize("volume_name", ["vm1-data", "db-timeout-archive"])
def test_a_failed_exports_volume_names_never_make_a_stuck_detach_transient(tmp_path, volume_name):
    """The cold export fails with its volume map, for the role's rescue, and Ansible prints that
    result on the task's line: the same stuck detach was permanent for a data volume named
    "vm1-data" and transient for one named "db-timeout-archive". The map leaves the names out."""
    result_file = _export_failure(tmp_path, volume_name)
    work = tmp_path / "work"
    (work / "library").mkdir(parents=True)
    (work / "library" / "fail_as.py").write_text(FAIL_AS)
    task = {
        "name": "Import a workload",
        "block": [
            {
                "name": "Expose source volumes",
                "fail_as": {"result_file": str(result_file)},
                "register": "exports",
            }
        ],
        "rescue": [
            {
                "name": "Fail if there was a problem in the migration",
                "ansible.builtin.fail": {"msg": "Failed to import srv-1."},
            }
        ],
    }
    play = {
        "name": "Import workloads",
        "hosts": "localhost",
        "connection": "local",
        "gather_facts": False,
        "tasks": [task],
    }
    (work / "play.yml").write_text(yaml.safe_dump([play], sort_keys=False))

    output = _run_playbook(tmp_path, _playbook(), work)

    assert "Timed out after 2 s waiting to detach volume vol-data from server srv-1" in output
    assert volume_name not in output, output
    assert classify_failure(output) is PermanentStepError


#: the real export module on the collection's fakes (test_cold_export_failure's source host): its
#: NBD server's start on the conversion host goes through the real RemoteShell.cmd_out, whose ssh
#: exits with the status given - 255 ssh's own failure, any other the remote command's; prints the
#: module's failure result
NBD_START_FAILURE = textwrap.dedent(
    """\
    import json
    import subprocess
    import sys
    from unittest import mock

    from ansible_collections.os_migrate.os_migrate.plugins.module_utils import volume_common
    from ansible_collections.os_migrate.os_migrate.plugins.modules import (
        import_workload_export_volumes as export,
    )
    from ansible_collections.os_migrate.os_migrate.tests.unit import fake_openstack as fake
    from ansible_collections.os_migrate.os_migrate.tests.unit import (
        test_cold_export_failure as cold,
    )

    status = int(sys.argv[1])
    RealShell = volume_common.RemoteShell  # run_module patches the name with the factory


    class NbdServerFails(cold.SourceHost):
        def __call__(self, address, ssh_user, key_path=None):
            shell = super().__call__(address, ssh_user, key_path)
            out = shell.cmd_out
            real = RealShell(address, ssh_user, key_path)

            def ssh_exits(args, **kwargs):
                raise subprocess.CalledProcessError(status, args)

            def cmd_out(command, **kwargs):
                if "nbdkit" in command and "--readonly" in command:
                    with mock.patch.object(volume_common.subprocess, "check_output", ssh_exits):
                        return real.cmd_out(command, **kwargs)
                return out(command, **kwargs)

            shell.cmd_out = cmd_out
            return shell


    src, data = cold._boot_from_volume_vm()
    with mock.patch.object(volume_common.time, "sleep"):
        result = fake.run_module(export, dict(cold.COMMON, data=data), src, NbdServerFails(src))
    for key in ("_exit_code", "_connected", "failed", "invocation"):
        result.pop(key, None)
    print(json.dumps(result))
    """
)


@pytest.mark.parametrize(
    ("status", "expected"),
    [(1, PermanentStepError), (255, TransientStepError)],
    ids=["the remote command fails by itself", "ssh fails"],
)
def test_a_remote_command_failing_by_itself_stays_permanent_and_ssh_failing_transient(
    tmp_path, status, expected
):
    """SDD §6.6 (reviews RV15-01, RV42B-02): the cold export's NBD server fails to start on the
    conversion host with the source VM stopped. Its error quoted ssh's whole command line,
    `-o ConnectTimeout=10` among its options, which TRANSIENT reads as a "timeout": a failure no
    retry fixes was retried, another export with the source stopped. A remote command failing by
    itself names its program and status and stays permanent; ssh's own failure (255) keeps its
    command line, retried as transient as §6.6 relies on."""
    run = subprocess.run(
        [sys.executable, "-c", NBD_START_FAILURE, str(status)],
        env={
            **os.environ,
            "PYTHONPATH": str(_collections(tmp_path)),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert run.returncode == 0, run.stderr
    result_file = tmp_path / "result.json"
    result_file.write_text(run.stdout)
    work = tmp_path / "work"
    (work / "library").mkdir(parents=True)
    (work / "library" / "fail_as.py").write_text(FAIL_AS)
    task = {
        "name": "Import a workload",
        "block": [
            {
                "name": "Expose source volumes",
                "fail_as": {"result_file": str(result_file)},
                "register": "exports",
            }
        ],
        "rescue": [
            {
                "name": "Fail if there was a problem in the migration",
                "ansible.builtin.fail": {"msg": "Failed to import srv-1."},
            }
        ],
    }
    play = {
        "name": "Import workloads",
        "hosts": "localhost",
        "connection": "local",
        "gather_facts": False,
        "tasks": [task],
    }
    (work / "play.yml").write_text(yaml.safe_dump([play], sort_keys=False))

    output = _run_playbook(tmp_path, _playbook(), work)

    assert ("ConnectTimeout" in output) is (status == 255), output[-2000:]
    assert classify_failure(output) is expected


def _collection_error_classes() -> dict[str, str]:
    """Every exception class the collection's plugins define, by name, with its file: a class
    deriving from a built-in exception, from another such class, or from a library one named
    ...Error or ...Exception."""
    bases: dict[str, tuple[list[str], str]] = {}
    for path in sorted((find_repo_root() / "plugins").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ClassDef):
                names = [ast.unparse(b).rsplit(".", 1)[-1] for b in node.bases]
                bases[node.name] = (names, str(path.relative_to(find_repo_root())))

    def is_error(name: str, seen: frozenset[str] = frozenset()) -> bool:
        if isinstance(getattr(builtins, name, None), type) and issubclass(
            getattr(builtins, name), BaseException
        ):
            return True
        if name in bases and name not in seen:
            return any(is_error(base, seen | {name}) for base in bases[name][0])
        return name.endswith(("Error", "Exception"))

    return {name: path for name, (_, path) in bases.items() if is_error(name)}


def test_no_collection_error_reads_transient_by_its_class_name():
    """SDD §6.6 (owner question q8, review RV35A-01): ansible-core before 2.19 - the image's and
    CI's pin - prints an escaped exception's qualified class name in the task output the executor
    classifies, so an error class of the collection whose name matches TRANSIENT turns every
    failure it raises transient - a stuck detach retried with the cold source stopped. None may."""
    errors = _collection_error_classes()
    assert "ConversionHostUnreachable" in errors and "DiskIdentityError" in errors
    assert {name: path for name, path in errors.items() if TRANSIENT.search(name)} == {}


def _what_ansible_core_prints_of_the_collection() -> dict[str, str]:
    """What ansible-core before 2.19 can print of the collection's code in the output the executor
    classifies, with the file defining it: a raised class qualified by its module's dotted path
    (ansible_collections.os_migrate.os_migrate.plugins.<dir>.<module>.<Class>), and an escaped
    error's traceback - each frame's file path and function or method name."""
    root = find_repo_root()
    found: dict[str, str] = {}
    for path in sorted((root / "plugins").rglob("*.py")):
        relative = path.relative_to(root)
        module = ".".join(relative.with_suffix("").parts)
        found[module] = str(relative)
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ClassDef):
                found[f"{module}.{node.name}"] = str(relative)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # a frame is its file and its function: kept apart per file (review RV37B-02)
                found[f"{relative}:{node.name}"] = str(relative)
    return found


def test_nothing_ansible_core_prints_of_the_collection_reads_transient():
    """SDD §6.6 (owner question q8, reviews RV35A-01, RV36A-02, RV41A-09): beside a raised class's
    own name, ansible-core before 2.19 prints its module path, and an escaped error's traceback
    names each frame's file and function - a module, class, function or method of the collection
    whose name matches TRANSIENT so turns a permanent failure transient, a stuck detach retried
    with the cold source stopped. None may: any expression can raise out of its own frame (an
    index, a call), so no predicate is "raising nothing" for sure - the one that read so was
    renamed."""
    found = _what_ansible_core_prints_of_the_collection()
    assert "plugins.module_utils.volume_common.DetachStuck" in found
    assert "plugins/module_utils/volume_common.py:wait_for_lock" in found
    assert {name: path for name, path in found.items() if TRANSIENT.search(name)} == {}


@pytest.mark.parametrize(
    "path, mutate",
    [
        (
            "plugins/module_utils/blocksync.py",
            lambda text: (
                text
                + '\n\ndef is_timeout(err):\n    raise RuntimeError(f"Volume {err} is stuck")\n'
            ),
        ),
        (
            "plugins/module_utils/volume_common.py",
            lambda text: text.replace("def gave_up_waiting(err):", "def is_timeout(err):", 1),
        ),
        (
            "plugins/module_utils/volume_common.py",
            lambda text: text + '\n\ndef index_timeout(err):\n    return err.args[0] == "late"\n',
        ),
    ],
    ids=[
        "a raising is_timeout in another module",
        "the predicate named after a timeout again",
        "a predicate that raises by an index",
    ],
)
def test_the_transient_guard_flags_every_name_that_reads_transient(
    tmp_path, monkeypatch, path, mutate
):
    """Reviews RV36A-02, RV37B-02, RV41A-09: a function of the collection whose name matches
    TRANSIENT fails the guard - raising or not: an expression such as `err.args[0]` raises out of
    its frame without a raise statement, and the 2.18 traceback prints that frame's name."""
    root = tmp_path / "repo"
    shutil.copytree(find_repo_root() / "plugins", root / "plugins")
    target = root / path
    target.write_text(mutate(target.read_text(encoding="utf-8")), encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "find_repo_root", lambda: root)
    with pytest.raises(AssertionError):
        test_nothing_ansible_core_prints_of_the_collection_reads_transient()
