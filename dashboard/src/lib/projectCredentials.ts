import type { Migration, Phase, Plan, ProjectCheck, Provider } from '../api/types';
import { formatDateTime } from './format';
import { providerLabel } from './providerUse';

/**
 * How long each project credential a cutover and a rollback would act with must still last, as the last check read it,
 * for the cutover to start (SDD §5.4 rule 6, §27.7): 24 hours.
 */
export const CUTOVER_CREDENTIAL_MARGIN_MS = 24 * 3_600_000;

/** The phases before a cutover, whose gate rule 6 holds (SDD §5.4, §16). */
const BEFORE_CUTOVER: ReadonlySet<Phase> = new Set<Phase>(['ready', 'precopy', 'syncing', 'awaiting_cutover']);

/** What lets a held cutover start: a check of the provider, or a new credential an admin writes, then its check. */
const REMEDY = {
  check: 'Check the provider: the cutover starts once a check reads it.',
  write: 'An admin writes a new credential for the project, then checks the provider.',
} as const;

/** A project credential that holds a cutover (SDD §5.4 rule 6, §16), in the migration page's words. */
export interface CredentialHold {
  side: 'source' | 'destination';
  projectId: string;
  /** "tenant-c (d4e5…)", or its id alone while its check read no name. */
  project: string;
  /** Its provider's name. */
  provider: string;
  /** Why it holds the cutover. */
  why: string;
  /** What lets the cutover start. */
  remedy: string;
}

/** A project credential's entry of its provider's check report (SDD §10), when the report holds one. */
export function projectCheckOf(provider: Pick<Provider, 'capabilities'>, projectId: string): ProjectCheck | null {
  const report = provider.capabilities['projects'];
  if (!report || typeof report !== 'object' || !Object.hasOwn(report, projectId)) return null;
  const entry = (report as Record<string, unknown>)[projectId];
  return entry && typeof entry === 'object' ? (entry as ProjectCheck) : null;
}

/** A project as the API's credential findings name it (SDD §9.3): the name its check read, with its id; its id alone. */
export function projectWords(projectId: string, entry: Pick<ProjectCheck, 'project_name'> | null): string {
  const name = entry?.project_name;
  return typeof name === 'string' && name.trim() && name !== projectId ? `${name} (${projectId})` : projectId;
}

/** A time a report holds as text, in milliseconds; null for none and for one that is no time. */
function timeOf(value: unknown): number | null {
  if (typeof value !== 'string') return null;
  const ms = Date.parse(value);
  return Number.isNaN(ms) ? null : ms;
}

/**
 * Why the provider's credential for the project holds a cutover at `now` (SDD §5.4 rule 6), as the API's
 * `credential_hold` judges the provider's record: written since its last check - no entry, or one checked before the
 * credential's `updated_at` - until a check reads it; its last check failed; an expiry within 24 hours of `now`, or one
 * that is no time. Null while it lasts or never expires, and for a project the provider holds no credential for: no
 * step acts with it, and a step's sign-in fails it before it changes anything (§7.1).
 */
export function credentialHold(provider: Provider, projectId: string, now: number): Pick<CredentialHold, 'why' | 'remedy'> | null {
  const held = provider.project_credentials && Object.hasOwn(provider.project_credentials, projectId) ? provider.project_credentials[projectId] : undefined;
  if (!held) return null;
  const written = timeOf(held.updated_at);
  const entry = projectCheckOf(provider, projectId);
  if (!entry) return { why: `written at ${formatDateTime(held.updated_at)}, not checked since`, remedy: REMEDY.check };
  const checked = timeOf(entry.checked_at);
  if (checked === null || (written !== null && checked < written)) {
    return { why: `written at ${formatDateTime(held.updated_at)}, after its last check`, remedy: REMEDY.check };
  }
  if (entry.status !== 'ok') return { why: entry.message ? `its last check failed: ${entry.message}` : 'its last check failed', remedy: REMEDY.check };
  if (entry.expires_at === null || entry.expires_at === undefined) return null;
  const expiry = timeOf(entry.expires_at);
  if (expiry === null) return { why: 'its last check read an expiry that is no time', remedy: REMEDY.check };
  if (expiry >= now + CUTOVER_CREDENTIAL_MARGIN_MS) return null;
  const when = formatDateTime(entry.expires_at);
  return { why: expiry <= now ? `expired at ${when}` : `expires at ${when}, within 24 hours`, remedy: REMEDY.write };
}

/**
 * Each project credential that holds the migration's cutover at `now` (SDD §5.4 rule 6, §16): before it cut over, the
 * plan's `source_project` at its source and its `destination_project` at its destination - the credentials its cutover
 * and a rollback act with -, judged as their providers' records hold them, the source's first. None for a plan that
 * names no project, and none while a provider is not loaded.
 */
export function cutoverHolds(
  m: Pick<Migration, 'phase'>,
  plan: Pick<Plan, 'source_project' | 'destination_project'> | null | undefined,
  source: Provider | undefined,
  destination: Provider | undefined,
  now: number,
): CredentialHold[] {
  if (!plan || !BEFORE_CUTOVER.has(m.phase)) return [];
  const sides = [
    ['source', plan.source_project, source],
    ['destination', plan.destination_project, destination],
  ] as const;
  const holds: CredentialHold[] = [];
  for (const [side, projectId, provider] of sides) {
    if (!projectId || !provider) continue;
    const hold = credentialHold(provider, projectId, now);
    if (!hold) continue;
    const project = projectWords(projectId, projectCheckOf(provider, projectId));
    holds.push({ side, projectId, project, provider: providerLabel(provider), ...hold });
  }
  return holds;
}

/** The project its own credential's token is scoped to, as its last check names it (`project`, SDD §10, §27.7); null until one does. */
export function ownProject(provider: Pick<Provider, 'capabilities'>): { id: string; name: string | null } | null {
  const project = provider.capabilities['project'];
  if (!project || typeof project !== 'object') return null;
  const { id, name } = project as { id?: unknown; name?: unknown };
  if (typeof id !== 'string' || !id) return null;
  return { id, name: typeof name === 'string' && name.trim() ? name : null };
}

/** The provider's own project as the card and the pickers name it (SDD §16): "finance (project-finance)", or null. */
export function ownProjectWords(provider: Pick<Provider, 'capabilities'>): string | null {
  const own = ownProject(provider);
  return own ? projectWords(own.id, { project_name: own.name }) : null;
}

/** A project credential as the dashboard shows it (SDD §16): its project's words and what its last check read. */
export interface ProjectCredentialRow {
  projectId: string;
  /** "shop (5f0c…)", its id alone while no check read a name. */
  project: string;
  updatedAt: string;
  /** Its check's entry, unless it was written since. */
  check: ProjectCheck | null;
  /** "Checked", "Check failed" or "Not checked since written" (SDD §16). */
  status: 'ok' | 'error' | 'unchecked';
}

/**
 * The provider's project credentials in the order of their projects' names (SDD §16), each with its last check's entry -
 * none while the report holds none for it or one checked before it was written - and the name that entry read.
 */
export function projectCredentialRows(provider: Pick<Provider, 'capabilities' | 'project_credentials'>): ProjectCredentialRow[] {
  return Object.entries(provider.project_credentials ?? {})
    .map(([projectId, credential]) => {
      const entry = projectCheckOf(provider, projectId);
      const written = timeOf(credential.updated_at);
      const checked = entry ? timeOf(entry.checked_at) : null;
      const current = entry && checked !== null && (written === null || checked >= written) ? entry : null;
      return {
        projectId,
        project: projectWords(projectId, entry),
        updatedAt: credential.updated_at,
        check: current,
        status: current ? (current.status === 'ok' ? 'ok' : 'error') : 'unchecked',
      } satisfies ProjectCredentialRow;
    })
    .sort((a, b) => a.project.localeCompare(b.project) || (a.projectId < b.projectId ? -1 : 1));
}

/** How long before an expiry the dashboard warns, as PROJECT_CREDENTIAL_EXPIRING does (SDD §27.7): 7 days. */
export const EXPIRY_WARNING_MS = 7 * 24 * 3_600_000;

/** A project choice of the plan form's pickers and the Inventory's select (SDD §16): '' is the provider's own credential. */
export interface ProjectChoice {
  value: string;
  label: string;
}

/**
 * The projects a plan or a read may act in on a provider (SDD §16, §27.4): first its own credential's project, sent as
 * none, then each project it holds a credential for, by name with its id.
 */
export function projectChoices(provider: Pick<Provider, 'capabilities' | 'project_credentials'>): ProjectChoice[] {
  const own = ownProjectWords(provider);
  return [
    { value: '', label: own ? `Its own credential's project: ${own}` : "Its own credential's project" },
    ...projectCredentialRows(provider).map((row) => ({ value: row.projectId, label: row.project })),
  ];
}
