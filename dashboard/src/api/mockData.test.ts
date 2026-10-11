import { describe, expect, it } from 'vitest';
import { buildFixtures, buildProviders, eligibility, makeVm, preflight } from './mockData';

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

describe('mock eligibility and preflight mirror SDD §9.2/§9.3 for ports without exactly one fixed IP', () => {
  const providers = buildProviders(now);
  const source = providers.find((p) => p.id === 'rhosp17-dc1')!;
  const vcenter = providers.find((p) => p.id === 'vcenter-hq')!;
  const destination = providers.find((p) => p.id === 'rhoso-prod')!;
  const plan = { ...buildFixtures(now).plans[0]!, handover: { enabled: true, backend_map: { 'tripleo-ceph': 'hostgroup@ceph-ssd#volumes-ssd' } } };
  const disks = [{ size_gb: 20 }];
  const nets = { 'app-net': 1450, 'l2-net': 1450, 'VM Network': 1450 };

  it('rules out cold and warm for a port with no fixed IP or several, and storage handover for several', () => {
    const dual = makeVm({ id: 'os-p1', name: 'dual-01', disks, nics: [{ network: 'app-net', fixed_ips: ['10.0.0.5', 'fd00::5'] }] });
    const reasons = eligibility(dual, 'openstack', plan, source, destination, []);
    for (const strategy of ['cold', 'warm', 'storage_handover'] as const) {
      expect(reasons[strategy]?.some((r) => r.includes('app-net (10.0.0.5, fd00::5)')), strategy).toBe(true);
    }
    const noIp = makeVm({ id: 'os-p2', name: 'l2-01', disks, nics: [{ network: 'l2-net', fixed_ips: [] }] });
    const l2 = eligibility(noIp, 'openstack', plan, source, destination, []);
    expect(l2.cold?.some((r) => r.includes('l2-net (no fixed IP)'))).toBe(true);
    expect(l2.warm?.some((r) => r.includes('l2-net (no fixed IP)'))).toBe(true);
    expect(l2.storage_handover?.some((r) => r.includes('l2-net'))).toBe(false);
    const one = makeVm({ id: 'os-p3', name: 'one-01', disks, nics: [{ network: 'app-net', fixed_ips: ['10.0.0.6'] }] });
    expect(Object.values(eligibility(one, 'openstack', plan, source, destination, [])).flat()).toEqual([]);
    // VMware NICs go through the migration kit
    const vmw = makeVm({ id: 'vm-p4', name: 'vmw-01', cbt_enabled: true, disks: [{ size_gb: 20, kind: 'vmdk', volume_type: null }], nics: [{ network: 'VM Network', fixed_ips: ['10.0.0.7', 'fd00::7'] }] });
    expect(Object.values(eligibility(vmw, 'vmware', plan, vcenter, destination, [])).flat()).toEqual([]);
  });

  it('reports NET_PORT_IPS naming each such NIC with its addresses, never for VMware', () => {
    const vm = makeVm({
      id: 'os-p5',
      name: 'mixed-01',
      disks,
      nics: [
        { network: 'app-net', fixed_ips: ['10.0.0.5', 'fd00::5'] },
        { network: 'l2-net', fixed_ips: [] },
        { network: 'app-net', fixed_ips: ['10.0.0.6'] },
      ],
    });
    const found = preflight(vm, 'openstack', plan, nets, [vm.name]).filter((f) => f.code === 'NET_PORT_IPS');
    expect(found).toHaveLength(1);
    expect(found[0]!.severity).toBe('warning');
    expect(found[0]!.message).toContain('app-net (10.0.0.5, fd00::5), l2-net (no fixed IP)');
    expect(found[0]!.message).not.toContain('10.0.0.6');
    expect(preflight(vm, 'vmware', plan, nets, [vm.name]).some((f) => f.code === 'NET_PORT_IPS')).toBe(false);
  });
});

describe('mock eligibility and preflight follow port_creation_mode (SDD §9.2/§9.3)', () => {
  const providers = buildProviders(now);
  const source = providers.find((p) => p.id === 'rhosp17-dc1')!;
  const destination = providers.find((p) => p.id === 'rhoso-prod')!;
  const base = { ...buildFixtures(now).plans[0]!, handover: { enabled: true, backend_map: { 'tripleo-ceph': 'hostgroup@ceph-ssd#volumes-ssd' } } };
  const neutron = { ...base, port_creation_mode: 'neutron' as const };
  const disks = [{ size_gb: 20 }];

  it('keeps cold and warm for any number of fixed IPs through Neutron; the handover keeps one address', () => {
    expect(base.port_creation_mode).toBe('nova');
    const dual = makeVm({ id: 'os-q1', name: 'dual-02', disks, nics: [{ network: 'app-net', fixed_ips: ['10.0.0.5', 'fd00::5'] }] });
    const reasons = eligibility(dual, 'openstack', neutron, source, destination, []);
    expect(reasons.cold).toEqual([]);
    expect(reasons.warm).toEqual([]);
    expect(reasons.storage_handover?.some((r) => r.includes('app-net (10.0.0.5, fd00::5)'))).toBe(true);
    expect(preflight(dual, 'openstack', neutron, { 'app-net': 1450 }, [dual.name]).some((f) => f.code === 'NET_PORT_IPS')).toBe(false);
    const nova = preflight(dual, 'openstack', base, { 'app-net': 1450 }, [dual.name]).find((f) => f.code === 'NET_PORT_IPS');
    expect(nova?.remediation).toMatch(/keep mac addresses and every fixed ip/i);
  });
});
