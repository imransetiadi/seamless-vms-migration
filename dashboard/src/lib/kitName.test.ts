import { describe, expect, it } from 'vitest';
// SafeVmName of the pinned migration kit 2.2.7, run on each name with its own Go code (SDD §7.2): one copy the API's
// planning/kit.py and this port are held to
import casesRaw from '../../../seamless/tests/fixtures/kit_vm_name_cases.json?raw';
import { kitVmName } from './kitName';

const CASES = JSON.parse(casesRaw) as { cases: Record<string, string> };

describe("the migration kit's name of a VM, as the API's kit_vm_name (SDD §7.2, §9.3, review RV26-04)", () => {
  it.each(Object.entries(CASES.cases))('%j is %j to the kit', (name, kit) => {
    expect(kitVmName(name)).toBe(kit);
  });

  it("reads no name from Object's prototype as a transliteration", () => {
    expect(kitVmName('constructor')).toBe('constructor');
    expect(kitVmName('toString')).toBe('toString');
  });
});
