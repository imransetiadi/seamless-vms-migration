import { render, screen, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { buildFixtures } from '../api/mockData';
import type { Migration, Plan } from '../api/types';
import { MigrationsTable } from './MigrationsTable';

// counts the rows a render draws: each row draws one EstimateCell (the real one, so the table is unchanged);
// a plain counter, as the config's restoreMocks would reset a vi.fn between tests
const drawn = vi.hoisted(() => ({ rows: 0 }));
vi.mock('./EstimateCell', async (importOriginal) => {
  const original = await importOriginal<typeof import('./EstimateCell')>();
  return {
    ...original,
    EstimateCell: (props: Parameters<typeof original.EstimateCell>[0]) => {
      drawn.rows += 1;
      return original.EstimateCell(props);
    },
  };
});

const NOW = Date.parse('2026-10-08T12:00:00Z');
const fx = buildFixtures(NOW);
const plan = fx.plans.find((p) => p.id === 'plan-4f2a9c1e') as Plan;
const migrations = fx.migrations.filter((m) => m.plan_id === plan.id);

function table(list: Migration[], p: Plan = plan) {
  return (
    <MemoryRouter>
      <MigrationsTable migrations={list} plan={p} />
    </MemoryRouter>
  );
}

function rowFor(name: string): HTMLElement {
  const row = within(screen.getByRole('table'))
    .getAllByRole('row')
    .find((r) => within(r).queryAllByRole('rowheader')[0]?.textContent === name);
  if (!row) throw new Error(`row ${name} missing`);
  return row;
}

describe('MigrationsTable rendering', () => {
  beforeEach(() => {
    drawn.rows = 0;
  });

  it('redraws only the row of a migration a live event patched, not every row of the plan', () => {
    expect(migrations.length).toBeGreaterThan(2);
    const { rerender } = render(table(migrations));
    expect(drawn.rows).toBe(migrations.length);
    drawn.rows = 0;

    // what live.ts does with a migration.progress event: a new array, one new migration, the others as they were
    const target = migrations[1] as Migration;
    const patched = migrations.map((m) => (m.id === target.id ? { ...m, progress_pct: 42 } : m));
    rerender(table(patched));

    expect(drawn.rows).toBe(1);
  });

  it('still redraws every row whose wave name or override the plan changed', () => {
    const migration = migrations.find((m) => m.wave_id);
    if (!migration?.wave_id) throw new Error('fixture has no migration in a wave');
    const { rerender } = render(table(migrations));

    const renamed: Plan = {
      ...plan,
      waves: plan.waves.map((w) => (w.id === migration.wave_id ? { ...w, name: 'Renamed wave' } : w)),
      strategy_overrides: { ...plan.strategy_overrides, [migration.vm.source_id]: migration.strategy },
    };
    rerender(table(migrations, renamed));

    const row = rowFor(migration.vm.name);
    expect(within(row).getByText('Renamed wave')).toBeInTheDocument();
    expect(within(row).getByText('override')).toBeInTheDocument();
  });
});
