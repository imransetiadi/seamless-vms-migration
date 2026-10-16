/** Which plans still need a provider, as the API decides it before a delete (SDD §12, §16). */
import type { Plan, Provider } from '../api/types';
import { planStatusMeta } from './status';

/** Phases that release a VM (SDD §5.4): a plan needs its providers for no such migration any more. */
const RELEASES_VM: ReadonlySet<string> = new Set(['cancelled', 'finalized', 'rolled_back']);

export interface ProviderUser {
  plan: Plan;
  /** Why it still needs the provider: its status as the plan shows it, or its VMs not finalized. */
  why: string;
}

const notFinalized = (n: number) => `${n} VM${n === 1 ? '' : 's'} not finalized`;

/**
 * The plans that still need a provider, as source or destination (SDD §12): one stored neither completed nor
 * cancelled, or a completed one with VMs not finalized, rolled back or cancelled - a rollback needs both providers
 * until a VM is finalized. The stored status decides, as in the API; the reason reads as the plan shows it. A cancelled
 * migration's cleanup still under way, and a rolled-back VM keeping its copied data for a cancel to clean up, keep the
 * providers too, but the plan list does not carry them: the API's 409 names them (SDD §16).
 */
export function plansThatNeed(providerId: string, plans: readonly Plan[]): ProviderUser[] {
  return plans.flatMap((plan) => {
    if (plan.source_provider_id !== providerId && plan.destination_provider_id !== providerId) return [];
    const holding = Object.entries(plan.migration_counts).reduce((n, [phase, count]) => (RELEASES_VM.has(phase) ? n : n + (count ?? 0)), 0);
    if (plan.status === 'completed' || plan.status === 'cancelled') return holding > 0 ? [{ plan, why: notFinalized(holding) }] : [];
    const why = plan.derived_status === 'completed' && holding > 0 ? notFinalized(holding) : planStatusMeta(plan.derived_status).label.toLowerCase();
    return [{ plan, why }];
  });
}

/** Why Delete is unavailable while plans still need the provider, three by name at most (SDD §16); null when none does. */
export function deleteRefusal(users: readonly ProviderUser[], shown = 3): string | null {
  if (!users.length) return null;
  const names = users
    .slice(0, shown)
    // quoted: a plan's name may hold parentheses, dashes and commas
    .map((u) => `“${u.plan.name}” (${u.why})`)
    .join(', ');
  const more = users.length > shown ? ` and ${users.length - shown} more` : '';
  return `${users.length === 1 ? 'A plan still needs' : 'Plans still need'} it: ${names}${more}. A plan needs its providers until it completes and its VMs are finalized.`;
}

/**
 * The name a text gives a provider, as the API's Provider.label (SDD §1 naming): its name, else its id - a name stored
 * blank before the API refused one (review RV20-11) never reads "in : ...".
 */
export function providerLabel(provider: Pick<Provider, 'id' | 'name'>): string {
  return isBlank(provider.name) ? provider.id : provider.name;
}

/**
 * The code points a blank name holds only - Unicode 15.1's separators (Z), controls (Cc) and format characters (Cf) -
 * pinned, as the API's BLANK_RANGES, so every runtime answers alike whatever its own Unicode tables (SDD §4.2, review
 * RV22-06); seamless/tests/fixtures/blank_ranges.json holds both to the same list.
 */
export const BLANK_RANGES: ReadonlyArray<readonly [number, number]> = [
  [0x0000, 0x0020],
  [0x007f, 0x00a0],
  [0x00ad, 0x00ad],
  [0x0600, 0x0605],
  [0x061c, 0x061c],
  [0x06dd, 0x06dd],
  [0x070f, 0x070f],
  [0x0890, 0x0891],
  [0x08e2, 0x08e2],
  [0x1680, 0x1680],
  [0x180e, 0x180e],
  [0x2000, 0x200f],
  [0x2028, 0x202f],
  [0x205f, 0x2064],
  [0x2066, 0x206f],
  [0x3000, 0x3000],
  [0xfeff, 0xfeff],
  [0xfff9, 0xfffb],
  [0x110bd, 0x110bd],
  [0x110cd, 0x110cd],
  [0x13430, 0x1343f],
  [0x1bca0, 0x1bca3],
  [0x1d173, 0x1d17a],
  [0xe0001, 0xe0001],
  [0xe0020, 0xe007f],
];

/**
 * Whether a provider's name is blank: no character outside BLANK_RANGES - empty, spaces, U+200B, U+FEFF -, the API's
 * rule for refusing one and for labelling one stored before (SDD §4.2, reviews RV21-08, RV22-06).
 */
export function isBlank(name: string): boolean {
  for (const ch of name) {
    const cp = ch.codePointAt(0)!;
    if (!BLANK_RANGES.some(([first, last]) => cp >= first && cp <= last)) return false;
  }
  return true;
}

/**
 * The name a text about the destination cloud gives it where a plan or a migration is shown: the plan's destination
 * provider's label, else "the destination" (SDD §1 naming, §16, plan T6).
 */
export function destinationName(providers: readonly Provider[] | undefined, plan: Pick<Plan, 'destination_provider_id'> | null | undefined): string {
  const destination = plan ? providers?.find((p) => p.id === plan.destination_provider_id) : undefined;
  return destination ? providerLabel(destination) : 'the destination';
}
