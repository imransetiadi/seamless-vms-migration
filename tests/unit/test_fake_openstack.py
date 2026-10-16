"""The openstacksdk fakes are no kinder than openstacksdk and Cinder (review RV6-04): a wait that
always succeeds, or a detach that ends at once, let a test pass for the wrong reason - mutants
that dropped the source-side detach waits passed every test. These tests pin the model."""

from __future__ import absolute_import, division, print_function

__metaclass__ = type

import subprocess

import pytest

from ansible_collections.os_migrate.os_migrate.tests.unit import fake_openstack as fake


def _cloud():
    cloud = fake.FakeCloud("src")
    cloud.add_server("srv-1", "vm1")
    cloud.add_server("srv-2", "vm2")
    cloud.add_volume("vol-1", "data", 1, server_id="srv-1", device="/dev/vdb")
    return cloud


@pytest.mark.parametrize("status, ends", [
    ("available", "available"),  # already there: returned at once
    ("creating", "available"),  # Cinder finishes a transitional status on its own
    ("error", fake.ResourceFailure),  # block_storage's default failures
    ("in-use", fake.ResourceTimeout),  # a status Cinder does not leave on its own
])
def test_the_fakes_wait_for_status_ends_as_openstacksdks_does(status, ends):
    cloud = _cloud()
    volume = cloud.volumes["vol-1"]
    volume.status = status

    if isinstance(ends, str):
        assert cloud.block_storage.wait_for_status(volume, "available", wait=5).status == ends
    else:
        with pytest.raises(ends):
            cloud.block_storage.wait_for_status(volume, "available", wait=5)


def test_the_fakes_wait_for_status_fails_for_a_resource_that_went_away_or_without_a_bound():
    cloud = _cloud()
    volume = cloud.volumes.pop("vol-1")
    volume.status = "creating"

    # openstacksdk 4.21's wait re-reads it with fetch, which raises NotFoundException for a
    # resource gone (its own "went away" ResourceFailure is never reached)
    with pytest.raises(fake.NotFoundException):
        cloud.block_storage.wait_for_status(volume, "available", wait=5)
    with pytest.raises(AssertionError, match="waits forever"):
        cloud.block_storage.wait_for_status(volume, "available")


def test_the_fakes_detach_ends_only_once_cinder_read_it_through():
    """Nova accepts a detach and Cinder ends it a little later: until then the volume is
    `detaching`, its attachment still listed, so an attach elsewhere is refused; each read brings
    the end closer, and a wait lets the time pass."""
    cloud = _cloud()
    cloud.detach_reads = 2
    volume = cloud.volumes["vol-1"]

    cloud.detach_volume(cloud.servers["srv-1"], volume, wait=False)

    assert volume.status == "detaching" and [a["server_id"] for a in volume.attachments] == ["srv-1"]
    with pytest.raises(fake.SDKException, match="not available"):
        cloud.attach_volume(cloud.servers["srv-2"], volume)
    assert cloud.get_volume_by_id("vol-1").status == "detaching"
    assert cloud.get_volume_by_id("vol-1").status == "available" and volume.attachments == []

    cloud.attach_volume(cloud.servers["srv-2"], volume)
    cloud.detach_volume(cloud.servers["srv-2"], volume, wait=False)
    assert cloud.block_storage.wait_for_status(volume, "available", wait=5).attachments == []


def test_the_fakes_detach_of_one_holder_leaves_a_shared_volume_in_use():
    cloud = _cloud()
    cloud.detach_reads = 1
    volume = cloud.volumes["vol-1"]
    # a multi-attach volume both servers hold
    volume.attachments.append({"server_id": "srv-2", "device": "/dev/vdb", "volume_id": "vol-1"})

    cloud.detach_volume(cloud.servers["srv-1"], volume, wait=False)
    cloud.get_volume_by_id("vol-1")

    assert [a["server_id"] for a in volume.attachments] == ["srv-2"]
    assert volume.status == "in-use"
    with pytest.raises(fake.ResourceTimeout):  # it never becomes available
        cloud.block_storage.wait_for_status(volume, "available", wait=5)


def test_the_fakes_detach_of_a_volume_the_server_does_not_hold_is_nova_s_404():
    """openstacksdk 4.x's detach_volume deletes the server's volume attachment with
    ignore_missing=False: Nova answers 404 for one the server does not have."""
    cloud = _cloud()

    with pytest.raises(fake.NotFoundException, match="not attached to server srv-2"):
        cloud.detach_volume(cloud.servers["srv-2"], cloud.volumes["vol-1"], wait=False)
    assert [a["server_id"] for a in cloud.volumes["vol-1"].attachments] == ["srv-1"]


def test_the_fakes_second_detach_while_one_is_pending_is_refused():
    """Nova refuses to detach an attachment already detaching: the volume is no longer `in-use`."""
    cloud = _cloud()
    volume = cloud.volumes["vol-1"]
    cloud.detach_volume(cloud.servers["srv-1"], volume, wait=False)

    with pytest.raises(fake.SDKException, match="detaching from server srv-1 already"):
        cloud.detach_volume(cloud.servers["srv-1"], volume, wait=False)
    assert cloud.block_storage.wait_for_status(volume, "available", wait=5).attachments == []


def test_the_conversion_host_shows_each_held_volumes_virtio_serial_and_its_by_id_link():
    """As a virtio-blk conversion host: a volume's disk shows the serial Nova gives it - the volume
    id cut to 20 characters - and udev's /dev/disk/by-id/virtio-<serial> link exists while the
    volume is attached; the host's own disk shows no serial, and a gone disk has no serial file
    (review RV12-03)."""
    cloud = fake.FakeCloud("src")
    conv = cloud.add_conversion_host("conv", "10.0.0.10")
    volume = cloud.add_volume("0123456789abcdefghij-tail", "long", 10)
    shell = fake.ShellFactory([cloud])("10.0.0.10", "cloud-user")
    cloud.attach_volume(conv, volume)
    name = volume.attachments[0]["device"].rsplit("/", 1)[-1]
    link = ["test", "-b", "/dev/disk/by-id/virtio-0123456789abcdefghij"]

    assert shell.cmd_out(["cat", "/sys/block/%s/serial" % name]) == "0123456789abcdefghij"
    assert shell.cmd_val(link) == 0
    assert shell.cmd_out(["cat", "/sys/block/vda/serial"]) == ""  # the host's own disk

    cloud.detach_volume(conv, volume, wait=False)
    cloud.block_storage.wait_for_status(volume, "available", wait=5)

    assert shell.cmd_val(link) == 1
    with pytest.raises(subprocess.CalledProcessError):
        shell.cmd_out(["cat", "/sys/block/%s/serial" % name])
