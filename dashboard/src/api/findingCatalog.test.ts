import { describe, expect, it } from 'vitest';
// the API's finding catalog (SDD §9.3): one copy the API and the offline mock are held to
import catalogRaw from '../../../seamless/tests/fixtures/finding_catalog.json?raw';
import { FINDING_CATALOG, NETWORK_CODES } from './findingCatalog';
import { buildFixtures } from './mockData';

const SHARED = JSON.parse(catalogRaw) as { catalog: Record<string, { severity: string; strategies: string[]; remediation: string }> };
/** The findings the API words with a remediation or severity of their own (planning/preflight.py finding(..., remediation=, severity=)). */
const OWN = new Set(['MAP_NETWORK_MISSING', 'NET_PORT_NOT_PERMITTED', 'MAP_FLAVOR_MISSING', 'MAP_FLAVOR_AUTO', 'MAP_VOLUME_TYPE_MISSING', 'MAP_GPU_FLAVOR', 'SRC_VM_AUTOSCALED']);

describe("the offline mock's finding catalog is the API's (SDD §9.3, §16)", () => {
  it("holds each code's severity, strategies and default remediation as the API's CATALOG", () => {
    expect(FINDING_CATALOG).toEqual(SHARED.catalog);
  });

  it("gives every finding of the demo the catalog's severity, strategies and remediation, as the API's finding()", () => {
    const findings = buildFixtures(Date.parse('2026-10-09T12:00:00Z')).migrations.flatMap((m) => m.findings);
    expect(new Set(findings.map((f) => f.code)).size).toBeGreaterThan(5);
    const off = findings.filter((f) => {
      const entry = SHARED.catalog[f.code];
      if (!entry) return true;
      if (OWN.has(f.code)) return JSON.stringify(f.strategies) !== JSON.stringify(entry.strategies);
      return f.severity !== entry.severity || JSON.stringify(f.strategies) !== JSON.stringify(entry.strategies) || f.remediation !== entry.remediation;
    });
    expect([...new Set(off.map((f) => `${f.code}: ${f.remediation}`))]).toEqual([]);
  });

  // a conversion host without a declared key or a pin, and a data path that checks none (SDD §9.3, §26.5; plan task ST7)
  it('has the two host key findings', () => {
    const strategies = ['cold', 'warm', 'vmware_cold', 'vmware_warm'];
    expect(FINDING_CATALOG.CONV_HOST_KEY_UNKNOWN).toEqual({
      severity: 'blocker',
      strategies,
      remediation:
        "Declare the conversion host's public keys on its provider (conversion_host.ssh_host_keys), read out of band; or, for a host Seamless manages, delete it and pre-stage again, which creates it with an injected key.",
    });
    expect(FINDING_CATALOG.CONV_HOST_KEYS_UNCHECKED).toEqual({
      severity: 'warning',
      strategies,
      remediation:
        'Set SEAMLESS_ANSIBLE_HOST_KEY_CHECKING to true: each conversion host then needs declared keys or a pin, which a host Seamless creates gets injected and the provider check reads from the console of one it finds.',
    });
    // right after CONV_HOST_MISSING, in the API's order
    const codes = Object.keys(FINDING_CATALOG);
    expect(codes.slice(codes.indexOf('CONV_HOST_MISSING'), codes.indexOf('CONV_HOST_MISSING') + 3)).toEqual(['CONV_HOST_MISSING', 'CONV_HOST_KEY_UNKNOWN', 'CONV_HOST_KEYS_UNCHECKED']);
  });

  // seamless/tests/test_preflight.py holds them to §16's list, which a dashboard test cannot read (vite.config.ts fs.allow)
  it('holds the thirteen network findings, each a code of the catalog (SDD §16, §23.7, §23.9, plan task N12c)', () => {
    expect(NETWORK_CODES.size).toBe(13);
    expect([...NETWORK_CODES].filter((code) => !(code in FINDING_CATALOG))).toEqual([]);
  });
});
