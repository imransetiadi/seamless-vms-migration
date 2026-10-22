import { describe, expect, it } from 'vitest';
import eligibilityRaw from '../../../seamless/tests/fixtures/eligibility_cases.json?raw';
import { buildFixtures, buildInventories, buildProviders, destinationDescriptor, eligibility, gpuFlavorsOf, iso, makeVm, preflight, quotaShare, readDestinationGpus, replaceNames } from './mockData';
import type { MemberRead, StackRead } from './mockData';
import { gpuRequests } from '../lib/gpu';
import { capabilityRoles } from '../lib/networks';
import type { DestinationFlavor, DestinationInventory, DestinationNetwork, DestinationSubnet, Finding, GpuCapacity, GpuCheckReport, Phase, Plan, Provider, ProviderKind, Severity, VMRef } from './types';

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
    const found = preflight(vm, 'openstack', plan, nets).filter((f) => f.code === 'NET_PORT_IPS');
    expect(found).toHaveLength(1);
    expect(found[0]!.severity).toBe('warning');
    expect(found[0]!.message).toContain('app-net (10.0.0.5, fd00::5), l2-net (no fixed IP)');
    expect(found[0]!.message).not.toContain('10.0.0.6');
    expect(preflight(vm, 'vmware', plan, nets).some((f) => f.code === 'NET_PORT_IPS')).toBe(false);
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
    expect(preflight(dual, 'openstack', neutron, { 'app-net': 1450 }).some((f) => f.code === 'NET_PORT_IPS')).toBe(false);
    const nova = preflight(dual, 'openstack', base, { 'app-net': 1450 }).find((f) => f.code === 'NET_PORT_IPS');
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
    const found = preflight(vm, 'openstack', plan({ network: 'tenant-web', fixed_ips: ['10.30.1.5'] }), nets, { subnets, selected: [vm, other] });
    expect(found.find((f) => f.code === 'MAP_NIC_OVERRIDE')?.message).toBe('NIC override(s): fa:16:3e:00:00:01 (tenant-app) -> tenant-web: 10.30.1.5.');
    expect(codes(found)).not.toContain('MAP_NIC_OVERRIDE_INVALID');
  });

  it('blocks an address outside the target subnets, one another NIC gets, and any override on VMware', () => {
    const outside = preflight(vm, 'openstack', plan({ network: 'tenant-web' }), nets, { subnets, selected: [vm] });
    expect(outside.find((f) => f.code === 'MAP_NIC_OVERRIDE_INVALID')?.message).toBe(
      "NIC override(s) of nic-01 cannot be applied: fa:16:3e:00:00:01: 10.20.1.5 lies in no subnet of 'tenant-web'.",
    );
    const twice = preflight(vm, 'openstack', plan({ fixed_ips: ['10.20.1.6'] }), nets, { subnets, selected: [vm, other] });
    expect(twice.find((f) => f.code === 'MAP_NIC_OVERRIDE_INVALID')?.message).toContain('fa:16:3e:00:00:01: 10.20.1.6 also goes to nic-02 (fa:16:3e:00:00:02)');
    const vmw = preflight(vm, 'vmware', plan({ network: 'tenant-web' }), nets, { subnets, selected: [vm] });
    expect(vmw.find((f) => f.code === 'MAP_NIC_OVERRIDE_INVALID')?.message).toContain("the migration kit keeps the guest's network settings");
    expect(eligibility(vm, 'vmware', plan({ network: 'tenant-web' }), vcenter, destination, vmw).vmware_cold?.some((r) => r.includes('MAP_NIC_OVERRIDE_INVALID'))).toBe(true);
  });

  it('blocks an address Neutron gives no VM, as the API words it', () => {
    // an IPv6 subnet's first address is a port's (review NC-08)
    const dual = [...subnets, { name: 'web-v6', network: 'tenant-web', cidr: 'fd00:30::/64' }];
    const found = preflight(vm, 'openstack', plan({ network: 'tenant-web', fixed_ips: ['10.30.0.0', '10.30.255.255', '10.30.1.5', 'fd00:30::'] }), nets, {
      subnets: dual,
      selected: [vm],
    });
    expect(found.find((f) => f.code === 'MAP_NIC_OVERRIDE_INVALID')?.message).toBe(
      'NIC override(s) of nic-01 cannot be applied: fa:16:3e:00:00:01: 10.30.0.0 is the network address of 10.30.0.0/16, not a host address; ' +
        'fa:16:3e:00:00:01: 10.30.255.255 is the broadcast address of 10.30.0.0/16, not a host address.',
    );
  });

  it.each(['constructor', 'toString', '__proto__'])('refuses an override to %s, a network the destination lacks, as the API (review RV23-01)', (name) => {
    const found = preflight(vm, 'openstack', plan({ network: name }), nets, { subnets, selected: [vm] });
    expect(found.find((f) => f.code === 'MAP_NIC_OVERRIDE_INVALID')?.message).toBe(
      `NIC override(s) of nic-01 cannot be applied: fa:16:3e:00:00:01: the destination has no network '${name}'.`,
    );
  });

  it("writes the target network's name as the API's messages do (review NC-11)", () => {
    const quoted = "bob's-web";
    const missing = preflight(vm, 'openstack', plan({ network: quoted }), nets, { subnets, selected: [vm] });
    expect(missing.find((f) => f.code === 'MAP_NIC_OVERRIDE_INVALID')?.message).toBe(
      'NIC override(s) of nic-01 cannot be applied: fa:16:3e:00:00:01: the destination has no network "bob\'s-web".',
    );
    const outside = preflight(vm, 'openstack', plan({ network: quoted }), { ...nets, [quoted]: 1442 }, {
      subnets: [...subnets, { name: 'q', network: quoted, cidr: '10.40.0.0/16' }],
      selected: [vm],
    });
    expect(outside.find((f) => f.code === 'MAP_NIC_OVERRIDE_INVALID')?.message).toBe(
      'NIC override(s) of nic-01 cannot be applied: fa:16:3e:00:00:01: 10.20.1.5 lies in no subnet of "bob\'s-web".',
    );
  });

  it('finds an address another NIC gets whatever way it is written', () => {
    const v6 = makeVm({ id: 'os-o2', name: 'nic-02', disks: [{ size_gb: 20 }], nics: [{ network: 'tenant-app', mac: 'fa:16:3e:00:00:02', fixed_ips: ['FD00:0::0005'] }] });
    const found = preflight(vm, 'openstack', plan({ fixed_ips: ['fd00::5'] }), nets, { subnets: [...subnets, { name: 'app-v6', network: 'tenant-app', cidr: 'fd00::/64' }], selected: [vm, v6] });
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
    const found = preflight(vm, 'openstack', base, { 'tenant-web': 1442 }, { selected: [vm], networkDetails: twoOwn });
    expect(found.find((f) => f.code === 'MAP_NETWORK_AMBIGUOUS')?.message).toBe(
      "The cutover cannot tell these networks of the destination apart: fa:16:3e:00:00:01 (tenant-app): 2 networks of the credential's project are named 'tenant-web'.",
    );
    const twoOther = [net('a', 'tenant-web', false, 'p1'), net('b', 'tenant-web', false, 'p2')];
    expect(preflight(vm, 'openstack', base, { 'tenant-web': 1442 }, { selected: [vm], networkDetails: twoOther }).find((f) => f.code === 'MAP_NETWORK_AMBIGUOUS')?.message).toBe(
      "The cutover cannot tell these networks of the destination apart: fa:16:3e:00:00:01 (tenant-app): 2 networks of other projects are named 'tenant-web' (p1, p2), none of the credential's project.",
    );
    // the credential's own among others, or another project's alone, resolves; a VMware NIC is judged too (SDD §7.2)
    for (const details of [[net('x', 'tenant-web', false, 'p1'), net('a', 'tenant-web', true, 'shop')], [net('x', 'tenant-web', false, 'p1')]])
      expect(codes(preflight(vm, 'openstack', base, { 'tenant-web': 1442 }, { selected: [vm], networkDetails: details }))).not.toContain('MAP_NETWORK_AMBIGUOUS');
    expect(codes(preflight(vm, 'vmware', base, { 'tenant-web': 1442 }, { selected: [vm], networkDetails: twoOther }))).toContain('MAP_NETWORK_AMBIGUOUS');
  });

  it.each(['openstack', 'vmware'] as const)('resolves a %s NIC on a network named like an Object.prototype key by that name (review RV23-01)', (kind) => {
    const nic = makeVm({ id: 'os-o3', name: 'nic-03', disks: [{ size_gb: 20 }], nics: [{ network: 'constructor', mac: 'fa:16:3e:00:00:03', fixed_ips: ['10.20.1.7'] }] });
    const plan = { ...base, mappings: { ...base.mappings, networks: {} } };
    const twoOwn = [net('a', 'constructor', true, 'shop'), net('b', 'constructor', true, 'shop')];
    const found = preflight(nic, kind, plan, { constructor: 1442 }, { selected: [nic], networkDetails: twoOwn });
    expect(found.find((f) => f.code === 'MAP_NETWORK_AMBIGUOUS')?.message).toBe(
      "The cutover cannot tell these networks of the destination apart: fa:16:3e:00:00:03 (constructor): 2 networks of the credential's project are named 'constructor'.",
    );
  });

  it('lists a missing network before a port its credential may not place, in the API\'s order (review RV25-03)', () => {
    // test_preflight.py's test_a_missing_network_comes_before_a_port_its_credential_may_not_place, the same VM and inventory
    const plan = { ...base, mappings: { ...base.mappings, networks: {} }, nic_overrides: {}, prestage_resources: [] } as Plan;
    const two = makeVm({
      id: 'os-r25c',
      name: 'web-01',
      disks: [{ size_gb: 20 }],
      nics: [
        { network: 'gone-net', mac: 'fa:16:3e:00:00:01', fixed_ips: ['10.0.0.5'], mtu: 1500 },
        { network: 'other-net', mac: 'fa:16:3e:00:00:02', fixed_ips: ['10.0.0.6'], mtu: 1500 },
      ],
    });
    const other = { ...net('n1', 'other-net', false, 'p-other'), mtu: 1500, shared: false, external: false };
    const found = preflight(two, 'openstack', plan, { 'other-net': 1500 }, { selected: [two], networkDetails: [other], destination: 'RHOSO 18.0' });
    expect(found.filter((f) => f.code.startsWith('MAP_NETWORK') || f.code.startsWith('NET_PORT')).map((f) => [f.code, f.message])).toEqual([
      ['MAP_NETWORK_MISSING', 'No mapping or same-named network in RHOSO 18.0 for: gone-net.'],
      [
        'NET_PORT_NOT_PERMITTED',
        "The credential of RHOSO 18.0 may not put these NICs on their network: fa:16:3e:00:00:02 on 'other-net' (project p-other): it is neither shared with the credential's project nor external.",
      ],
    ]);
  });

  it('blocks an own address Neutron would refuse on the resolved network, with the API\'s message', () => {
    const plan = { ...base, mappings: { ...base.mappings, networks: {} } };
    const details = [net('app', 'tenant-app', true, 'shop')];
    const subnets = [{ name: 's', network: 'tenant-app', network_id: 'app', cidr: '10.30.0.0/16', ipv6_address_mode: null, ipv6_ra_mode: null }];
    const found = preflight(vm, 'openstack', plan, { 'tenant-app': 1442 }, { selected: [vm], networkDetails: details, subnets });
    const refused = found.find((f) => f.code === 'NET_ADDRESS_REFUSED');
    expect(refused?.severity).toBe('blocker');
    expect(refused?.message).toBe(
      "Neutron would refuse these addresses on their network in the destination: fa:16:3e:00:00:01 on 'tenant-app': 10.20.1.5 is no host address of any subnet of the port's network.",
    );
    expect(refused?.remediation).toBe(
      'Give the NIC an address its destination network takes: map its network to one whose subnets hold it, or set the address in a NIC override; for a SLAAC address keep the MAC (port_creation_mode "neutron").',
    );
    const fits = [{ ...subnets[0]!, cidr: '10.20.0.0/16' }];
    expect(codes(preflight(vm, 'openstack', plan, { 'tenant-app': 1442 }, { selected: [vm], networkDetails: details, subnets: fits }))).not.toContain('NET_ADDRESS_REFUSED');
    // Nova creates the port with no owner: an IPv6 subnet's first address is taken (review NC-08)
    const first = makeVm({ id: 'os-o1', name: 'nic-01', disks: [{ size_gb: 20 }], nics: [{ network: 'tenant-app', mac: 'fa:16:3e:00:00:01', fixed_ips: ['fd00:30::'] }] });
    const v6 = [{ ...subnets[0]!, cidr: 'fd00:30::/64' }];
    const nova = { ...plan, port_creation_mode: 'nova' as const };
    expect(codes(preflight(first, 'openstack', nova, { 'tenant-app': 1442 }, { selected: [first], networkDetails: details, subnets: v6 }))).not.toContain('NET_ADDRESS_REFUSED');
  });

  it("blocks a NIC on another project's network the credential may not use, with the API's message (SDD §9.3, §6.6)", () => {
    const shared = net('p', 'tenant-web', false, 'project-admin');
    const external = { ...net('e', 'tenant-web', false, 'project-admin'), shared: false, external: true };
    const tenant = { ...net('b', 'tenant-web', false, 'project-b'), shared: false };
    const member = ['member', 'reader'];
    const run = (network: DestinationNetwork, roles: string[] | undefined, kind: ProviderKind = 'openstack') =>
      preflight(vm, kind, base, { 'tenant-web': 1442 }, {
        selected: [vm],
        networkDetails: [network],
        subnets: [{ name: 's', network: 'tenant-web', network_id: network.id!, cidr: '10.20.0.0/16', ipv6_address_mode: null, ipv6_ra_mode: null }],
        roles,
      });
    const denied = run(shared, member).find((f) => f.code === 'NET_PORT_NOT_PERMITTED');
    expect(denied?.severity).toBe('blocker');
    expect(denied?.message).toBe(
      "The credential of the destination may not put these NICs on their network: fa:16:3e:00:00:01 on 'tenant-web' (project project-admin): Neutron lets only the admin or advsvc role, or that project, choose a port's address or MAC there, and the credential's token has neither.",
    );
    expect(denied?.remediation).toBe(
      "Send the NIC to a network of the credential's project: map its network to one, or set a NIC override on the VM's migration page. Another project's shared network takes it once the destination credential has the advsvc role (Neutron's legacy rules). An external network that is not shared needs the admin role: Nova attaches it for no other.",
    );
    expect(run(external, member).find((f) => f.code === 'NET_PORT_NOT_PERMITTED')?.message).toBe(denied?.message);
    // another tenant's private network, whatever the roles
    for (const roles of [['admin'], undefined])
      expect(run(tenant, roles).find((f) => f.code === 'NET_PORT_NOT_PERMITTED')?.message).toBe(
        "The credential of the destination may not put these NICs on their network: fa:16:3e:00:00:01 on 'tenant-web' (project project-b): it is neither shared with the credential's project nor external.",
      );
    for (const roles of [['admin', 'member', 'reader'], ['advsvc'], undefined]) expect(codes(run(shared, roles))).not.toContain('NET_PORT_NOT_PERMITTED');
    // a VMware NIC too: the migration kit creates every port with the NIC's MAC (SDD §7.2)
    expect(codes(run(shared, member, 'vmware'))).toContain('NET_PORT_NOT_PERMITTED');
    // an unshared external network, the credential's own included, needs the admin role: Nova attaches it for no other (review RT-03)
    const nova = "it is external and not shared, and Nova lets only the admin role attach such a network to a server; the credential's token lacks it.";
    expect(run(external, ['advsvc']).find((f) => f.code === 'NET_PORT_NOT_PERMITTED')?.message).toContain(nova);
    const ownExternal = { ...net('x', 'tenant-web', true, 'shop'), shared: false, external: true };
    expect(run(ownExternal, member).find((f) => f.code === 'NET_PORT_NOT_PERMITTED')?.message).toBe(
      `The credential of the destination may not put these NICs on their network: fa:16:3e:00:00:01 on 'tenant-web' (project shop): ${nova}`,
    );
    expect(codes(run(ownExternal, ['admin']))).not.toContain('NET_PORT_NOT_PERMITTED');
  });

  it("judges another project's network by the destination's policy profile, naming the least role that clears it (SDD §21.5, review RV18-10)", () => {
    const shared = net('p', 'tenant-web', false, 'project-admin');
    const run = (roles: string[], profile: string) =>
      preflight(vm, 'openstack', base, { 'tenant-web': 1442 }, {
        selected: [vm],
        networkDetails: [shared],
        subnets: [{ name: 's', network: 'tenant-web', network_id: shared.id!, cidr: '10.20.0.0/16', ipv6_address_mode: null, ipv6_ra_mode: null }],
        roles,
        profile,
        destination: 'DC3 Jakarta',
      }).find((f) => f.code === 'NET_PORT_NOT_PERMITTED');
    expect(run(['service'], 'secure_rbac')).toBeUndefined();
    const refused = run(['advsvc'], 'secure_rbac');
    expect(refused?.message).toContain("Neutron's secure-RBAC defaults let only the admin or service role");
    expect(refused?.remediation).toBe(
      "Send the NIC to a network of the credential's project: map its network to one, or set a NIC override on the VM's migration page. Another project's shared network takes it once the destination credential has the service role for the migration window (Neutron's secure-RBAC defaults); admin is not needed for this. An external network that is not shared needs the admin role: Nova attaches it for no other.",
    );
    // unknown: the advice names the provider's check beside declaring, as the API's does (SDD §21.5, review 19)
    const unknown = run(['service'], 'unknown');
    // naming the provider, as the API (SDD §1 naming, review RV23-07)
    expect(unknown?.message).toContain('the policy profile of DC3 Jakarta is unknown');
    expect(unknown?.message).toContain('(check DC3 Jakarta so its series is detected, or declare its policy profile or series)');
    expect(unknown?.remediation).toBe(
      "Send the NIC to a network of the credential's project: map its network to one, or set a NIC override on the VM's migration page. Another project's shared network takes it once a check of the provider detects its series, or the provider declares its policy profile or series, so the check counts that profile's role: service under secure-RBAC, advsvc under the legacy rules. An external network that is not shared needs the admin role: Nova attaches it for no other.",
    );
  });

  it("writes a network's name and a missing project as the API's messages do (review NC-11)", () => {
    const quoted = "bob's-web";
    const plan = { ...base, mappings: { ...base.mappings, networks: { 'tenant-app': quoted } } };
    const unowned = { ...net('q', quoted, false, 'x'), project_id: null, shared: false };
    const subnets = [{ name: 's', network: quoted, network_id: 'q', cidr: '10.30.0.0/16', ipv6_address_mode: null, ipv6_ra_mode: null }];
    const found = preflight(vm, 'openstack', plan, { [quoted]: 1442 }, { selected: [vm], networkDetails: [unowned], subnets, roles: ['member'] });
    expect(found.find((f) => f.code === 'NET_PORT_NOT_PERMITTED')?.message).toBe(
      'The credential of the destination may not put these NICs on their network: fa:16:3e:00:00:01 on "bob\'s-web" (project None): it is neither shared with the credential\'s project nor external.',
    );
    expect(found.find((f) => f.code === 'NET_ADDRESS_REFUSED')?.message).toBe(
      'Neutron would refuse these addresses on their network in the destination: fa:16:3e:00:00:01 on "bob\'s-web": 10.20.1.5 is no host address of any subnet of the port\'s network.',
    );
  });

  it("checks a VMware NIC's IPv4 guest addresses by the migration kit's rule, with the API's message (SDD §9.3, §7.2)", () => {
    const plan = { ...base, mappings: { ...base.mappings, networks: { 'DC2-Prod': 'dc2-prod' } } };
    const details = [net('prod', 'dc2-prod', true, 'shop')];
    const subnets = ['10.2.0.0/24', '10.2.1.0/24'].map((cidr, i) => ({ id: `sub-${i}`, name: `s${i}`, network: 'dc2-prod', network_id: 'prod', cidr, ipv6_address_mode: null, ipv6_ra_mode: null }));
    const found = (...fixed_ips: string[]) => {
      const v = makeVm({ id: 'vm-x1', name: 'vmw-01', disks: [{ size_gb: 20 }], nics: [{ network: 'DC2-Prod', mac: '00:50:56:AA:BB:CC', fixed_ips }] });
      return preflight(v, 'vmware', plan, { 'dc2-prod': 1500 }, { selected: [v], networkDetails: details, subnets });
    };
    // one subnet holds them; an IPv6 address the kit never sends is not judged
    expect(codes(found('10.2.1.5', '10.2.1.6', 'fd00::5'))).not.toContain('NET_ADDRESS_REFUSED');
    expect(found('10.2.0.5', '10.2.1.5').find((f) => f.code === 'NET_ADDRESS_REFUSED')?.message).toBe(
      "Neutron would refuse these addresses on their network in the destination: 00:50:56:aa:bb:cc on 'dc2-prod': 10.2.0.5, 10.2.1.5 lie in different subnets of the port's network: the migration kit gives a NIC's IPv4 addresses one subnet.",
    );
    expect(found('10.2.0.255').find((f) => f.code === 'NET_ADDRESS_REFUSED')?.message).toContain("00:50:56:aa:bb:cc on 'dc2-prod': 10.2.0.255 is no host address of any subnet");
    expect(codes(found('fe80::1'))).not.toContain('NET_ADDRESS_REFUSED');
    // the kit keeps the guest's network settings: an override (itself refused) never moves a VMware NIC, as the API
    const v = makeVm({ id: 'vm-x1', name: 'vmw-01', disks: [{ size_gb: 20 }], nics: [{ network: 'DC2-Prod', mac: '00:50:56:aa:bb:cc', fixed_ips: ['10.2.0.5'] }] });
    const moved = { ...plan, nic_overrides: { 'vm-x1': { '00:50:56:aa:bb:cc': { network: 'twice', fixed_ips: null } } } };
    const twice = [...details, net('t1', 'twice', true, 'shop'), net('t2', 'twice', true, 'shop')];
    const all = codes(preflight(v, 'vmware', moved, { 'dc2-prod': 1500, twice: 1500 }, { selected: [v], networkDetails: twice, subnets }));
    expect(all).toContain('MAP_NIC_OVERRIDE_INVALID');
    expect(all).not.toContain('MAP_NETWORK_AMBIGUOUS');
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
    expect(codes(preflight(tall, 'openstack', base, { 'tenant-web': 1400 }, context))).not.toContain('NET_MTU_SHRINK');
    const elsewhere = { ...base, nic_overrides: { 'os-o1': { 'fa:16:3e:00:00:01': { network: 'tenant-web', fixed_ips: ['10.40.1.9'] } } } };
    expect(preflight(tall, 'openstack', elsewhere, { 'tenant-web': 1400 }, context).find((f) => f.code === 'MAP_NIC_OVERRIDE_INVALID')?.message).toContain(
      "fa:16:3e:00:00:01: 10.40.1.9 lies in no subnet of 'tenant-web'",
    );
  });

  it('checks no address when the inventory lists no subnet', () => {
    const plan = { ...base, mappings: { ...base.mappings, networks: {} } };
    const details = [net('app', 'tenant-app', true, 'shop')];
    for (const subnets of [[], undefined])
      expect(codes(preflight(vm, 'openstack', plan, { 'tenant-app': 1442 }, { selected: [vm], networkDetails: details, subnets }))).not.toContain(
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
      preflight(vm, 'openstack', plan, { 'tenant-app': 1442 }, { selected: [vm], networkDetails: details, subnets }).find((f) => f.code === 'MAP_NIC_OVERRIDE_INVALID')?.message;
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
    // the mock's RHOSO credential is an admin: jump-host-01 keeps its address on the shared provider-ext
    const roles = capabilityRoles(buildProviders(now).find((p) => p.id === 'rhoso-prod')!.capabilities);
    expect(roles).toContain('admin');
    for (const mode of ['nova', 'neutron'] as const) {
      const plan = { ...base, mappings: { networks: {}, flavors: {}, volume_types: {}, projects: {} }, port_creation_mode: mode };
      for (const v of vms) {
        const found = codes(preflight(v, 'openstack', plan, dst.networks, { selected: vms, subnets: dst.subnets, networkDetails: dst.network_details, roles }));
        expect(found, v.name).not.toContain('MAP_NETWORK_AMBIGUOUS');
        expect(found, `${v.name} ${mode}`).not.toContain('NET_ADDRESS_REFUSED');
        expect(found, `${v.name} ${mode}`).not.toContain('NET_PORT_NOT_PERMITTED');
      }
    }
    // the mock's VMware plan sends its VMs' NICs to erp-net: none is blocked either (SDD §7.2)
    const vmware = inventories['vcenter-hq'] as VMRef[];
    const erp = { ...base, mappings: { networks: { 'VM Network ERP': 'erp-net' }, flavors: {}, volume_types: {}, projects: {} } };
    for (const v of vmware) {
      const found = codes(preflight(v, 'vmware', erp, dst.networks, { selected: vmware, subnets: dst.subnets, networkDetails: dst.network_details, roles }));
      for (const code of ['MAP_NETWORK_AMBIGUOUS', 'NET_ADDRESS_REFUSED', 'NET_PORT_NOT_PERMITTED']) expect(found, `${v.name} ${code}`).not.toContain(code);
    }
    // a member's token could not keep it there
    const jump = vms.find((v) => v.name === 'jump-host-01')!;
    expect(codes(preflight(jump, 'openstack', base, dst.networks, { selected: vms, subnets: dst.subnets, networkDetails: dst.network_details, roles: ['member'] }))).toContain('NET_PORT_NOT_PERMITTED');
  });
});

describe('mock preflight reports a network RHOSO lacks like the API (SDD §9.3 MAP_NETWORK_MISSING, MAP_NETWORK_PRESTAGED)', () => {
  const base = buildFixtures(now).plans[0]!;
  const plan = (prestage: string[], networks: Record<string, string> = {}): Plan => ({
    ...base,
    prestage_resources: prestage,
    mappings: { networks, flavors: {}, volume_types: {}, projects: {} },
  });
  const nets = { 'tenant-web': 1500 };
  const networkFindings = <T extends { code: string }>(found: T[]) => found.filter((f) => f.code.startsWith('MAP_NETWORK_'));
  const MISSING = 'Add a network mapping or create a same-named network on the destination.';

  it('names the destination provider where a message means the destination cloud, as the API does (SDD §1 naming, plan T6)', () => {
    const vm = makeVm({
      id: 'os-t6',
      name: 'app-01',
      disks: [{ size_gb: 20 }],
      nics: [
        { network: 'lab-a', mac: 'fa:16:3e:00:00:09', fixed_ips: ['10.9.0.9'] },
        { network: 'lab-b', mac: 'fa:16:3e:00:00:0a', fixed_ips: ['10.9.1.9'] },
      ],
    });
    const found = preflight(vm, 'openstack', plan([], { 'lab-b': 'gone-net' }), nets, { destination: 'OpenStack 2026.2 Hibiscus (DC3)' });
    expect(networkFindings(found).map((f) => f.message)).toEqual([
      'No mapping or same-named network in OpenStack 2026.2 Hibiscus (DC3) for: lab-a.',
      'Network mapping target(s) do not exist in OpenStack 2026.2 Hibiscus (DC3): lab-b -> gone-net.',
    ]);
    expect(JSON.stringify(found)).not.toContain('RHOSO');
  });

  it('blocks an unmapped VMware port group, which nothing pre-stages (review RT-07)', () => {
    const vm = makeVm({ id: 'vm-x1', name: 'vmw-01', disks: [{ size_gb: 20 }], nics: [{ network: 'DC9-Unmapped', mac: '00:50:56:00:00:01', fixed_ips: ['10.9.0.5'] }] });
    for (const prestage of [[], ['networks']]) {
      expect(networkFindings(preflight(vm, 'vmware', plan(prestage), nets))).toEqual([
        {
          code: 'MAP_NETWORK_MISSING',
          severity: 'blocker',
          message: 'No mapping or same-named network in the destination for: DC9-Unmapped. VMware sources are not pre-staged: map every port group.',
          remediation: MISSING,
          strategies: [],
        },
      ]);
    }
  });

  it('names each missing OpenStack network once, as pre-staged when the plan pre-stages networks', () => {
    const vm = makeVm({
      id: 'os-n1',
      name: 'net-01',
      disks: [{ size_gb: 20 }],
      nics: [
        { network: 'lab-b', mac: 'fa:16:3e:00:00:01', fixed_ips: ['10.9.0.5'] },
        { network: 'lab-a', mac: 'fa:16:3e:00:00:02', fixed_ips: ['10.9.1.5'] },
        { network: 'lab-a', mac: 'fa:16:3e:00:00:03', fixed_ips: ['10.9.1.6'] },
      ],
    });
    expect(networkFindings(preflight(vm, 'openstack', plan([]), nets))).toEqual([
      { code: 'MAP_NETWORK_MISSING', severity: 'blocker', message: 'No mapping or same-named network in the destination for: lab-a, lab-b.', remediation: MISSING, strategies: [] },
    ]);
    expect(networkFindings(preflight(vm, 'openstack', plan(['networks']), nets))).toEqual([
      {
        code: 'MAP_NETWORK_PRESTAGED',
        severity: 'info',
        message: 'Pre-staging creates same-named network(s) in the destination: lab-a, lab-b.',
        remediation: 'Nothing to do: pre-staging creates the network on the destination with the same name. Add a network mapping to use an existing destination network instead.',
        strategies: [],
      },
    ]);
  });

  it('leaves a NIC its override sends to another network to MAP_NIC_OVERRIDE_INVALID', () => {
    const vm = makeVm({ id: 'os-n3', name: 'net-03', disks: [{ size_gb: 20 }], nics: [{ network: 'lab-a', mac: 'fa:16:3e:00:00:05', fixed_ips: ['10.9.0.5'] }] });
    const moved = { ...plan([]), nic_overrides: { 'os-n3': { 'fa:16:3e:00:00:05': { network: 'tenant-web', fixed_ips: null } } } };
    expect(networkFindings(preflight(vm, 'openstack', moved, nets))).toEqual([]);
  });

  it('blocks a mapping whose target RHOSO lacks, also when networks are pre-staged', () => {
    const vm = makeVm({ id: 'os-n2', name: 'net-02', disks: [{ size_gb: 20 }], nics: [{ network: 'lab-a', mac: 'fa:16:3e:00:00:04', fixed_ips: ['10.9.0.5'] }] });
    for (const prestage of [[], ['networks']]) {
      expect(networkFindings(preflight(vm, 'openstack', plan(prestage, { 'lab-a': 'gone-net' }), nets))).toEqual([
        {
          code: 'MAP_NETWORK_MISSING',
          severity: 'blocker',
          message: 'Network mapping target(s) do not exist in the destination: lab-a -> gone-net.',
          remediation: 'Fix the mapping: the destination network must exist.',
          strategies: [],
        },
      ]);
    }
  });
});

describe('the mock advisor names the destination to Jev like the API (SDD §1 naming, §13.4, review RV20-01)', () => {
  it('names the destination by kind and series: the settled series, else the declared one, never the name', () => {
    const dc3 = { kind: 'openstack' as ProviderKind, name: 'ACME Bank - Jakarta DC3 (prod)', capabilities: { release: '2026.2' } as Record<string, unknown>, release: null as string | null };
    expect(destinationDescriptor({ ...dc3, kind: 'rhoso' })).toBe('RHOSO 18.0');
    expect(destinationDescriptor(dc3)).toBe('a community OpenStack 2026.2 destination');
    expect(destinationDescriptor({ ...dc3, capabilities: {}, release: '2025.2' })).toBe('a community OpenStack 2025.2 destination');
    for (const unsettled of [{ release: null }, { release: "ACME's own build" }, {}]) {
      expect(destinationDescriptor({ ...dc3, capabilities: unsettled, release: null })).toBe('a community OpenStack destination');
    }
    // a declared series the APIs contradict: the check settles none, and none is named (review RV21-09)
    const contradicted = { release: null, release_range: ['2025.1', '2025.2'] };
    expect(destinationDescriptor({ ...dc3, capabilities: contradicted, release: '2024.1' })).toBe('a community OpenStack destination');
  });

  it('replaces a name wherever it stands as a word, the longest name first, as the API does', () => {
    const vm = { 'web-01': '[vm]' };
    expect(replaceNames('web-01-data, web-01_root, data-web-01 and web-010', vm)).toBe('[vm]-data, [vm]_root, data-[vm] and web-010');
    expect(replaceNames('ACME prod and ACME', { ACME: 'x', 'ACME prod': 'RHOSO 18.0' })).toBe('RHOSO 18.0 and x');
    expect(replaceNames('a cost of $1 (approx.)', { '$1 (approx.)': 'y' })).toBe('a cost of y');
    // one pass: what a replacement wrote is never searched again, as the API's (review RV22-04)
    expect(replaceNames('required on RHOSO prod (Jakarta); VM RHOSO', { 'RHOSO prod (Jakarta)': 'RHOSO 18.0', RHOSO: '[vm]' })).toBe('required on RHOSO 18.0; VM [vm]');
    expect(replaceNames('web-01 and vm', { 'web-01': '[vm]', vm: '[vm]' })).toBe('[vm] and [vm]');
  });

  it('seeds no advisor snapshot that names a destination provider', () => {
    const fixtures = buildFixtures(Date.parse('2026-10-16T08:00:00Z'));
    const names = fixtures.providers.filter((p) => p.role === 'destination').map((p) => p.name);
    expect(names.length).toBeGreaterThan(0);
    expect(fixtures.snapshots.length).toBeGreaterThan(0);
    for (const snapshot of fixtures.snapshots) {
      const sent = JSON.stringify(snapshot.payload);
      expect(names.filter((name) => sent.includes(name))).toEqual([]);
    }
  });
});

describe('the mock names the destination provider where the API does (SDD §1 naming, §9.3, review RV20-05)', () => {
  const name = 'OpenStack 2026.2 Hibiscus (DC3)';
  const providers = buildProviders(now);
  const source = providers.find((p) => p.id === 'rhosp17-dc1')!;
  const destination = { ...providers.find((p) => p.id === 'rhoso-prod')!, name };

  it('in a handover storage reason', () => {
    const plan = { ...buildFixtures(now).plans[0]!, handover: { enabled: true, backend_map: { 'tripleo-ceph': 'hostgroup@ceph-ssd#typo' } } };
    const vm = makeVm({ id: 'os-x9', name: 'rbd-09', disks: [{ size_gb: 20, volume_type: 'tripleo-ceph' }] });
    const reasons = eligibility(vm, 'openstack', plan, source, destination, []).storage_handover ?? [];
    expect(reasons.some((r) => r.includes(`${name} lists no pool hostgroup@ceph-ssd#typo`))).toBe(true);
    expect(reasons.filter((r) => r.includes('the destination'))).toEqual([]);
  });

  it('in NET_MTU_SHRINK, one finding worded as the API words it', () => {
    const base = buildFixtures(now).plans[0]!;
    const plan = { ...base, mappings: { ...base.mappings, networks: {} }, nic_overrides: {} };
    const vm = makeVm({
      id: 'os-m1',
      name: 'mtu-01',
      disks: [{ size_gb: 20 }],
      nics: [
        { network: 'tenant-web', mac: 'fa:16:3e:00:00:09', fixed_ips: [], mtu: 9000 },
        { network: 'tenant-db', mac: 'fa:16:3e:00:00:0a', fixed_ips: [], mtu: 9000 },
      ],
    });
    const found = preflight(vm, 'openstack', plan, { 'tenant-web': 1500, 'tenant-db': 1450 }, { selected: [vm], destination: name });
    expect(found.filter((f) => f.code === 'NET_MTU_SHRINK')).toEqual([
      expect.objectContaining({
        message: `The MTU in ${name} is smaller: tenant-db 9000 -> 1450, tenant-web 9000 -> 1500.`,
        remediation: 'Lower the guest MTU or map to a destination network with a larger MTU (ML2/OVN Geneve networks typically use 1442).',
      }),
    ]);
  });

  it.each(['openstack', 'vmware'] as const)('judges a %s NIC on a network named like an Object.prototype key by that name (review RV23-01)', (kind) => {
    const base = buildFixtures(now).plans[0]!;
    const plan = { ...base, mappings: { ...base.mappings, networks: {} }, nic_overrides: {} };
    const vm = makeVm({ id: 'os-m2', name: 'mtu-02', disks: [{ size_gb: 20 }], nics: [{ network: 'constructor', mac: 'fa:16:3e:00:00:0b', fixed_ips: [], mtu: 9000 }] });
    const found = preflight(vm, kind, plan, { constructor: 1500 }, { selected: [vm], destination: name });
    expect(found.filter((f) => f.code === 'NET_MTU_SHRINK').map((f) => f.message)).toEqual([`The MTU in ${name} is smaller: constructor 9000 -> 1500.`]);
  });
});

interface ProviderSide {
  conversion_host: boolean;
  capabilities: Record<string, unknown>;
  name?: string;
}

interface EligibilityCase {
  why: string;
  source_kind: ProviderKind;
  vm: VMRef;
  plan: Pick<Plan, 'handover' | 'port_creation_mode' | 'nic_overrides'>;
  source: ProviderSide;
  destination: ProviderSide;
  findings: { code: string; severity: Severity }[];
  /** The migration holds the destination volumes an earlier attempt created (SDD §9.2, §9.3); false when not given. */
  holds_volumes?: boolean;
  expected: Record<string, string[]>;
}

interface FindingCase {
  why: string;
  vm: VMRef;
  /** the plan's other selected VMs */
  others?: VMRef[];
  /** the source provider's kind, openstack when not given */
  source_kind?: ProviderKind;
  plan: Pick<Plan, 'handover' | 'port_creation_mode' | 'nic_overrides'> & Partial<Pick<Plan, 'mappings' | 'prestage_resources' | 'acknowledged_findings'>>;
  /** a Heat or Octavia case's source provider label (SDD §1 naming, §9.3) */
  source_label?: string;
  /** a Heat or Octavia case's source reads, as its inventory reads them (SDD §10, plan task N2) */
  source_inventory?: { stacks?: StackRead; lb_members?: MemberRead };
  /**
   * a network case's destination: its label, networks, subnets, the token's roles and the policy profile; a GPU case's
   * its flavors too, the vGPU profiles it declares and its GPU capacity, as its inventory reads them (SDD §22.3)
   */
  destination?: {
    label: string;
    networks: DestinationNetwork[];
    subnets?: DestinationSubnet[];
    roles?: string[];
    policy_profile?: string;
    flavors?: DestinationFlavor[];
    gpu_profiles?: Record<string, string>;
    gpu_capacity?: GpuCapacity;
  };
  /** the source provider's declared vGPU profiles, by flavor (SDD §22.3) */
  gpu_profiles?: Record<string, string>;
  /** the phase of the plan's migration of a VM that has one, by its source id: its share counts by it (SDD §9.3, §22.5) */
  phases?: Record<string, Phase>;
  /** each code's message; null for a code that must not be found (PR #64 review) */
  expected: Record<string, string | null>;
  /** a finding's own severity and remediation, where they are not its catalog's (plan task G12) */
  severities?: Record<string, Severity>;
  remediations?: Record<string, string>;
}

/** The API's reasons and finding texts, case by case: seamless/tests/fixtures/eligibility_cases.json. */
const ELIGIBILITY = JSON.parse(eligibilityRaw) as { cases: EligibilityCase[]; finding_cases: FindingCase[] };

describe("the mock's GPU mapping rule, as the API's (SDD §9.3, §22.4, plan task G4)", () => {
  const plan = buildFixtures(now).plans[0]!;
  const gpuVm = makeVm({ id: 'os-gpu1', name: 'ml-01', flavor: 'g1.vgpu', disks: [{ size_gb: 20 }], flavor_extra_specs: { 'resources:VGPU': '1' } });
  const flavors = ['m1.small', 'g1.a100-4c'];
  // the destination's one GPU flavor, as its inventory lists it (SDD §22.3)
  const gpuFlavors = gpuFlavorsOf([{ name: 'g1.a100-4c', vcpus: 8, ram_mb: 32768, disk_gb: 80, extra_specs: { 'resources:VGPU': '1' } }]);

  it("names a GPU VM's mapped flavor the destination lacks as MAP_FLAVOR_MISSING alone, as the API words it", () => {
    const mapped = { ...plan, mappings: { ...plan.mappings, flavors: { 'g1.vgpu': 'g1.nope' } } } as Plan;
    const found = preflight(gpuVm, 'openstack', mapped, {}, { selected: [gpuVm], destination: 'RHOSO 18.0', flavors, gpuFlavors });
    const codes = found.filter((f) => f.code === 'MAP_FLAVOR_MISSING' || f.code === 'VM_VGPU').map((f) => [f.code, f.message]);
    expect(codes).toEqual([['MAP_FLAVOR_MISSING', "Flavor 'g1.vgpu' is mapped to 'g1.nope', which does not exist in RHOSO 18.0."]]);
  });

  it('lifts the block once the mapping names a GPU flavor, and reads no mapping from the prototype', () => {
    const mapped = { ...plan, mappings: { ...plan.mappings, flavors: { 'g1.vgpu': 'g1.a100-4c' } } } as Plan;
    const context = { selected: [gpuVm], destination: 'RHOSO 18.0', flavors, gpuFlavors };
    expect(preflight(gpuVm, 'openstack', mapped, {}, context).map((f) => f.code)).not.toContain('VM_VGPU');
    const prototype = makeVm({ id: 'os-gpu2', name: 'ml-02', flavor: 'constructor', disks: [{ size_gb: 20 }], flavor_extra_specs: { 'resources:VGPU': '1' } });
    const found = preflight(prototype, 'openstack', plan, {}, { ...context, selected: [prototype] });
    expect(found.find((f) => f.code === 'VM_VGPU')!.message).toBe(
      "Flavor requests a vGPU (resources:VGPU). The plan maps no flavor to 'constructor': map it to a GPU flavor of RHOSO 18.0.",
    );
  });
});

describe("the mock's GPU VMs, flavors and pools, as the demo's (SDD §16, §22.10, plan task G10)", () => {
  /** A GpuDevice's fields as the API's model fills them. */
  const DEVICE = { profile: null, vendor_id: null, device_id: null, count: 1, resource_class: null, traits: [], forbidden_traits: [], same_provider: false, alias: null };
  /** The vGPU profiles the GPU destinations declare for their two vGPU flavors (SDD §22.3, §22.10). */
  const DECLARED: Record<string, string> = { 'g1.a100-4c': 'A100-4C', 'g1.a100-3-20c': 'A100-3-20C' };
  const pool = (traits: string[], free: number, providers: number, largest: number) => ({
    resource_class: 'VGPU',
    traits,
    forbidden_traits: [],
    same_provider: false,
    free,
    providers,
    largest_free: largest,
    detail: null,
  });

  it("the mock holds the demo's GPU VMs, flavors and pools", () => {
    const inventories = buildInventories();
    const providers = buildProviders(now);
    const vmsOf = (id: string) => new Map((inventories[id] as VMRef[]).map((vm) => [vm.name, vm]));
    const shape = (vm: VMRef | undefined) => vm && { vcpus: vm.vcpus, ram_mb: vm.ram_mb, power_state: vm.power_state, cbt_enabled: vm.cbt_enabled, gpus: vm.gpus };

    // an OpenStack VM whose flavor asks for a vGPU with its type's trait, its GPUs as its source reports them (SDD §22.2)
    const ml = vmsOf('rhosp17-dc1').get('gpu-ml-01')!;
    expect(ml.flavor).toBe('g1.vgpu');
    expect(ml.flavor_extra_specs).toEqual({ 'resources:VGPU': '1', 'trait:CUSTOM_NVIDIA_A100_4C': 'required' });
    expect(ml.gpus).toEqual([{ ...DEVICE, kind: 'vgpu', resource_class: 'VGPU', traits: ['CUSTOM_NVIDIA_A100_4C'] }]);
    expect(ml.gpus).toEqual(gpuRequests(ml.flavor_extra_specs));

    // a VMware vGPU, a MIG-backed one and a DirectPath I/O GPU of a powered-off VM (SDD §22.6)
    const vmware = vmsOf('vcenter-hq');
    expect(shape(vmware.get('dc2-ml-01'))).toEqual({ vcpus: 8, ram_mb: 32768, power_state: 'running', cbt_enabled: true, gpus: [{ ...DEVICE, kind: 'vgpu', profile: 'grid_a100-4c' }] });
    expect(shape(vmware.get('dc2-ml-02'))).toEqual({ vcpus: 16, ram_mb: 65536, power_state: 'running', cbt_enabled: true, gpus: [{ ...DEVICE, kind: 'vgpu', profile: 'grid_a100-3-20c' }] });
    expect(shape(vmware.get('dc2-cuda-01'))).toEqual({
      vcpus: 16,
      ram_mb: 131072,
      power_state: 'stopped',
      cbt_enabled: true,
      gpus: [{ ...DEVICE, kind: 'passthrough', vendor_id: '10de', device_id: '20b5' }],
    });

    // the GPU destinations: three GPU flavors, the profiles their providers declare, and the pools of their vGPU flavors
    for (const id of ['rhoso-prod', 'hibiscus-dc3']) {
      const provider = providers.find((p) => p.id === id)!;
      expect(provider.gpu_profiles, id).toEqual(DECLARED);
      const inventory = inventories[id] as DestinationInventory;
      const flavor = (name: string) => inventory.flavors.find((f) => f.name === name);
      expect(flavor('g1.a100-4c'), id).toMatchObject({ vcpus: 8, ram_mb: 32768, extra_specs: { 'resources:VGPU': '1', 'trait:CUSTOM_NVIDIA_A100_4C': 'required' } });
      expect(flavor('g1.a100-3-20c'), id).toMatchObject({ vcpus: 16, ram_mb: 65536, extra_specs: { 'resources:VGPU': '1', 'trait:CUSTOM_NVIDIA_A100_3_20C': 'required' } });
      expect(flavor('p1.a100'), id).toMatchObject({ vcpus: 16, ram_mb: 131072, extra_specs: { 'pci_passthrough:alias': 'a100:1' } });
      // the flavors whose extra specs ask for a GPU, read as the API's gpu_requests reads them with the profiles the
      // provider declares at the time of the read - never kept with the inventory (§22.3)
      expect(inventory, id).not.toHaveProperty('gpu_flavors');
      const read = readDestinationGpus(inventory, provider.gpu_profiles, iso(now));
      expect(read.gpu_flavors, id).toEqual(
        ['g1.a100-4c', 'g1.a100-3-20c', 'p1.a100'].map((name) => {
          const { vcpus, ram_mb, disk_gb, extra_specs } = flavor(name)!;
          return { name, vcpus, ram_mb, disk_gb, gpus: gpuRequests(extra_specs, DECLARED[name] ?? null) };
        }),
      );
      // A100-4C's pool 4 free on 1 provider, A100-3-20C's 2 free on 1, and the class alone, which unites them (§22.3)
      const pools = [pool(['CUSTOM_NVIDIA_A100_4C'], 4, 1, 4), pool(['CUSTOM_NVIDIA_A100_3_20C'], 2, 1, 2), pool([], 6, 2, 4)];
      expect(inventory.gpu_capacity!.pools, id).toEqual(pools);
      expect(read.gpu_capacity, id).toEqual({ status: 'ok', reason: null, detail: null, read_at: iso(now), pools });
      // the destination's check reads the same pools, when it runs (SDD §10, §22.3)
      const report: GpuCheckReport = provider.capabilities;
      expect(report.gpu_capacity, id).toEqual({ status: 'ok', reason: null, detail: null, read_at: provider.last_checked_at, pools });
    }

    // a destination without a GPU flavor reads ok with no pools, and sends Placement nothing (§22.3)
    const staging = readDestinationGpus(inventories['rhoso-staging'], undefined, iso(now));
    expect(staging).toEqual({ gpu_flavors: [], gpu_capacity: { status: 'ok', reason: null, detail: null, read_at: iso(now), pools: [] } });
  });

  it("keeps the seeded plans' GPU findings as they were: the GPU flavors map nothing on their own (SDD §22.4)", () => {
    const fixtures = buildFixtures(now);
    // the render VM's plan still maps its flavor to one that asks for no GPU: it stays blocked, saying why
    const render = fixtures.migrations.find((m) => m.vm.name === 'gpu-render-01')!;
    expect(render.phase).toBe('blocked');
    expect(render.findings.find((f) => f.code === 'VM_VGPU')?.message).toBe(
      "Flavor requests a vGPU (resources:VGPU). Its flavor mapping, 'g1.a10' to 'm1.large', names a flavor of RHOSO 18.0 — Production that asks for no GPU: the server would lose its GPU.",
    );
    expect(render.findings.map((f) => f.code)).not.toContain('MAP_FLAVOR_MISSING');
  });
});

describe('the mock words eligibility and the handover findings as the API (SDD §9.2, §9.3, §16, review RV20-08)', () => {
  const base = buildFixtures(now).plans[0]!;
  const provider = (id: string, kind: ProviderKind, side: ProviderSide) =>
    ({ id, name: side.name ?? id, kind, capabilities: side.capabilities, conversion_host: side.conversion_host ? { manage: true, name: 'conv', flavor: null } : null }) as Provider;

  it.each(ELIGIBILITY.cases)('$why', (c) => {
    const plan = { ...base, ...c.plan } as Plan;
    const findings = c.findings.map((f) => ({ code: f.code, severity: f.severity, message: '', remediation: null, strategies: [] }) as Finding);
    const destinationKind: ProviderKind = c.source_kind === 'vmware' ? 'rhoso' : 'openstack';
    const reasons = eligibility(
      c.vm, c.source_kind, plan, provider('src', c.source_kind, c.source), provider('dst', destinationKind, c.destination), findings, c.holds_volumes ?? false,
    );
    expect(reasons).toEqual(c.expected);
  });

  it.each(ELIGIBILITY.finding_cases)('$why', (c) => {
    const plan = { ...base, ...c.plan } as Plan;
    const selected = [c.vm, ...(c.others ?? [])];
    const dest = c.destination;
    const networks = Object.fromEntries((dest?.networks ?? []).map((n) => [n.name, n.mtu]));
    const context = {
      ...(dest
        ? { selected, networkDetails: dest.networks, subnets: dest.subnets ?? [], roles: dest.roles, profile: dest.policy_profile, destination: dest.label }
        : { selected }),
      // a GPU case's destination flavors, its GPU flavors among them as its inventory lists them, and its capacity (SDD §22.3)
      ...(dest?.flavors ? { flavors: dest.flavors.map((f) => f.name), gpuFlavors: gpuFlavorsOf(dest.flavors, dest.gpu_profiles) } : {}),
      ...(dest?.gpu_capacity ? { gpuCapacity: dest.gpu_capacity } : {}),
      shares: Object.fromEntries(Object.entries(c.phases ?? {}).map(([vmId, phase]) => [vmId, quotaShare(phase)])),
      gpuProfiles: c.gpu_profiles,
      // a Heat or Octavia case's source, by its label, and its reads (SDD §9.3, §10, plan task N2)
      source: c.source_label,
      stacks: c.source_inventory?.stacks,
      lbMembers: c.source_inventory?.lb_members,
    };
    const found = preflight(c.vm, c.source_kind ?? 'openstack', plan, networks, context);
    const messages = new Map(found.map((f) => [f.code, f.message]));
    expect(Object.fromEntries(Object.keys(c.expected).map((code) => [code, messages.get(code) ?? null]))).toEqual(c.expected);
    const byCode = new Map(found.map((f) => [f.code, f]));
    expect(Object.fromEntries(Object.keys(c.severities ?? {}).map((code) => [code, byCode.get(code)?.severity ?? null]))).toEqual(c.severities ?? {});
    expect(Object.fromEntries(Object.keys(c.remediations ?? {}).map((code) => [code, byCode.get(code)?.remediation ?? null]))).toEqual(c.remediations ?? {});
  });

  it('raises both source-state blockers for a server in error with a Nova task, naming the task, as the API (SDD §9.3, review PV-05)', () => {
    const plan = { ...base } as Plan;
    const template = ELIGIBILITY.finding_cases.find((c) => c.vm.task_state)!.vm;
    const vm: VMRef = { ...template, power_state: 'error', task_state: 'deleting' };
    const found = preflight(vm, 'openstack', plan, {}, { selected: [vm] });
    expect(found.map((f) => f.code)).toEqual(expect.arrayContaining(['SRC_VM_ERROR_STATE', 'SRC_VM_TRANSITIONAL_STATE']));
    expect(found.find((f) => f.code === 'SRC_VM_TRANSITIONAL_STATE')!.message).toBe(
      'Source VM backup-01 has a Nova task in flight (deleting); a stop or snapshot would fail.',
    );
  });

  it("judges a VMware source's VMs by the kit's names only, once, and never two unusable names as twins, as the API (SDD §9.3, reviews RV26-04, RV28-04)", () => {
    const plan = { ...base } as Plan;
    const template = ELIGIBILITY.finding_cases.find((c) => c.source_kind === 'vmware')!.vm;
    const vmOf = (source_id: string, name: string) => ({ ...template, source_id, name, project: null });
    const judged = (vm: VMRef, others: VMRef[], kind: ProviderKind) =>
      preflight(vm, kind, plan, {}, { selected: [vm, ...others] }).filter((f) => f.code === 'SRC_VM_DUPLICATE_NAME' || f.code === 'SRC_VM_NAME_UNUSABLE');
    const dotted = vmOf('vm-1', 'web.01');
    expect(judged(dotted, [vmOf('vm-2', 'web_01')], 'openstack')).toEqual([]);
    expect(judged(vmOf('vm-3', 'サーバー'), [], 'openstack')).toEqual([]);
    // a VM named exactly alike is a kit twin too: one finding with the kit's reason and remedy names every VM written alike
    expect(judged(dotted, [vmOf('vm-4', 'web.01'), vmOf('vm-2', 'web_01')], 'vmware').map((f) => [f.message, f.remediation])).toEqual([
      [
        "Another selected VM has the name 'web_01' as the migration kit writes it (-/vm-2, -/vm-4); the kit names the VM's server, ports and volumes after it.",
        "Rename one of the VMs in vCenter: the migration kit names a VM's server, ports and volumes after its name as it writes it, so the two would share them - in separate plans too, when they go to one destination project.",
      ],
    ]);
    // an OpenStack source keeps os-migrate's reason for the same name
    expect(judged(dotted, [vmOf('vm-4', 'web.01')], 'openstack').map((f) => f.message)).toEqual([
      "Another selected VM is also named 'web.01' (-/vm-4); os-migrate selects workloads by name.",
    ]);
    expect(judged(vmOf('vm-5', 'サーバー'), [vmOf('vm-6', 'веб')], 'vmware').map((f) => f.code)).toEqual(['SRC_VM_NAME_UNUSABLE']);
  });
});
