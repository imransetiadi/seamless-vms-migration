import { describe, expect, it } from 'vitest';
import type { DestinationNetwork } from '../api/types';
import { capabilityRoles, portRefusal, resolveNetwork } from './networks';

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
});

describe('portRefusal (SDD §6.6, §9.3)', () => {
  const roleWhy = "Neutron lets only the admin or advsvc role, or that project, choose a port's address or MAC there, and the credential's token has neither";
  const privateWhy = "it is neither shared with the credential's project nor external";

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
      expect(portRefusal(external, roles)).toBeNull();
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
