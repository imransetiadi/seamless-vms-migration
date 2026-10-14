"""A source conversion host that becomes unreachable mid-export (SDD 6.6, QASuite R-07, review
RV8-01): a lock wait rides out an outage of up to 15 minutes, a reboot or a blip, and the step
completes; an outage longer than that fails the export, which first attaches the data volumes it
detached back to the source VM through the API alone - the source cleanup cannot run while the
host is down, and the rollback that follows starts the source VM."""

from __future__ import absolute_import, division, print_function

__metaclass__ = type

import subprocess
import time
from unittest import mock

import pytest

from ansible_collections.os_migrate.os_migrate.plugins.module_utils import volume_common
from ansible_collections.os_migrate.os_migrate.plugins.modules import (
    import_workload_export_volumes as export_module,
)
from ansible_collections.os_migrate.os_migrate.plugins.modules import (
    import_workload_src_cleanup as cleanup_module,
)
from ansible_collections.os_migrate.os_migrate.tests.unit import fake_openstack as fake
from ansible_collections.os_migrate.os_migrate.tests.unit.test_cold_export_failure import (
    COMMON,
    DISKS,
    SourceHost,
    _boot_from_volume_vm,
    _holders,
)

DATA_VOLUMES = ("vol-data1", "vol-data2")


class Clock:
    """volume_common's time module: sleep moves the clock on at once."""

    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds

    def __getattr__(self, name):
        return getattr(time, name)


class RebootingHost(SourceHost):
    """The source conversion host goes down once ``armed``: from the next take of ``lock`` on,
    for ``outage`` seconds of ``clock``, every ssh to it fails as ssh does - exit status 255 - and
    each try costs the 10 s connect timeout (RemoteShell's ConnectTimeout=10)."""

    def __init__(self, cloud, clock, lock, outage):
        super().__init__(cloud)
        self.clock, self.lock, self.outage = clock, lock, outage
        self.armed = False
        self.down_since = None

    def down(self):
        return self.down_since is not None and self.clock.now < self.down_since + self.outage

    def __call__(self, address, ssh_user, key_path=None):
        shell = super().__call__(address, ssh_user, key_path)
        host = self
        val, out, ping = shell.cmd_val, shell.cmd_out, shell.test_ssh_connection

        def unreachable(command):
            host.clock.now += 10
            return subprocess.CalledProcessError(
                255, ["ssh", "%s@%s" % (ssh_user, address)] + list(command)
            )

        def cmd_val(command, **kwargs):
            if (host.armed and host.down_since is None and command[:2] == ["sudo", "flock"]
                    and command[6] == host.lock):
                host.down_since = host.clock.now
            if host.down():
                host.clock.now += 10
                return 255
            return val(command, **kwargs)

        def cmd_out(command, **kwargs):
            if host.down():
                raise unreachable(command)
            return out(command, **kwargs)

        def test_ssh_connection():
            if host.down():
                raise unreachable(["echo connected"])
            return ping()

        shell.cmd_val, shell.cmd_out, shell.test_ssh_connection = cmd_val, cmd_out, test_ssh_connection
        return shell


def _run(module, args, src, host):
    """One module run as Ansible reports it: its result, or MODULE FAILURE for an exception."""
    try:
        return fake.run_module(module, dict(args), src, host)
    except Exception as err:  # pylint: disable=broad-except
        return {"failed": True, "msg": "MODULE FAILURE: %s" % err}


def _cleanup_args(data, exported):
    return dict(COMMON, data=data, transfer_uuid=exported["transfer_uuid"],
                volume_map=exported["volume_map"])


def _outage(phase, outage):
    """The export, then - as the role runs them - the block's final cleanup, or the rescue's."""
    src, data = _boot_from_volume_vm()
    clock = Clock()
    lock = (volume_common.ATTACH_LOCK_FILE_SOURCE if phase == "export attach lock"
            else volume_common.PORT_LOCK_FILE)
    host = RebootingHost(src, clock, lock, outage)
    common = dict(COMMON, data=data)
    cleaned = rescued = None
    with mock.patch.object(volume_common, "time", clock):
        host.armed = phase == "export attach lock"
        exported = _run(export_module, common, src, host)
        if not exported.get("failed"):
            # the transfer and the destination server are made meanwhile; the block's last task
            host.armed = True
            cleaned = _run(cleanup_module, _cleanup_args(data, exported), src, host)
        if (exported.get("failed") or cleaned.get("failed")) and "volume_map" in exported:
            rescued = _run(cleanup_module, _cleanup_args(data, exported), src, host)
    return src, host, exported, cleaned, rescued


@pytest.mark.parametrize("phase", ["export attach lock", "final cleanup port lock"])
def test_a_two_minute_conversion_host_reboot_is_ridden_out(phase):
    """Review RV8-01: a first window of one minute gave up inside the reboot, and the rollback
    started the source VM without its data volumes. The step now completes, and the final
    cleanup puts the data volumes back on the source VM."""
    src, host, exported, cleaned, _rescued = _outage(phase, outage=120)

    assert not exported.get("failed"), exported
    assert not cleaned.get("failed"), cleaned
    assert host.down_since is not None, "the outage never started"
    for volume_id in DATA_VOLUMES:
        assert _holders(src, volume_id) == ["srv-1"], (volume_id, src.volumes[volume_id].attachments)


def test_an_outage_past_the_window_leaves_the_data_volumes_on_the_source_vm():
    """An outage of 20 minutes while the export waits for the attach lock, the data volumes
    detached: the wait gives up, and the export attaches them back to the source VM through the
    API before it fails - the rescue's cleanup cannot reach the host, and the rollback that follows
    starts the source VM, which must have every data volume."""
    src, _host, exported, _cleaned, rescued = _outage("export attach lock", outage=1200)

    assert exported.get("failed") and "Lock timeout" in exported["msg"], exported
    assert rescued is not None and rescued.get("failed"), rescued  # the host is still down
    for volume_id in DATA_VOLUMES:
        assert _holders(src, volume_id) == ["srv-1"], (volume_id, src.volumes[volume_id].attachments)
    # the source VM holds exactly its own disks: the root copy the export made is not one of them
    held = sorted(v.id for v in src.volumes.values() if "srv-1" in _holders(src, v.id))
    assert held == sorted(DISKS), held
