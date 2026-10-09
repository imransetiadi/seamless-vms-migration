import { describe, expect, it } from 'vitest';
import { buildFixtures, buildProviders, eligibility, makeVm } from './mockData';

const now = Date.parse('2026-10-09T12:00:00Z');

describe('mock eligibility mirrors SDD §9.2 for storage handover', () => {
  const providers = buildProviders(now);
  const source = providers.find((p) => p.id === 'rhosp17-dc1')!;
  const destination = providers.find((p) => p.id === 'rhoso-prod')!;
  const plan = {
    ...buildFixtures(now).plans[0]!,
    handover: { enabled: true, backend_map: { 'tripleo-ceph': 'hostgroup@ceph-ssd#volumes-ssd', 'netapp-nfs': 'hostgroup@ontap-nfs' } },
  };

  it('rules out encrypted disks, which Cinder cannot unmanage', () => {
    const vm = makeVm({ id: 'os-x1', name: 'enc-01', disks: [{ size_gb: 20 }, { size_gb: 50, encrypted: true }] });
    const reasons = eligibility(vm, 'openstack', plan, source, destination, []).storage_handover ?? [];
    expect(reasons.some((r) => /encrypted/i.test(r))).toBe(true);
  });

  it('accepts a NetApp NFS volume whose export the destination mounts', () => {
    const vm = makeVm({ id: 'os-x2', name: 'nfs-01', disks: [{ size_gb: 20, volume_type: 'netapp-nfs' }] });
    expect(eligibility(vm, 'openstack', plan, source, destination, []).storage_handover).toEqual([]);
  });
});
