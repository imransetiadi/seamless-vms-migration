/**
 * Destination release series (SDD §10.1, §21.1–§21.3), as the API's planning/platforms.py: the community series a
 * destination may run, read from its services' maximum microversions, its support tier and its policy profile. The
 * provider card words them (SDD §16) and the mock's check reports them; release_series.json and
 * destination_release_cases.json keep this file and the API's equal (plan T8).
 */
import type { ProviderKind } from '../api/types';

export type Tier = 'tier1' | 'tier2' | 'tier3';
export type PolicyProfile = 'secure_rbac' | 'legacy' | 'unknown';

export interface Series {
  name: string;
  codename: string;
  phase: 'Maintained' | 'Unmaintained' | 'End of Life';
  slurp: boolean;
  tier: Tier;
  compute_max: string;
  block_storage_max: string | null;
}

export interface SupportTier {
  tier: Tier | 'validated' | 'outside';
  reason: 'end_of_life' | 'below_floor' | 'newer_than_validated' | 'not_detected' | null;
  declared_outside_range: boolean;
}

/** What a destination's check reports about its series (SDD §10, §10.1). */
export interface DestinationRelease {
  release_range: string[];
  release: string | null;
  support_tier: SupportTier;
  policy_profile: PolicyProfile;
}

/** The window's community series, oldest first: the API's SERIES (SDD §21.1, §21.3). */
export const SERIES: readonly Series[] = [
  { name: '2023.1', codename: 'Antelope', phase: 'Unmaintained', slurp: true, tier: 'tier2', compute_max: '2.95', block_storage_max: '3.70' },
  { name: '2023.2', codename: 'Bobcat', phase: 'End of Life', slurp: false, tier: 'tier3', compute_max: '2.95', block_storage_max: '3.70' },
  { name: '2024.1', codename: 'Caracal', phase: 'Unmaintained', slurp: true, tier: 'tier2', compute_max: '2.96', block_storage_max: '3.71' },
  { name: '2024.2', codename: 'Dalmatian', phase: 'End of Life', slurp: false, tier: 'tier3', compute_max: '2.96', block_storage_max: '3.71' },
  { name: '2025.1', codename: 'Epoxy', phase: 'Maintained', slurp: true, tier: 'tier1', compute_max: '2.100', block_storage_max: null },
  { name: '2025.2', codename: 'Flamingo', phase: 'Maintained', slurp: false, tier: 'tier1', compute_max: '2.100', block_storage_max: null },
  { name: '2026.1', codename: 'Gazpacho', phase: 'Maintained', slurp: true, tier: 'tier1', compute_max: '2.103', block_storage_max: null },
  { name: '2026.2', codename: 'Hibiscus', phase: 'Maintained', slurp: false, tier: 'tier1', compute_max: '2.104', block_storage_max: null },
];

/** The tiers of the window, the most favourable first. */
const TIERS: readonly Tier[] = ['tier1', 'tier2', 'tier3'];
/** The oldest series whose oslo.policy enforces the new defaults (SDD §21.5). */
const SECURE_RBAC_FROM = '2023.2';
const REASONS = { older: 'below_floor', newer: 'newer_than_validated' } as const;

type Version = [number, number];

/** A microversion such as "2.100" as numbers - so 2.9 is below 2.95 - or null. */
export function parseMicroversion(value: unknown): Version | null {
  const match = typeof value === 'string' ? /^\s*(\d+)\.(\d+)\s*$/.exec(value) : null;
  return match ? [Number(match[1]), Number(match[2])] : null;
}

const compare = (a: Version, b: Version) => a[0] - b[0] || a[1] - b[1];
const seriesNumber = (name: string): Version => {
  const [year, half] = name.split('.');
  return [Number(year), Number(half)];
};

interface ReleaseRange {
  series: string[];
  outside: 'older' | 'newer' | 'not_detected' | null;
}

/** Whether a cloud whose Cinder maximum is `blockStorage` may run `series`: its confirmed maximum, else at least the older series' (RV18-05). */
function cinderFits(series: Series, blockStorage: Version): boolean {
  if (series.block_storage_max !== null) return compare(parseMicroversion(series.block_storage_max)!, blockStorage) === 0;
  const older = SERIES.slice(0, SERIES.indexOf(series))
    .map((s) => parseMicroversion(s.block_storage_max))
    .filter((v): v is Version => v !== null);
  const floor = older.reduce<Version | null>((max, v) => (max === null || compare(v, max) > 0 ? v : max), null);
  return floor === null || compare(blockStorage, floor) >= 0;
}

/** The series a cloud with these maximum microversions may run (SDD §10.1). */
export function releaseRange(computeMax: unknown, blockStorageMax: unknown = null): ReleaseRange {
  const compute = parseMicroversion(computeMax);
  if (compute === null) return { series: [], outside: 'not_detected' };
  const maxima = SERIES.map((series) => [parseMicroversion(series.compute_max)!, series] as const);
  if (compare(compute, maxima[0]![0]) < 0) return { series: [], outside: 'older' };
  if (compare(compute, maxima[maxima.length - 1]![0]) > 0) return { series: [], outside: 'newer' };
  let found = maxima.filter(([max]) => compare(max, compute) === 0).map(([, series]) => series);
  if (!found.length) {
    const below = maxima.filter(([max]) => compare(max, compute) < 0).pop()![1];
    const above = maxima.find(([max]) => compare(max, compute) > 0)![1];
    found = [below, above];
  }
  const blockStorage = parseMicroversion(blockStorageMax);
  if (blockStorage !== null) {
    const narrowed = found.filter((series) => cinderFits(series, blockStorage));
    if (narrowed.length) found = narrowed;
  }
  return { series: found.map((series) => series.name), outside: null };
}

/** Whether a declared series decides: the range holds it, or the range is empty and does not contradict it (RV18-06). */
function declaredDecides(found: ReleaseRange, declared: string): boolean {
  if (found.series.includes(declared)) return true;
  if (found.series.length) return false;
  if (found.outside === 'older') return compare(seriesNumber(declared), seriesNumber(SERIES[0]!.name)) < 0;
  if (found.outside === 'newer') return compare(seriesNumber(declared), seriesNumber(SERIES[SERIES.length - 1]!.name)) > 0;
  return true;
}

/** The policy profile a community cloud's series implies: the settled series, else every series of the range. */
function profileBySeries(settled: string | null, found: ReleaseRange): PolicyProfile {
  const series = settled !== null ? [settled] : found.series;
  if (!series.length) return found.outside === 'newer' ? 'secure_rbac' : found.outside === 'older' ? 'legacy' : 'unknown';
  const secure = new Set(series.map((name) => compare(seriesNumber(name), seriesNumber(SECURE_RBAC_FROM)) >= 0));
  if (secure.size === 2) return 'unknown';
  return secure.has(true) ? 'secure_rbac' : 'legacy';
}

/** The tier of a cloud whose detected range is `found`, `declared` its provider's declared series (SDD §10.1). */
export function supportTier(found: ReleaseRange, declared: string | null = null): SupportTier {
  const tiers = new Map(SERIES.map((series) => [series.name, series.tier]));
  if (declared !== null && declaredDecides(found, declared)) {
    const tier = tiers.get(declared);
    if (tier === undefined) {
      const older = compare(seriesNumber(declared), seriesNumber(SERIES[0]!.name)) < 0;
      return { tier: 'outside', reason: older ? 'below_floor' : 'newer_than_validated', declared_outside_range: false };
    }
    return { tier, reason: tier === 'tier3' ? 'end_of_life' : null, declared_outside_range: false };
  }
  const outsideRange = declared !== null;
  if (!found.series.length) {
    const reason = found.outside === 'older' || found.outside === 'newer' ? REASONS[found.outside] : 'not_detected';
    return { tier: 'outside', reason, declared_outside_range: outsideRange };
  }
  const tier = found.series.map((name) => tiers.get(name)!).reduce((worst, t) => (TIERS.indexOf(t) > TIERS.indexOf(worst) ? t : worst));
  return { tier, reason: tier === 'tier3' ? 'end_of_life' : null, declared_outside_range: outsideRange };
}

/**
 * What a destination's check reports about its series (SDD §10.1): the API's destination_release. `release` and
 * `policyProfile` are what its provider declares; with no maximum read it is the provider's declarations alone (RV18-04).
 */
export function destinationRelease(
  kind: ProviderKind,
  release: string | null,
  policyProfile: 'secure_rbac' | 'legacy' | null,
  computeMax: unknown,
  blockStorageMax: unknown,
): DestinationRelease {
  const found = releaseRange(computeMax, blockStorageMax);
  const settled = release !== null && declaredDecides(found, release) ? release : found.series.length === 1 ? found.series[0]! : null;
  // RHOSO 18.0: validated by its kind; legacy until the lab decides D14
  const tier: SupportTier = kind === 'rhoso' ? { tier: 'validated', reason: null, declared_outside_range: false } : supportTier(found, release);
  const fallback: PolicyProfile = kind === 'rhoso' ? 'legacy' : profileBySeries(settled, found);
  return { release_range: found.series, release: settled, support_tier: tier, policy_profile: policyProfile ?? fallback };
}

/** The window's series for a declared series, newest first (SDD §16). */
export const RELEASE_CHOICES = [...SERIES].reverse().map((series) => ({ value: series.name, label: `${series.name} ${series.codename}` }));

/** A series with its codename when the window knows it: "2026.2 Hibiscus". */
export function seriesText(name: string): string {
  const series = SERIES.find((s) => s.name === name);
  return series ? `${series.name} ${series.codename}` : name;
}

/** A detected range from its oldest to its newest series, or null when it holds none. */
export function rangeText(range: readonly string[]): string | null {
  if (!range.length) return null;
  const first = seriesText(range[0]!);
  return range.length === 1 ? first : `${first} – ${seriesText(range[range.length - 1]!)}`;
}

const TIER_WORDS: Record<SupportTier['tier'], string> = { validated: 'Validated', tier1: 'Tier 1', tier2: 'Tier 2', tier3: 'Tier 3', outside: 'Outside' };
const REASON_WORDS: Record<NonNullable<SupportTier['reason']>, string> = {
  end_of_life: 'End of Life upstream',
  below_floor: `older than ${SERIES[0]!.name}, the window’s oldest`,
  newer_than_validated: `newer than ${SERIES[SERIES.length - 1]!.name}, the window’s newest`,
  not_detected: 'series not detected',
};

/** The support tier in words, with the reason of Tier 3 and Outside (SDD §16). */
export function tierText(tier: SupportTier): string {
  const words = TIER_WORDS[tier.tier] ?? String(tier.tier);
  return tier.reason ? `${words} · ${REASON_WORDS[tier.reason] ?? tier.reason}` : words;
}

const PROFILE_WORDS: Record<PolicyProfile, string> = { secure_rbac: 'Secure RBAC defaults', legacy: 'Legacy rules', unknown: 'Unknown' };

/** A policy profile in words (SDD §21.5). */
export function profileText(profile: PolicyProfile): string {
  return PROFILE_WORDS[profile] ?? 'Unknown';
}

const PLAIN_HTTP_WORDS: Record<string, string> = {
  endpoint: 'the provider’s endpoint',
  auth_url: 'the auth URL',
  'block-storage': 'block storage',
};

/** The endpoints a check reports as plain HTTP (`plain_http`, SDD §10), in words: "the auth URL and compute". */
export function plainHttpText(names: readonly string[]): string {
  const words = names.map((name) => PLAIN_HTTP_WORDS[name] ?? name);
  return words.length > 1 ? `${words.slice(0, -1).join(', ')} and ${words[words.length - 1]}` : (words[0] ?? '');
}

/** The destinations, where the dashboard lists platforms (SDD §16, §21; plan T6): the sign-in page names them so. */
export const DESTINATIONS = 'RHOSO 18.0 and community OpenStack';
/** The same with the window's series, oldest to newest: the Providers page and its empty state. */
export const DESTINATION_PLATFORMS = `${DESTINATIONS} ${rangeText(SERIES.map((series) => series.name))}`;
/** The destinations in a few words, where the sidebar has room for no more: the brand line under the product's name. */
export const BRAND_DESTINATIONS = 'RHOSO 18.0 or OpenStack';
