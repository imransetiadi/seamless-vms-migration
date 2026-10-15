import { cleanup, render, screen, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import { buildFixtures } from '../api/mockData';
import type { Migration, Phase, Plan } from '../api/types';
import { TONE_CLASSES } from '../lib/status';
import { waveMigrations } from '../lib/waves';
import { WavesBoard } from './WavesBoard';

const fx = buildFixtures(Date.parse('2026-10-08T12:00:00Z'));
// a waved plan whose first wave holds at least two VMs
const plan = fx.plans.find((p) => {
  const first = [...p.waves].sort((a, b) => a.order - b.order)[0];
  return first !== undefined && waveMigrations(first, fx.migrations, p.id).length >= 2;
}) as Plan;
const mine = fx.migrations.filter((m) => m.plan_id === plan.id);
const waves = [...plan.waves].sort((a, b) => a.order - b.order);

function board(p: Plan, migrations: Migration[] | null) {
  render(
    <MemoryRouter>
      <WavesBoard plan={p} migrations={migrations} />
    </MemoryRouter>,
  );
}

const card = (name: string) => screen.getByRole('heading', { name }).closest('li')!;

describe('WavesBoard (SDD §16)', () => {
  it('reads a wave whose migrations are all cancelled as Cancelled, with no count - never a green Complete', () => {
    const cancelled = mine.map((m) => ({ ...m, phase: 'cancelled' as Phase }));
    board({ ...plan, status: 'cancelled', derived_status: 'cancelled' }, cancelled);
    for (const wave of waves) {
      const members = waveMigrations(wave, cancelled, plan.id);
      const item = card(wave.name);
      expect(within(item).queryByText('Complete')).not.toBeInTheDocument();
      // the wave's badge, in the neutral tone, and each of its VMs'
      const badges = within(item).getAllByText('Cancelled');
      expect(badges).toHaveLength(members.length + 1);
      expect(badges[0]!.parentElement).toHaveClass(TONE_CLASSES.neutral.badge);
      expect(within(item).queryByText(/^\d+\/\d+$/)).not.toBeInTheDocument();
      expect(within(item).queryByRole('progressbar')).not.toBeInTheDocument();
    }
  });

  it("reads every wave of a cancelled plan as Cancelled - one with no migration, and while its migrations can't be loaded", () => {
    const spare = { id: 'wave-9', name: 'Spare', order: 9, vm_ids: [], depends_on: [], max_parallel: 1 };
    const cancelledPlan: Plan = { ...plan, status: 'cancelled', derived_status: 'cancelled', waves: [...plan.waves, spare] };
    board(cancelledPlan, mine.map((m) => ({ ...m, phase: 'cancelled' as Phase })));
    expect(within(card('Spare')).getByText('Cancelled')).toBeInTheDocument();
    expect(within(card('Spare')).queryByText('0/0')).not.toBeInTheDocument();
    cleanup();
    board(cancelledPlan, null);
    for (const wave of [...waves, spare]) {
      const item = card(wave.name);
      expect(within(item).getByText('Cancelled')).toBeInTheDocument();
      expect(within(item).queryByText('Unknown')).not.toBeInTheDocument();
      expect(within(item).queryByText('Progress unknown')).not.toBeInTheDocument();
    }
  });

  it('keeps a wave with no migration yet - waves planned before validation - Planned, never Cancelled', () => {
    board({ ...plan, status: 'draft', derived_status: 'draft' }, []);
    for (const wave of waves) {
      const item = card(wave.name);
      expect(within(item).getByText('Planned')).toBeInTheDocument();
      expect(within(item).queryByText('Cancelled')).not.toBeInTheDocument();
      expect(within(item).getByText('0/0')).toBeInTheDocument();
    }
  });

  it('reads a wave whose migrations were all cancelled - its VMs taken out of a running plan - as Cancelled, and the others as before', () => {
    const first = waveMigrations(waves[0]!, mine, plan.id);
    board(
      plan,
      mine.map((m) => (first.some((f) => f.id === m.id) ? { ...m, phase: 'cancelled' as Phase } : m)),
    );
    expect(within(card(waves[0]!.name)).getAllByText('Cancelled')).toHaveLength(first.length + 1);
    expect(within(card(waves[0]!.name)).queryByRole('progressbar')).not.toBeInTheDocument();
    const second = card(waves[1]!.name);
    expect(within(second).getByText('Active')).toBeInTheDocument();
    const members = waveMigrations(waves[1]!, mine, plan.id);
    const done = members.filter((m) => m.phase === 'completed' || m.phase === 'finalized').length;
    expect(within(second).getByText(`${done}/${members.length}`)).toBeInTheDocument();
  });

  it("counts a wave's VMs done as the plan's: completed and finalized of those not cancelled", () => {
    const first = waveMigrations(waves[0]!, mine, plan.id);
    const dropped = first[0]!.id;
    const migrations = mine.map((m) =>
      first.some((f) => f.id === m.id) ? { ...m, phase: (m.id === dropped ? 'cancelled' : 'completed') as Phase } : m,
    );
    board(plan, migrations);
    const item = card(waves[0]!.name);
    expect(within(item).getByText(`${first.length - 1}/${first.length - 1}`)).toBeInTheDocument();
    expect(within(item).getByText('Complete')).toBeInTheDocument();
  });

  it('reads a rolled-back wave as 0 done, as a plan that migrated nothing reads (SDD §16)', () => {
    const first = waveMigrations(waves[0]!, mine, plan.id);
    const dropped = first[0]!.id;
    const migrations = mine.map((m) =>
      first.some((f) => f.id === m.id) ? { ...m, phase: (m.id === dropped ? 'cancelled' : 'rolled_back') as Phase } : m,
    );
    board(plan, migrations);
    const item = card(waves[0]!.name);
    // the wave's badge and each rolled-back VM's
    expect(within(item).getAllByText('Rolled back')).toHaveLength(first.length);
    expect(within(item).getByText(`0/${first.length - 1}`)).toBeInTheDocument();
    expect(within(item).getByRole('progressbar', { name: `${waves[0]!.name}: VMs done` })).toHaveAttribute('aria-valuenow', '0');
  });
});
