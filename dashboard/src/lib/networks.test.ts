import { describe, expect, it } from 'vitest';
import type { DestinationNetwork } from '../api/types';
import { resolveNetwork } from './networks';

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
