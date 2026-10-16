import { describe, expect, it } from 'vitest';
// the API's finding catalog (SDD §9.3): one copy the API and the offline mock are held to
import catalogRaw from '../../../seamless/tests/fixtures/finding_catalog.json?raw';
import { FINDING_CATALOG } from './findingCatalog';
import { buildFixtures } from './mockData';

const SHARED = JSON.parse(catalogRaw) as { catalog: Record<string, { severity: string; strategies: string[]; remediation: string }> };
/** The findings the API words with a remediation or severity of their own (planning/preflight.py finding(..., remediation=, severity=)). */
const OWN = new Set(['MAP_NETWORK_MISSING', 'NET_PORT_NOT_PERMITTED', 'MAP_FLAVOR_MISSING', 'MAP_FLAVOR_AUTO', 'MAP_VOLUME_TYPE_MISSING']);

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
});
