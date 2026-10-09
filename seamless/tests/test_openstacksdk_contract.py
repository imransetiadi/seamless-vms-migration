"""The openstacksdk surface the provider and the handover executor rely on (SDD §7.3, §10).

The executor and provider tests use duck-typed doubles; this module checks the same names against
the installed openstacksdk and keystoneauth, so an SDK upgrade that renames an attribute, a query
option or a default fails here instead of in a cutover.
"""

import inspect

import pytest

openstack = pytest.importorskip("openstack")


def test_volume_attributes_used_for_pools_boot_properties_and_unmanage_checks():
    from openstack.block_storage.v3.volume import Volume

    for name in (
        "host",  # os-vol-host-attr:host -> Disk.pool, storage family (§7.3.1)
        "volume_image_metadata",  # boot properties (§7.3 step 7)
        "encryption_key_id",  # Cinder cannot unmanage encrypted volumes
        "is_encrypted",
        "group_id",
        "consistency_group_id",
        "is_bootable",
        "volume_type",
    ):
        assert hasattr(Volume, name), name


def test_port_and_server_attributes_used_to_restore_the_source_nics():
    """SDD §7.3 step 2 and rollback: a port's id, MAC, fixed IPs and binding; Nova's root device;
    a missing port answers NotFoundException (HTTP 404), which the rollback reads as deleted."""
    from openstack import exceptions
    from openstack.compute.v2.server import Server
    from openstack.network.v2.port import Port

    for name in ("id", "network_id", "mac_address", "fixed_ips", "device_id"):
        assert hasattr(Port, name), name
    assert hasattr(Server, "root_device_name")
    assert issubclass(exceptions.NotFoundException, exceptions.HttpException)
    # handover._not_found matches a 404 status code or this class name
    assert "NotFound" in exceptions.NotFoundException.__name__


def test_backend_pools_read_the_detailed_scheduler_stats():
    from openstack.block_storage.v3 import _proxy
    from openstack.block_storage.v3.stats import Pools

    assert Pools.base_path == "/scheduler-stats/get_pools?detail=True"
    assert hasattr(Pools, "name") and hasattr(Pools, "capabilities")
    assert "Pools" in inspect.getsource(_proxy.Proxy.backend_pools)


def test_snapshot_listing_filters_by_volume_across_projects():
    from openstack.block_storage.v3.snapshot import Snapshot

    keys = Snapshot._query_mapping._mapping
    assert "volume_id" in keys and "all_projects" in keys


def test_volume_attachments_report_delete_on_termination():
    from openstack.compute.v2.volume_attachment import VolumeAttachment

    assert hasattr(VolumeAttachment, "delete_on_termination")
    # the listing negotiates up to this microversion, which includes the field (2.79+)
    assert tuple(int(x) for x in VolumeAttachment._max_microversion.split(".")) >= (2, 79)


def test_raw_proxy_calls_return_error_answers_and_take_a_microversion():
    """Why the executor checks every raw answer, and how it asks for compute 2.85."""
    from keystoneauth1.session import Session
    from openstack.proxy import Proxy

    assert inspect.signature(Proxy.request).parameters["raise_exc"].default is False
    assert "microversion" in inspect.signature(Session.request).parameters


def test_proxy_methods_the_executor_and_provider_call():
    from openstack.block_storage.v3._proxy import Proxy as BlockStorage
    from openstack.compute.v2._proxy import Proxy as Compute
    from openstack.network.v2._proxy import Proxy as Network

    for proxy, names in (
        (
            Compute,
            (
                "get_server",
                "stop_server",
                "start_server",
                "wait_for_server",
                "volume_attachments",
                "delete_server",
                "wait_for_delete",
                "find_flavor",
                "create_server",
                "servers",
                "get_flavor",
            ),
        ),
        (
            BlockStorage,
            (
                "get_volume",
                "volumes",
                "snapshots",
                "backend_pools",
                "wait_for_status",
                "wait_for_delete",
            ),
        ),
        (
            Network,
            ("ports", "get_network", "find_network", "networks", "ips", "get_port", "create_port"),
        ),
    ):
        for name in names:
            assert callable(getattr(proxy, name, None)), f"{proxy.__module__}.{name}"
    assert "all_projects" in inspect.signature(BlockStorage.snapshots).parameters or (
        "query" in inspect.signature(BlockStorage.snapshots).parameters
    )
