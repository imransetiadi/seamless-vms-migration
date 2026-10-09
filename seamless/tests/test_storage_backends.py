"""Cinder driver families and storage-handover references (SDD §7.3.1)."""

import pytest

from seamless_migrate.storage import (
    StorageError,
    manage_reference,
    resolve_destination,
    split_host,
    storage_family,
)

NFS_CAPS = {"vendor_name": "NetApp", "storage_protocol": "nfs"}
ISCSI_CAPS = {"vendor_name": "NetApp", "storage_protocol": "iSCSI"}


def backend(pool, family):
    return {"pool": pool, "vendor": None, "protocol": None, "family": family}


def test_storage_family_classifies_ceph_and_netapp_protocols():
    assert storage_family({"vendor_name": "Open Source", "storage_protocol": "ceph"}) == "rbd"
    assert storage_family(NFS_CAPS) == "netapp_nfs"
    assert (
        storage_family({"vendor_name": "NetApp, Inc.", "storage_protocol": "NFS"}) == "netapp_nfs"
    )
    assert storage_family(ISCSI_CAPS) == "netapp_block"
    assert storage_family({"vendor_name": "NetApp", "storage_protocol": "FC"}) == "netapp_block"
    # NVMe namespaces and other vendors have no verified manage path
    assert storage_family({"vendor_name": "NetApp", "storage_protocol": "NVMe"}) == "other"
    assert storage_family({"vendor_name": "Open Source", "storage_protocol": "iSCSI"}) == "other"
    assert storage_family({}) == "other"


def test_manage_reference_per_family():
    assert split_host("hostgroup@ontap-nfs#10.0.0.5:/cinder_vol") == (
        "hostgroup@ontap-nfs",
        "10.0.0.5:/cinder_vol",
    )
    assert split_host("hostgroup@ceph") == ("hostgroup@ceph", None)
    assert manage_reference("rbd", "ssd", "volume-v1") == {"source-name": "volume-v1"}
    assert manage_reference("netapp_nfs", "10.0.0.5:/cinder_vol", "volume-v1") == {
        "source-name": "10.0.0.5:/cinder_vol/volume-v1"
    }
    assert manage_reference("netapp_block", "cinder_flexvol", "volume-v1") == {
        "source-name": "/vol/cinder_flexvol/volume-v1"
    }
    with pytest.raises(StorageError):
        manage_reference("other", "pool", "volume-v1")


def test_resolve_destination_netapp_nfs_matches_the_export_across_addresses():
    dst = [
        backend("hostgroup@ontap-nfs#10.20.0.5:/cinder_gold", "netapp_nfs"),
        backend("hostgroup@ontap-nfs#10.20.0.5:/cinder_vol", "netapp_nfs"),
        backend("hostgroup@ceph#rbd", "rbd"),
    ]
    # the source mounts the export through another LIF: the export path decides
    assert (
        resolve_destination("netapp_nfs", "192.0.2.5:/cinder_vol", "hostgroup@ontap-nfs", dst)
        == "hostgroup@ontap-nfs#10.20.0.5:/cinder_vol"
    )
    # an explicit pool must be that export
    with pytest.raises(StorageError, match="cinder_vol"):
        resolve_destination(
            "netapp_nfs", "192.0.2.5:/cinder_vol", "hostgroup@ontap-nfs#10.20.0.5:/cinder_gold", dst
        )


def test_resolve_destination_netapp_block_keeps_the_flexvol():
    dst = [
        backend("hostgroup@ontap-iscsi#flex_a", "netapp_block"),
        backend("hostgroup@ontap-iscsi#flex_b", "netapp_block"),
    ]
    assert (
        resolve_destination("netapp_block", "flex_b", "hostgroup@ontap-iscsi", dst)
        == "hostgroup@ontap-iscsi#flex_b"
    )
    assert (
        resolve_destination("netapp_block", "flex_b", "hostgroup@ontap-iscsi#flex_b", dst)
        == "hostgroup@ontap-iscsi#flex_b"
    )


def test_resolve_destination_rbd_uses_the_mapped_or_only_pool():
    dst = [backend("hostgroup@ceph-ssd#ssd", "rbd")]
    assert (
        resolve_destination("rbd", "ssd", "hostgroup@ceph-ssd#ssd", dst) == "hostgroup@ceph-ssd#ssd"
    )
    assert resolve_destination("rbd", "x", "hostgroup@ceph-ssd", dst) == "hostgroup@ceph-ssd#ssd"
    # an explicit RBD target is used as configured (SDD §7.3 before the amendment)
    assert resolve_destination("rbd", "x", "hostgroup@other#p", []) == "hostgroup@other#p"


def test_resolve_destination_refuses_mismatch_missing_pool_and_other():
    dst = [
        backend("hostgroup@ontap-nfs#10.20.0.5:/cinder_vol", "netapp_nfs"),
        backend("hostgroup@ontap-iscsi#flex_a", "netapp_block"),
        backend("hostgroup@ceph#a", "rbd"),
        backend("hostgroup@ceph#b", "rbd"),
    ]
    with pytest.raises(StorageError, match="no pool"):
        resolve_destination("netapp_block", "flex_z", "hostgroup@ontap-iscsi", dst)
    with pytest.raises(StorageError, match="no pool"):
        resolve_destination("netapp_nfs", "192.0.2.5:/other", "hostgroup@ontap-nfs", dst)
    with pytest.raises(StorageError, match="netapp_block"):
        resolve_destination("netapp_nfs", "192.0.2.5:/cinder_vol", "hostgroup@ontap-iscsi", dst)
    with pytest.raises(StorageError, match="unsupported"):
        resolve_destination("other", "p", "hostgroup@ceph#a", dst)
    with pytest.raises(StorageError, match="name the pool"):
        resolve_destination("rbd", "x", "hostgroup@ceph", dst)
    # without the destination's pool list a NetApp target cannot be checked
    with pytest.raises(StorageError, match="pools"):
        resolve_destination("netapp_block", "flex_a", "hostgroup@ontap-iscsi", [])
