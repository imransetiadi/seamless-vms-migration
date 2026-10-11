import { describe, expect, it } from 'vitest';
import { buildFixtures, buildInventories, buildProviders, eligibility, makeVm, preflight } from './mockData';
import type { DestinationInventory, Plan, VMRef } from './types';

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

  it('keeps every strategy for any number of fixed IPs through Neutron (SDD §7.3 step 8)', () => {
    expect(base.port_creation_mode).toBe('nova');
    const dual = makeVm({ id: 'os-q1', name: 'dual-02', disks, nics: [{ network: 'app-net', fixed_ips: ['10.0.0.5', 'fd00::5'] }] });
    const reasons = eligibility(dual, 'openstack', neutron, source, destination, []);
    expect(reasons.cold).toEqual([]);
    expect(reasons.warm).toEqual([]);
    expect(reasons.storage_handover).toEqual([]);
    expect(eligibility(dual, 'openstack', base, source, destination, []).storage_handover?.some((r) => r.includes('app-net (10.0.0.5, fd00::5)'))).toBe(true);
    expect(preflight(dual, 'openstack', neutron, { 'app-net': 1450 }, [dual.name]).some((f) => f.code === 'NET_PORT_IPS')).toBe(false);
    const nova = preflight(dual, 'openstack', base, { 'app-net': 1450 }, [dual.name]).find((f) => f.code === 'NET_PORT_IPS');
    expect(nova?.remediation).toMatch(/keep mac addresses and every fixed ip/i);
  });
});

describe('mock preflight checks NIC overrides like the API (SDD §9.3)', () => {
  const providers = buildProviders(now);
  const source = providers.find((p) => p.id === 'rhosp17-dc1')!;
  const vcenter = providers.find((p) => p.id === 'vcenter-hq')!;
  const destination = providers.find((p) => p.id === 'rhoso-prod')!;
  const nets = { 'tenant-app': 1442, 'tenant-web': 1442 };
  const subnets = [
    { name: 'app-v4', network: 'tenant-app', cidr: '10.20.0.0/16' },
    { name: 'web-v4', network: 'tenant-web', cidr: '10.30.0.0/16' },
  ];
  const vm = makeVm({ id: 'os-o1', name: 'nic-01', disks: [{ size_gb: 20 }], nics: [{ network: 'tenant-app', mac: 'fa:16:3e:00:00:01', fixed_ips: ['10.20.1.5'] }] });
  const other = makeVm({ id: 'os-o2', name: 'nic-02', disks: [{ size_gb: 20 }], nics: [{ network: 'tenant-app', mac: 'fa:16:3e:00:00:02', fixed_ips: ['10.20.1.6'] }] });
  const plan = (entry: { network?: string | null; fixed_ips?: string[] | null }) => ({
    ...buildFixtures(now).plans[0]!,
    mappings: { networks: {}, flavors: {}, volume_types: {}, projects: {} },
    nic_overrides: { 'os-o1': { 'fa:16:3e:00:00:01': { network: null, fixed_ips: null, ...entry } } },
  });
  const codes = (found: { code: string }[]) => found.map((f) => f.code);

  it('names where an applicable override sends the NIC', () => {
    const found = preflight(vm, 'openstack', plan({ network: 'tenant-web', fixed_ips: ['10.30.1.5'] }), nets, [vm.name], { subnets, selected: [vm, other] });
    expect(found.find((f) => f.code === 'MAP_NIC_OVERRIDE')?.message).toBe('NIC override(s): fa:16:3e:00:00:01 (tenant-app) -> tenant-web: 10.30.1.5.');
    expect(codes(found)).not.toContain('MAP_NIC_OVERRIDE_INVALID');
  });

  it('blocks an address outside the target subnets, one another NIC gets, and any override on VMware', () => {
    const outside = preflight(vm, 'openstack', plan({ network: 'tenant-web' }), nets, [vm.name], { subnets, selected: [vm] });
    expect(outside.find((f) => f.code === 'MAP_NIC_OVERRIDE_INVALID')?.message).toBe(
      "NIC override(s) of nic-01 cannot be applied: fa:16:3e:00:00:01: 10.20.1.5 lies in no subnet of 'tenant-web'.",
    );
    const twice = preflight(vm, 'openstack', plan({ fixed_ips: ['10.20.1.6'] }), nets, [vm.name], { subnets, selected: [vm, other] });
    expect(twice.find((f) => f.code === 'MAP_NIC_OVERRIDE_INVALID')?.message).toContain('fa:16:3e:00:00:01: 10.20.1.6 also goes to nic-02 (fa:16:3e:00:00:02)');
    const vmw = preflight(vm, 'vmware', plan({ network: 'tenant-web' }), nets, [vm.name], { subnets, selected: [vm] });
    expect(vmw.find((f) => f.code === 'MAP_NIC_OVERRIDE_INVALID')?.message).toContain("the migration kit keeps the guest's network settings");
    expect(eligibility(vm, 'vmware', plan({ network: 'tenant-web' }), vcenter, destination, vmw).vmware_cold?.some((r) => r.includes('MAP_NIC_OVERRIDE_INVALID'))).toBe(true);
  });

  it('blocks an address Neutron gives no VM, as the API words it', () => {
    const found = preflight(vm, 'openstack', plan({ network: 'tenant-web', fixed_ips: ['10.30.0.0', '10.30.255.255', '10.30.1.5'] }), nets, [vm.name], {
      subnets,
      selected: [vm],
    });
    expect(found.find((f) => f.code === 'MAP_NIC_OVERRIDE_INVALID')?.message).toBe(
      'NIC override(s) of nic-01 cannot be applied: fa:16:3e:00:00:01: 10.30.0.0 is the network address of 10.30.0.0/16, not a host address; ' +
        'fa:16:3e:00:00:01: 10.30.255.255 is the broadcast address of 10.30.0.0/16, not a host address.',
    );
  });

  it('finds an address another NIC gets whatever way it is written', () => {
    const v6 = makeVm({ id: 'os-o2', name: 'nic-02', disks: [{ size_gb: 20 }], nics: [{ network: 'tenant-app', mac: 'fa:16:3e:00:00:02', fixed_ips: ['FD00:0::0005'] }] });
    const found = preflight(vm, 'openstack', plan({ fixed_ips: ['fd00::5'] }), nets, [vm.name], { subnets: [...subnets, { name: 'app-v6', network: 'tenant-app', cidr: 'fd00::/64' }], selected: [vm, v6] });
    expect(found.find((f) => f.code === 'MAP_NIC_OVERRIDE_INVALID')?.message).toContain('fa:16:3e:00:00:01: fd00::5 also goes to nic-02 (fa:16:3e:00:00:02)');
  });

  it('counts the addresses a NIC gets for the port rules', () => {
    const dual = makeVm({ id: 'os-o1', name: 'nic-01', disks: [{ size_gb: 20 }], nics: [{ network: 'tenant-app', mac: 'fa:16:3e:00:00:01', fixed_ips: ['10.20.1.5', 'fd00::5'] }] });
    expect(eligibility(dual, 'openstack', plan({ fixed_ips: ['10.20.1.5'] }), source, destination, []).cold).toEqual([]);
  });
});

describe('mock preflight resolves each NIC\'s RHOSO network like the API (SDD §9.3, §7.3 step 0)', () => {
  const net = (id: string, name: string, own: boolean, project_id: string) => ({ id, name, mtu: 1442, project_id, own, shared: !own });
  const vm = makeVm({ id: 'os-o1', name: 'nic-01', disks: [{ size_gb: 20 }], nics: [{ network: 'tenant-app', mac: 'fa:16:3e:00:00:01', fixed_ips: ['10.20.1.5'] }] });
  const base = { ...buildFixtures(now).plans[0]!, mappings: { networks: { 'tenant-app': 'tenant-web' }, flavors: {}, volume_types: {}, projects: {} }, nic_overrides: {} };
  const codes = (found: { code: string }[]) => found.map((f) => f.code);

  it('blocks a name the cutover cannot resolve, with the API\'s message', () => {
    const twoOwn = [net('a', 'tenant-web', true, 'shop'), net('b', 'tenant-web', true, 'shop')];
    const found = preflight(vm, 'openstack', base, { 'tenant-web': 1442 }, [vm.name], { selected: [vm], networkDetails: twoOwn });
    expect(found.find((f) => f.code === 'MAP_NETWORK_AMBIGUOUS')?.message).toBe(
      "The cutover cannot tell these RHOSO networks apart: fa:16:3e:00:00:01 (tenant-app): 2 networks of the credential's project are named 'tenant-web'.",
    );
    const twoOther = [net('a', 'tenant-web', false, 'p1'), net('b', 'tenant-web', false, 'p2')];
    expect(preflight(vm, 'openstack', base, { 'tenant-web': 1442 }, [vm.name], { selected: [vm], networkDetails: twoOther }).find((f) => f.code === 'MAP_NETWORK_AMBIGUOUS')?.message).toBe(
      "The cutover cannot tell these RHOSO networks apart: fa:16:3e:00:00:01 (tenant-app): 2 networks of other projects are named 'tenant-web' (p1, p2), none of the credential's project.",
    );
    // the credential's own among others, or another project's alone, resolves; VMware never asks
    for (const details of [[net('x', 'tenant-web', false, 'p1'), net('a', 'tenant-web', true, 'shop')], [net('x', 'tenant-web', false, 'p1')]])
      expect(codes(preflight(vm, 'openstack', base, { 'tenant-web': 1442 }, [vm.name], { selected: [vm], networkDetails: details }))).not.toContain('MAP_NETWORK_AMBIGUOUS');
    expect(codes(preflight(vm, 'vmware', base, { 'tenant-web': 1442 }, [vm.name], { selected: [vm], networkDetails: twoOther }))).not.toContain('MAP_NETWORK_AMBIGUOUS');
  });

  it('blocks an own address Neutron would refuse on the resolved network, with the API\'s message', () => {
    const plan = { ...base, mappings: { ...base.mappings, networks: {} } };
    const details = [net('app', 'tenant-app', true, 'shop')];
    const subnets = [{ name: 's', network: 'tenant-app', network_id: 'app', cidr: '10.30.0.0/16', ipv6_address_mode: null, ipv6_ra_mode: null }];
    const found = preflight(vm, 'openstack', plan, { 'tenant-app': 1442 }, [vm.name], { selected: [vm], networkDetails: details, subnets });
    const refused = found.find((f) => f.code === 'NET_ADDRESS_REFUSED');
    expect(refused?.severity).toBe('blocker');
    expect(refused?.message).toBe(
      "Neutron would refuse these addresses on their RHOSO network: fa:16:3e:00:00:01 on 'tenant-app': 10.20.1.5 is no host address of any subnet of the port's network.",
    );
    expect(refused?.remediation).toBe(
      'Give the NIC an address its RHOSO network takes: map its network to one whose subnets hold it, or set the address in a NIC override; for a SLAAC address keep the MAC (port_creation_mode "neutron").',
    );
    const fits = [{ ...subnets[0]!, cidr: '10.20.0.0/16' }];
    expect(codes(preflight(vm, 'openstack', plan, { 'tenant-app': 1442 }, [vm.name], { selected: [vm], networkDetails: details, subnets: fits }))).not.toContain('NET_ADDRESS_REFUSED');
  });

  it('takes the MTU and an override\'s subnets from the resolved network, not same-named ones', () => {
    const own = { ...net('own', 'tenant-web', true, 'shop'), mtu: 1500 };
    const other = { ...net('other', 'tenant-web', false, 'p9'), mtu: 1400 };
    const tall = makeVm({ id: 'os-o1', name: 'nic-01', disks: [{ size_gb: 20 }], nics: [{ network: 'tenant-app', mac: 'fa:16:3e:00:00:01', fixed_ips: ['10.20.1.5'], mtu: 1500 }] });
    const subnets = [
      { name: 'own-v4', network: 'tenant-web', network_id: 'own', cidr: '10.20.0.0/16', ipv6_address_mode: null, ipv6_ra_mode: null },
      { name: 'other-v4', network: 'tenant-web', network_id: 'other', cidr: '10.40.0.0/16', ipv6_address_mode: null, ipv6_ra_mode: null },
    ];
    const context = { selected: [tall], networkDetails: [own, other], subnets };
    expect(codes(preflight(tall, 'openstack', base, { 'tenant-web': 1400 }, [tall.name], context))).not.toContain('NET_MTU_SHRINK');
    const elsewhere = { ...base, nic_overrides: { 'os-o1': { 'fa:16:3e:00:00:01': { network: 'tenant-web', fixed_ips: ['10.40.1.9'] } } } };
    expect(preflight(tall, 'openstack', elsewhere, { 'tenant-web': 1400 }, [tall.name], context).find((f) => f.code === 'MAP_NIC_OVERRIDE_INVALID')?.message).toContain(
      "fa:16:3e:00:00:01: 10.40.1.9 lies in no subnet of 'tenant-web'",
    );
  });

  it('checks no address when the inventory lists no subnet', () => {
    const plan = { ...base, mappings: { ...base.mappings, networks: {} } };
    const details = [net('app', 'tenant-app', true, 'shop')];
    for (const subnets of [[], undefined])
      expect(codes(preflight(vm, 'openstack', plan, { 'tenant-app': 1442 }, [vm.name], { selected: [vm], networkDetails: details, subnets }))).not.toContain(
        'NET_ADDRESS_REFUSED',
      );
  });

  it('applies the SLAAC rule to an override\'s addresses, with the API\'s message', () => {
    const details = [net('app', 'tenant-app', true, 'shop')];
    const subnets = [
      { name: 'v4', network: 'tenant-app', network_id: 'app', cidr: '10.20.0.0/16', ipv6_address_mode: null, ipv6_ra_mode: null },
      { name: 'v6', network: 'tenant-app', network_id: 'app', cidr: 'fd00:20::/64', ipv6_address_mode: 'slaac', ipv6_ra_mode: 'slaac' },
    ];
    const entry = (address: string, mode: 'nova' | 'neutron') => ({
      ...base,
      mappings: { ...base.mappings, networks: {} },
      port_creation_mode: mode,
      nic_overrides: { 'os-o1': { 'fa:16:3e:00:00:01': { network: null, fixed_ips: ['10.20.1.6', address] } } },
    });
    const message = (plan: Plan) =>
      preflight(vm, 'openstack', plan, { 'tenant-app': 1442 }, [vm.name], { selected: [vm], networkDetails: details, subnets }).find((f) => f.code === 'MAP_NIC_OVERRIDE_INVALID')?.message;
    expect(message(entry('fd00:20::f816:3eff:fe00:1', 'neutron'))).toBeUndefined();
    expect(message(entry('fd00:20::5', 'neutron'))).toBe(
      'NIC override(s) of nic-01 cannot be applied: fa:16:3e:00:00:01: fd00:20::5 is not fd00:20::f816:3eff:fe00:1, the address the SLAAC or DHCPv6-stateless subnet fd00:20::/64 gives MAC fa:16:3e:00:00:01.',
    );
    expect(message(entry('fd00:20::f816:3eff:fe00:1', 'nova'))).toBe(
      'NIC override(s) of nic-01 cannot be applied: fa:16:3e:00:00:01: fd00:20::f816:3eff:fe00:1 lies on fd00:20::/64, a SLAAC or DHCPv6-stateless subnet: through Nova the port gets a new MAC, and Neutron takes no explicit address there.',
    );
  });

  it('blocks none of the mock\'s own OpenStack VMs', () => {
    const inventories = buildInventories();
    const dst = inventories['rhoso-prod'] as DestinationInventory;
    const vms = inventories['rhosp17-dc1'] as VMRef[];
    for (const mode of ['nova', 'neutron'] as const) {
      const plan = { ...base, mappings: { networks: {}, flavors: {}, volume_types: {}, projects: {} }, port_creation_mode: mode };
      for (const v of vms) {
        const found = codes(preflight(v, 'openstack', plan, dst.networks, [v.name], { selected: vms, subnets: dst.subnets, networkDetails: dst.network_details }));
        expect(found, v.name).not.toContain('MAP_NETWORK_AMBIGUOUS');
        expect(found, `${v.name} ${mode}`).not.toContain('NET_ADDRESS_REFUSED');
      }
    }
  });
});
