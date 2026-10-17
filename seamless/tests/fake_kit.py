"""A fake run of the migration kit 2.2.7's cutover for orchestrator tests (review RV28-01).

It prints the kit playbook's task headers and acts as the kit's ``migrate`` module does on the
volume it finds by the kit's name of the VM (plugins/modules/src/migrate/migrate.go): none - copy,
convert, power the VM off, mark the volume converted; one marked converted - "Volume already
converted, skipping migration..": nothing copied, nothing powered off. Then the role's "Create
network ports" and "Create instance", which boots from that volume. The VM's power state and the
volume live in the directory ``$ANSIBLE_FAKE_KIT_STATE`` (``power``: on/off; ``volume``:
converted).
"""

from __future__ import annotations

import stat
import sys
from pathlib import Path

from seamless_migrate.planning.kit import kit_vm_name
from tests.test_executor_ansible import FakeImpl, FakeRegistry

KIT_CUTOVER = r"""#!@PYTHON@
import os, sys
from pathlib import Path
import yaml

class Loader(yaml.SafeLoader):
    pass

Loader.add_constructor("!unsafe", lambda loader, node: loader.construct_scalar(node))
merged = {}
for arg in sys.argv[1:]:
    if arg.startswith("@"):
        merged.update(yaml.load(Path(arg[1:]).read_text(), Loader=Loader) or {})
state = Path(os.environ["ANSIBLE_FAKE_KIT_STATE"])
vm = merged["vms_list"][0]
host = "192.0.2.10"
print("PLAY [Migrate vmware virtual machines] ****")
print("TASK [os_migrate.vmware_migration_kit.import_workloads : Migrate Guest from Vmware using "
      "nbdkit " + vm + "] ****")
sys.stdout.flush()
volume = state / "volume"
if volume.exists() and volume.read_text() == "converted":
    pass  # "Volume already converted, skipping migration.."
else:
    volume.write_text("converted")  # copied and converted, then PowerOffVM
    (state / "power").write_text("off")
print("changed: [" + host + "]")
print("TASK [os_migrate.vmware_migration_kit.import_workloads : Create network ports] ****")
once = state / "fail_ports_once"
if once.exists():
    once.unlink()
    print('fatal: [' + host + ']: FAILED! => {"changed": false, "msg": "Failed to create port: '
          'Conflict 409"}')
    print("PLAY RECAP ****")
    sys.exit(2)
print("changed: [" + host + "]")
print("TASK [os_migrate.vmware_migration_kit.import_workloads : Create instance] ****")
with open(os.environ["ANSIBLE_FAKE_CREATED"], "a") as fh:
    fh.write(vm + "\n")
print("changed: [" + host + "]")
print("PLAY RECAP ****")
"""


def install_kit(directory: Path) -> Path:
    """Write the fake kit run as ``ansible-playbook`` into ``directory`` and return its path."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "ansible-playbook"
    path.write_text(KIT_CUTOVER.replace("@PYTHON@", sys.executable))
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
    return path


class KitRegistry(FakeRegistry):
    """The executor's providers over the fake kit's state: the source's power_on powers the VM on,
    and the destination holds the kit's volume of the VM while the kit's state names one."""

    def __init__(self, state: Path, vm_name: str) -> None:
        super().__init__()
        self.state, self.vm_name = state, vm_name

    def get(self, provider):
        impl = FakeImpl(self)
        state = self.state

        async def power_on(source_id):
            impl.calls.append(("power_on", source_id))
            (state / "power").write_text("on")

        async def volumes():
            volume = state / "volume"
            if not volume.exists():
                return []
            converted = {"converted": "true"} if volume.read_text() == "converted" else {}
            return [
                {
                    "id": "kit-vol-1",
                    "name": f"{kit_vm_name(self.vm_name)}-2000",
                    "status": "available",
                    "metadata": {"osm": "true", **converted},
                    "attachments": [],
                }
            ]

        async def delete_volume(volume_id):
            impl.calls.append(("delete_volume", volume_id))
            (state / "volume").unlink(missing_ok=True)

        impl.power_on, impl.volumes, impl.delete_volume = power_on, volumes, delete_volume
        return impl
