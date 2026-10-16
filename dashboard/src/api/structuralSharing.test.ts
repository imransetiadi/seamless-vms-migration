import { describe, expect, it } from 'vitest';
import { shareStructure } from './structuralSharing';

/** A plan's mappings as the API serves them: JSON, where "__proto__" is an own key like any other (SDD §9.2). */
const served = (networks: string) => JSON.parse(`{"id": "plan-1", "mappings": {"networks": ${networks}, "flavors": {"m1": "m2"}}}`) as {
  id: string;
  mappings: { networks: Record<string, string>; flavors: Record<string, string> };
};

describe("the query cache's structural sharing (SDD §16, review RV24-02)", () => {
  it('keeps a network the API maps under "__proto__", which replaceEqualDeep hands to the prototype setter', () => {
    const before = served('{"analytics": "tenant-app"}');
    const after = served('{"__proto__": "tenant-web", "analytics": "tenant-app"}');
    const shared = shareStructure(before, after) as typeof after;
    expect(Object.entries(shared.mappings.networks)).toEqual([
      ['__proto__', 'tenant-web'],
      ['analytics', 'tenant-app'],
    ]);
    // the first answer too, with nothing cached before it
    expect(Object.hasOwn((shareStructure(undefined, after) as typeof after).mappings.networks, '__proto__')).toBe(true);
  });

  it('still keeps what did not change by reference, as React Query does', () => {
    const before = served('{"analytics": "tenant-app"}');
    expect(shareStructure(before, served('{"analytics": "tenant-app"}'))).toBe(before);
    const changed = shareStructure(before, served('{"analytics": "tenant-web"}')) as typeof before;
    expect(changed).not.toBe(before);
    expect(changed.mappings.flavors).toBe(before.mappings.flavors);
    expect(changed.mappings.networks).toEqual({ analytics: 'tenant-web' });
  });
});
