import { describe, expect, it } from 'vitest';
// One series table and one case table for the control plane and the dashboard (SDD §10.1, §21.1; plan T8).
import casesRaw from '../../../seamless/tests/fixtures/destination_release_cases.json?raw';
import seriesRaw from '../../../seamless/tests/fixtures/release_series.json?raw';
import { destinationRelease, plainHttpText, profileText, RELEASE_CHOICES, rangeText, SERIES, seriesText, tierText, type DestinationRelease } from './platforms';

const shared = JSON.parse(seriesRaw) as { series: unknown[] };
const cases = JSON.parse(casesRaw) as {
  cases: Array<{
    why: string;
    kind: 'openstack' | 'rhoso';
    release: string | null;
    policy_profile: 'secure_rbac' | 'legacy' | null;
    compute_max: string;
    block_storage_max: string | null;
    expected: DestinationRelease;
  }>;
};

describe('the destination series rules, like the control plane (SDD §10.1)', () => {
  it("keeps the API's series table: names, codenames, phases, tiers and maxima", () => {
    expect(SERIES).toEqual(shared.series);
  });

  it.each(cases.cases.map((c) => [c.why, c] as const))('%s', (_why, c) => {
    expect(destinationRelease(c.kind, c.release, c.policy_profile, c.compute_max, c.block_storage_max)).toEqual(c.expected);
  });

  it("offers the window's series newest first, with their codenames, for a declared series (SDD §16)", () => {
    expect(RELEASE_CHOICES.map((choice) => choice.value)).toEqual(['2026.2', '2026.1', '2025.2', '2025.1', '2024.2', '2024.1', '2023.2', '2023.1']);
    expect(RELEASE_CHOICES[0]!.label).toBe('2026.2 Hibiscus');
  });
});

describe('what the provider card says (SDD §16)', () => {
  it('names a series with its codename, and a range from its oldest to its newest', () => {
    expect(seriesText('2026.2')).toBe('2026.2 Hibiscus');
    expect(seriesText('2027.1')).toBe('2027.1');
    expect(rangeText(['2024.1', '2024.2'])).toBe('2024.1 Caracal – 2024.2 Dalmatian');
    expect(rangeText(['2026.2'])).toBe('2026.2 Hibiscus');
    expect(rangeText([])).toBeNull();
  });

  it('says the tier in words, with the reason of Tier 3 and Outside', () => {
    expect(tierText({ tier: 'validated', reason: null, declared_outside_range: false })).toBe('Validated');
    expect(tierText({ tier: 'tier1', reason: null, declared_outside_range: false })).toBe('Tier 1');
    expect(tierText({ tier: 'tier3', reason: 'end_of_life', declared_outside_range: false })).toBe('Tier 3 · End of Life upstream');
    expect(tierText({ tier: 'outside', reason: 'below_floor', declared_outside_range: false })).toBe('Outside · older than 2023.1, the window’s oldest');
    expect(tierText({ tier: 'outside', reason: 'newer_than_validated', declared_outside_range: false })).toBe('Outside · newer than 2026.2, the window’s newest');
    expect(tierText({ tier: 'outside', reason: 'not_detected', declared_outside_range: false })).toBe('Outside · series not detected');
  });

  it('says the policy profile in words', () => {
    expect(profileText('secure_rbac')).toBe('Secure RBAC defaults');
    expect(profileText('legacy')).toBe('Legacy rules');
    expect(profileText('unknown')).toBe('Unknown');
  });

  it('names the endpoints that use plain HTTP in words (SDD §10)', () => {
    expect(plainHttpText(['endpoint', 'auth_url', 'block-storage'])).toBe('the provider’s endpoint, the auth URL and block storage');
    expect(plainHttpText(['identity'])).toBe('identity');
  });
});
