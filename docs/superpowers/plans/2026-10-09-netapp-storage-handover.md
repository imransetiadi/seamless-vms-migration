# NetApp ONTAP storage handover — implementation plan (2026-10-09)

Spec: SDD §3, §4.2 (`Disk.pool`), §7.3 step 0 and rollback, §7.3.1, §9.2, §10 (`storage_backends`).
Goal: RHOSP 17.1 sources on NetApp ONTAP (NFS, iSCSI, FC) can hand their volumes over to a RHOSO 18.0
backend on the same SVM without copying data; cold and warm keep working unchanged.

| # | Track | Task | Tests that must fail first, then pass |
|---|---|---|---|
| 1 | B | `seamless_migrate/storage.py`: `storage_family(caps)`, `split_host`, `manage_reference`, `resolve_destination` (pure) | `tests/test_storage_backends.py`: `test_storage_family_classifies_ceph_and_netapp_protocols`, `test_manage_reference_per_family`, `test_resolve_destination_netapp_nfs_matches_the_export_across_addresses`, `test_resolve_destination_netapp_block_keeps_the_flexvol`, `test_resolve_destination_rbd_uses_the_mapped_or_only_pool`, `test_resolve_destination_refuses_mismatch_missing_pool_and_other` |
| 2 | B | `Disk.pool`; `OpenStackProvider.check()` reports `storage_backends`; volume disks carry their pool | `tests/test_providers.py`: `test_openstack_check_reports_storage_backends_with_family`, `test_openstack_volume_disks_carry_their_cinder_pool` |
| 3 | B | Selector: handover ineligible for an unsupported family or an unresolvable destination pool | `tests/test_selector.py`: `test_handover_ineligible_on_unsupported_backend_family`, `test_handover_ineligible_when_netapp_pool_has_no_destination`, `test_handover_eligible_on_netapp_nfs_with_matching_export` |
| 4 | B | `HandoverExecutor`: step 0 resolution before the stop, references per family, `storage` in the definition, rollback with the source family's reference, legacy definitions stay RBD | `tests/test_executor_handover.py`: `test_handover_netapp_nfs_manages_by_share_path`, `test_handover_netapp_block_manages_by_lun_path`, `test_handover_refuses_before_stop_when_a_pool_cannot_be_resolved`, `test_handover_rollback_netapp_manages_back_with_the_source_share`, `test_handover_resume_of_a_definition_without_storage_stays_rbd` |
| 5 | B | Demo: NetApp pools in the fake clouds' capabilities, pools on demo disks | `tests/test_demo.py`: `test_demo_clouds_report_netapp_and_ceph_storage_backends` |
| 6 | C | Dashboard: types, storage backends on provider cards, pool family on VM disks, handover settings in plan creation, mock parity | `Providers.test.tsx`: storage backends listed by family; `Plans.test.tsx` ("turns on storage handover with a RHOSO backend per volume type (Ceph and NetApp)"): handover backend map sent; `mockData.test.ts` ("accepts a NetApp NFS volume whose export the destination mounts", "rules out encrypted disks, which Cinder cannot unmanage"): handover eligibility mirrors §9.2 |
| 7 | D | Docs: QASuite lab case for ONTAP handover, Performance backend row, README | — |

Lab verification (QASuite) is required before production use, as for the Ceph handover.
