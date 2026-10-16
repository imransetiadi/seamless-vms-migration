import { describe, expect, it } from 'vitest';
import eligibilityRaw from '../../../seamless/tests/fixtures/eligibility_cases.json?raw';
import { buildFixtures, buildInventories, buildProviders, destinationDescriptor, eligibility, makeVm, preflight, replaceNames } from './mockData';
import { capabilityRoles } from '../lib/networks';
import type { DestinationInventory, DestinationNetwork, Finding, Plan, Provider, ProviderKind, Severity, VMRef } from './types';

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
  expected: Record<string, string[]>;
}

interface FindingCase {
  why: string;
  vm: VMRef;
  /** the plan's other selected VMs */
  others?: VMRef[];
  plan: Pick<Plan, 'handover' | 'port_creation_mode' | 'nic_overrides'>;
  expected: Record<string, string>;
}

/** The API's reasons and finding texts, case by case: seamless/tests/fixtures/eligibility_cases.json. */
const ELIGIBILITY = JSON.parse(eligibilityRaw) as { cases: EligibilityCase[]; finding_cases: FindingCase[] };

describe('the mock words eligibility and the handover findings as the API (SDD §9.2, §9.3, §16, review RV20-08)', () => {
  const base = buildFixtures(now).plans[0]!;
  const provider = (id: string, kind: ProviderKind, side: ProviderSide) =>
    ({ id, name: side.name ?? id, kind, capabilities: side.capabilities, conversion_host: side.conversion_host ? { manage: true, name: 'conv', flavor: null } : null }) as Provider;

  it.each(ELIGIBILITY.cases)('$why', (c) => {
    const plan = { ...base, ...c.plan } as Plan;
    const findings = c.findings.map((f) => ({ code: f.code, severity: f.severity, message: '', remediation: null, strategies: [] }) as Finding);
    const destinationKind: ProviderKind = c.source_kind === 'vmware' ? 'rhoso' : 'openstack';
    const reasons = eligibility(c.vm, c.source_kind, plan, provider('src', c.source_kind, c.source), provider('dst', destinationKind, c.destination), findings);
    expect(reasons).toEqual(c.expected);
  });

  it.each(ELIGIBILITY.finding_cases)('$why', (c) => {
    const plan = { ...base, ...c.plan } as Plan;
    const selected = [c.vm, ...(c.others ?? [])];
    const found = preflight(c.vm, 'openstack', plan, {}, { selected });
    expect(Object.fromEntries(found.filter((f) => f.code in c.expected).map((f) => [f.code, f.message]))).toEqual(c.expected);
  });
});
