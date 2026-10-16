import { describe, expect, it } from 'vitest';
// One case table for the control plane and the dashboard (SDD §6.6, §21.5; plan T8).
import raw from '../../../seamless/tests/fixtures/port_refusal_cases.json?raw';
import type { DestinationNetwork, DestinationSubnet, Nic, ProviderKind } from '../api/types';
import { capabilityRoles, kitSubnet, policyProfileOf, portRefusal, resolveNetwork } from './networks';
import { kitIpv4 } from './nics';

const net = (id: string, name: string, own: boolean, project_id = own ? 'shop' : `${id}-owner`): DestinationNetwork => ({
  id,
  name,
  mtu: 1442,
  project_id,
  own,
  shared: !own,
});

describe('resolveNetwork (SDD §7.3 step 0, §9.3)', () => {
  it("takes the credential's own network of the name, else the only one it sees, as the API does", () => {
    expect(resolveNetwork([net('a', 'tenant-web', true)], 'tenant-web')).toEqual({ network: net('a', 'tenant-web', true), why: null });
    expect(resolveNetwork([net('x', 'tenant-web', false), net('a', 'tenant-web', true)], 'tenant-web').network?.id).toBe('a');
    expect(resolveNetwork([net('x', 'tenant-web', false)], 'tenant-web').network?.id).toBe('x');
    expect(resolveNetwork([net('a', 'tenant-web', true), net('b', 'tenant-web', true)], 'tenant-web')).toEqual({
      network: null,
      why: "2 networks of the credential's project are named 'tenant-web'",
    });
    expect(resolveNetwork([net('a', 'tenant-web', false, 'p1'), net('b', 'tenant-web', false, 'p2')], 'tenant-web')).toEqual({
      network: null,
      why: "2 networks of other projects are named 'tenant-web' (p1, p2), none of the credential's project",
    });
    expect(resolveNetwork([net('a', 'tenant-web', true)], 'gone')).toEqual({ network: null, why: null });
  });

  it("writes the name and a missing project as the API does (review NC-11)", () => {
    expect(resolveNetwork([net('a', "bob's-net", true), net('b', "bob's-net", true)], "bob's-net").why).toBe(
      '2 networks of the credential\'s project are named "bob\'s-net"',
    );
    const unowned = { ...net('a', 'x', false), project_id: null };
    expect(resolveNetwork([unowned, net('b', 'x', false, 'p2')], 'x').why).toBe(
      "2 networks of other projects are named 'x' (None, p2), none of the credential's project",
    );
  });
});

describe('portRefusal (SDD §6.6, §9.3)', () => {
  const roleWhy = "Neutron lets only the admin or advsvc role, or that project, choose a port's address or MAC there, and the credential's token has neither";
  const privateWhy = "it is neither shared with the credential's project nor external";
  const novaWhy =
    "it is external and not shared, and Nova lets only the admin role attach such a network to a server; the credential's token lacks it";

  it('asks the admin role for an unshared external network, the credential\'s own included, as the API does (review RT-03)', () => {
    const ownExternal = { ...net('x', 'rhoso-net', true), shared: false, external: true };
    for (const roles of [['member', 'reader'], ['advsvc']]) expect(portRefusal(ownExternal, roles)).toBe(novaWhy);
    expect(portRefusal(ownExternal, ['admin'])).toBeNull();
    // shared, it is any token's to attach; no reported roles judge neither half
    expect(portRefusal({ ...ownExternal, shared: true }, ['member'])).toBeNull();
    expect(portRefusal(ownExternal, undefined)).toBeNull();
  });

  it("lets another project's network take a port only when shared or external, and from the admin or advsvc role, as the API does", () => {
    const shared = net('p', 'rhoso-net', false, 'project-admin');
    const external = { ...net('e', 'rhoso-net', false, 'project-admin'), shared: false, external: true };
    const tenant = { ...net('b', 'rhoso-net', false, 'project-b'), shared: false };
    const member = ['member', 'reader'];
    // the credential's own network, whatever its roles
    expect(portRefusal(net('o', 'rhoso-net', true), member)).toBeNull();
    expect(portRefusal(shared, member)).toBe(roleWhy);
    expect(portRefusal(external, member)).toBe(roleWhy);
    for (const roles of [['admin', 'member', 'reader'], ['advsvc']]) {
      expect(portRefusal(shared, roles)).toBeNull();
      // Nova attaches an unshared external network for the admin role only (review RT-03)
      expect(portRefusal(external, roles)).toBe(roles.includes('admin') ? null : novaWhy);
      // an admin sees another tenant's private network: the VM must never land there
      expect(portRefusal(tenant, roles)).toBe(privateWhy);
    }
    // a destination that reports no roles: that half is not judged
    expect(portRefusal(shared, undefined)).toBeNull();
    expect(portRefusal(tenant, undefined)).toBe(privateWhy);
  });

  it('reads the roles a destination check reported, if any (SDD §10)', () => {
    expect(capabilityRoles({ admin: true, roles: ['admin', 'reader'] })).toEqual(['admin', 'reader']);
    expect(capabilityRoles({ admin: true })).toBeUndefined();
    expect(capabilityRoles(undefined)).toBeUndefined();
  });
});

describe('the VMware migration kit (SDD §7.2)', () => {
  const subnet = (id: string, cidr: string): DestinationSubnet => ({ id, name: id, network: 'dc2-prod', network_id: 'net-prod', cidr });
  const nic = (...fixed_ips: string[]): Nic => ({ network: 'DC2-Prod', mac: '00:50:56:aa:bb:cc', fixed_ips, vnic_type: 'normal', mtu: null });

  it('sends Neutron the IPv4 guest addresses only, an IPv4-mapped IPv6 one as its IPv4, as the API reads it', () => {
    expect(kitIpv4(['10.2.0.15', 'fe80::1', '::ffff:10.2.0.16', 'not-an-ip', 'fd00::5'])).toEqual(['10.2.0.15', '10.2.0.16']);
  });

  it("gives a NIC's port the subnet holding its IPv4 addresses, as the API's kit_subnet", () => {
    const v6 = subnet('sub-v6', 'fd00::/64');
    const first = subnet('sub-a', '10.2.0.0/24');
    const second = subnet('sub-b', '10.2.1.0/24');
    expect(kitSubnet(nic('10.2.1.5', 'fe80::1'), [v6, first, second])).toEqual({ subnet: second, why: null });
    // no IPv4 address: any of its subnets, an IPv4 one first; none at all: no subnet
    expect(kitSubnet(nic('fe80::1'), [v6, first])).toEqual({ subnet: first, why: null });
    expect(kitSubnet(nic(), [v6])).toEqual({ subnet: v6, why: null });
    expect(kitSubnet(nic(), [])).toEqual({ subnet: null, why: null });
    for (const address of ['192.0.2.5', '10.2.0.0', '10.2.0.255'])
      expect(kitSubnet(nic(address), [first, second])).toEqual({ subnet: null, why: `${address} is no host address of any subnet of the port's network` });
    expect(kitSubnet(nic('10.2.0.5', '10.2.1.5'), [first, second])).toEqual({
      subnet: null,
      why: "10.2.0.5, 10.2.1.5 lie in different subnets of the port's network: the migration kit gives a NIC's IPv4 addresses one subnet",
    });
    expect(kitSubnet(nic('10.2.0.5', '10.2.0.6'), [first, second])).toEqual({ subnet: first, why: null });
  });
});

const shared = JSON.parse(raw) as {
  networks: Record<string, Pick<DestinationNetwork, 'own' | 'shared' | 'external'>>;
  cases: Array<{ network: string; profile: string; roles: string[] | null; expected: string | null }>;
  profile_cases: Array<{
    why: string;
    capabilities: Record<string, unknown>;
    provider: { kind: ProviderKind; release: string | null; policy_profile: 'secure_rbac' | 'legacy' | null };
    expected: string;
  }>;
};

describe('the port rule by the policy profile, like the control plane (SDD §6.6, §21.5)', () => {
  it.each(shared.cases.map((c) => [`${c.network}, ${c.profile}, ${JSON.stringify(c.roles)}`, c] as const))('%s', (_name, c) => {
    const network: DestinationNetwork = { id: 'n', name: 'n', mtu: 1500, project_id: 'p', ...shared.networks[c.network]! };
    expect(portRefusal(network, c.roles ?? undefined, c.profile)).toBe(c.expected);
  });

  it.each(shared.profile_cases.map((c) => [c.why, c] as const))('the profile: %s', (_why, c) => {
    expect(policyProfileOf(c.capabilities, c.provider)).toBe(c.expected);
  });
});
