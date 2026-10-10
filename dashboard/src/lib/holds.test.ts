import { describe, expect, it } from 'vitest';
import { buildFixtures } from '../api/mockData';
import type { Migration, Phase, Plan } from '../api/types';
import { heldElsewhere } from './holds';

const fx = buildFixtures(Date.parse('2026-10-08T12:00:00Z'));
const basePlan = fx.plans[0] as Plan;
const baseMigration = fx.migrations[0] as Migration;

function plan(id: string, name: string, source = 'src-a'): Plan {
  return { ...basePlan, id, name, source_provider_id: source };
}

function migration(planId: string, vmId: string, vmName: string, phase: Phase): Migration {
  return { ...baseMigration, id: `${planId}-${vmId}`, plan_id: planId, phase, vm: { ...baseMigration.vm, source_id: vmId, name: vmName } };
}

describe('heldElsewhere (SDD §5.4)', () => {
  const plans = [plan('p1', 'Mine'), plan('p2', 'Finance'), plan('p3', 'Finance'), plan('p4', 'Other cloud', 'src-b')];

  it('names the selected VMs that migrations of other plans with the same source hold, each holder once', () => {
    const migrations = [
      migration('p1', 'vm-1', 'web-01', 'ready'), // this plan
      migration('p2', 'vm-1', 'web-01', 'precopy'),
      migration('p3', 'vm-1', 'web-01', 'precopy'), // the same holder text: once
      migration('p2', 'vm-2', 'web-02', 'cancelled'), // released
      migration('p2', 'vm-3', 'web-03', 'rolled_back'), // released
      migration('p2', 'vm-4', 'web-04', 'finalized'), // released
      migration('p4', 'vm-5', 'web-05', 'ready'), // another source cloud
      migration('p2', 'vm-6', 'web-06', 'completed'), // not selected
    ];
    const held = heldElsewhere(['vm-1', 'vm-2', 'vm-3', 'vm-4', 'vm-5'], 'src-a', 'p1', plans, migrations);
    expect([...held.entries()]).toEqual([['vm-1', ['web-01 (plan "Finance", precopy)']]]);
  });

  it('counts a completed migration as holding until it is finalized', () => {
    const held = heldElsewhere(['vm-6'], 'src-a', undefined, plans, [migration('p2', 'vm-6', 'web-06', 'completed')]);
    expect([...held.values()]).toEqual([['web-06 (plan "Finance", completed)']]);
  });
});
