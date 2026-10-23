/** The Events page's categories (SDD §16): each files the event kinds its predicate matches; an
 * event shows under every category it matches. */
export const EVENT_CATEGORIES: Array<{ value: string; label: string; match: (kind: string) => boolean }> = [
  { value: 'all', label: 'All', match: () => true },
  { value: 'migrations', label: 'Migrations', match: (k) => k.startsWith('migration.') },
  { value: 'plans', label: 'Plans and waves', match: (k) => k.startsWith('plan.') || k.startsWith('wave.') },
  {
    value: 'advisor',
    label: 'Advisor and memory',
    // the advisor's audit events (SDD §14.5) are advisor activity too, its policies' changes included
    match: (k) =>
      k.startsWith('advisor.') || k.startsWith('memory.') || k.startsWith('recommendation.') || k.startsWith('decision.') || k.startsWith('policy.'),
  },
  // a conversion host's pinned key is its provider's (SDD §4.2, §26.4)
  { value: 'providers', label: 'Providers', match: (k) => k.startsWith('provider.') || k.startsWith('conversion_host.') },
  // a prune cuts the audit trail itself (SDD §15); a dashboard user created grants access (SDD §13.1); a policy change
  // can weaken an approval gate (SDD §14.5); a hook runs code with the control plane's reach (SDD §23.3); a conversion
  // host's key decides whom the data path trusts (SDD §26.5)
  {
    value: 'security',
    label: 'Security',
    match: (k) =>
      k.startsWith('auth.') ||
      k.startsWith('user.') ||
      k.startsWith('policy.') ||
      k.startsWith('hook.') ||
      k.startsWith('conversion_host.') ||
      k === 'events.pruned',
  },
];
