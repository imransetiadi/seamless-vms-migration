import { describe, expect, it } from 'vitest';
import { buildFixtures } from '../api/mockData';
import type { Migration } from '../api/types';
import { attentionItems } from './attention';

const NOW = Date.parse('2026-10-08T12:00:00Z');

function scenario() {
  const fx = buildFixtures(NOW);
  const plans = new Map(fx.plans.map((p) => [p.id, p]));
  const byName = (name: string): Migration => {
    const m = fx.migrations.find((x) => x.vm.name === name);
    if (!m) throw new Error(name);
    return m;
  };
  return { fx, plans, byName };
}

describe('attentionItems', () => {
  it('ranks a failed cutover with a stopped source first', () => {
    const { fx, plans } = scenario();
    const items = attentionItems(fx.migrations, plans, NOW);
    expect(items[0]?.migration.vm.name).toBe('legacy-rhel6-app');
    expect(items[0]?.reason).toMatch(/source VM is stopped/);
  });

  it('flags single-shot migrations at the cutover gate only when their wave is active (SDD §5.4)', () => {
    const { fx, plans, byName } = scenario();
    // wave-3 depends on wave-1 and wave-2, which are still running.
    byName('api-gw-02').phase = 'ready';
    const names = attentionItems(fx.migrations, plans, NOW).map((i) => i.migration.vm.name);
    expect(names).toContain('ad-dc-01'); // wave-2 has no dependencies
    expect(names).not.toContain('api-gw-02');
  });

  it('distinguishes waiting for approval from waiting for a cutover request', () => {
    const { fx, plans, byName } = scenario();
    const billing = byName('app-billing-01');
    expect(attentionItems(fx.migrations, plans, NOW).find((i) => i.migration === billing)?.reason).toMatch(/waiting for approval/);
    billing.approvals = [{ actor: 'sari', at: '2026-10-08T11:59:00Z', comment: null }];
    expect(attentionItems(fx.migrations, plans, NOW).find((i) => i.migration === billing)?.reason).toMatch(/cutover request/);
  });
});
