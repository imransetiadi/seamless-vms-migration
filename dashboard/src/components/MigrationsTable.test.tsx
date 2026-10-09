import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import { buildFixtures } from '../api/mockData';
import type { Migration, Plan } from '../api/types';
import { MigrationsTable } from './MigrationsTable';

const NOW = Date.parse('2026-10-08T12:00:00Z');
const fx = buildFixtures(NOW);
const plan = fx.plans.find((p) => p.id === 'plan-4f2a9c1e') as Plan;
const migrations = fx.migrations.filter((m) => m.plan_id === plan.id);

function renderTable(list: Migration[] = migrations) {
  return render(
    <MemoryRouter>
      <MigrationsTable migrations={list} plan={plan} />
    </MemoryRouter>,
  );
}

function rows(): HTMLElement[] {
  return within(screen.getByRole('table')).getAllByRole('row').slice(1);
}

function rowNames(): string[] {
  return rows().map((row) => within(row).getAllByRole('rowheader')[0]?.textContent ?? '');
}

function rowFor(name: string): HTMLElement {
  const row = rows().find((r) => within(r).getAllByRole('rowheader')[0]?.textContent === name);
  if (!row) throw new Error(`row ${name} missing`);
  return row;
}

describe('MigrationsTable', () => {
  it('links every VM to its migration detail page', () => {
    renderTable();
    expect(rows()).toHaveLength(migrations.length);
    const link = within(rowFor('web-03')).getByRole('link', { name: 'web-03' });
    expect(link).toHaveAttribute('href', '/migrations/mig-3c1a0f9e23');
    expect(screen.getByRole('status')).toHaveTextContent(`Showing ${migrations.length} of ${migrations.length} migrations`);
  });

  it('sorts by phase in lifecycle order', async () => {
    const user = userEvent.setup();
    renderTable();
    const header = screen.getByRole('columnheader', { name: /^phase/i });
    await user.click(within(header).getByRole('button'));
    expect(header).toHaveAttribute('aria-sort', 'ascending');
    expect(rowNames()).toEqual([
      'mq-broker-02',
      'api-gw-02',
      'gpu-render-01',
      'ad-dc-01',
      'db-pg-01',
      'db-mysql-02',
      'app-billing-01',
      'web-03',
      'web-02',
      'web-01',
    ]);
  });

  it('sorts by estimated downtime in both directions', async () => {
    const user = userEvent.setup();
    renderTable();
    const expected = [...migrations]
      .sort((a, b) => (a.estimate?.downtime_s ?? Infinity) - (b.estimate?.downtime_s ?? Infinity))
      .map((m) => m.vm.name);
    const header = screen.getByRole('columnheader', { name: /est\. downtime/i });

    await user.click(within(header).getByRole('button'));
    expect(rowNames()).toEqual(expected);
    await user.click(within(header).getByRole('button'));
    expect(header).toHaveAttribute('aria-sort', 'descending');
    expect(rowNames()[0]).toBe(expected.filter((name) => migrations.find((m) => m.vm.name === name)?.estimate).at(-1));
  });

  it('filters by text, phase, strategy and wave', async () => {
    const user = userEvent.setup();
    renderTable();

    await user.type(screen.getByRole('searchbox', { name: /search migrations/i }), 'web');
    expect(rowNames().sort()).toEqual(['web-01', 'web-02', 'web-03']);
    await user.clear(screen.getByRole('searchbox', { name: /search migrations/i }));

    await user.selectOptions(screen.getByLabelText('Phase'), 'blocked');
    expect(rowNames()).toEqual(['gpu-render-01']);
    await user.selectOptions(screen.getByLabelText('Phase'), 'all');

    await user.selectOptions(screen.getByLabelText('Strategy'), 'cold');
    expect(rowNames().sort()).toEqual(['ad-dc-01', 'api-gw-02', 'gpu-render-01']);
    await user.selectOptions(screen.getByLabelText('Strategy'), 'all');

    await user.selectOptions(screen.getByLabelText('Wave'), 'wave-1');
    expect(rowNames().sort()).toEqual(['web-01', 'web-02', 'web-03']);
    expect(screen.getByRole('status')).toHaveTextContent(`Showing 3 of ${migrations.length} migrations`);
  });

  it('summarises findings by severity in words, not colour', () => {
    renderTable();
    expect(rowFor('gpu-render-01')).toHaveTextContent('1 blocker');
    const pg = migrations.find((m) => m.vm.name === 'db-pg-01') as Migration;
    const warnings = pg.findings.filter((f) => f.severity === 'warning').length;
    expect(warnings).toBeGreaterThan(0);
    expect(rowFor('db-pg-01')).toHaveTextContent(`${warnings} warning${warnings === 1 ? '' : 's'}`);
  });

  it('shows the estimate against the SLO with text', () => {
    renderTable();
    const billing = migrations.find((m) => m.vm.name === 'app-billing-01') as Migration;
    const cell = rowFor('app-billing-01');
    expect(cell).toHaveTextContent(billing.estimate?.meets_slo ? /within SLO/ : /over SLO/);
  });
});


describe('MigrationsTable without migrations', () => {
  it('shows what the page says about an empty plan and its action (SDD §16)', () => {
    render(
      <MemoryRouter>
        <MigrationsTable migrations={[]} plan={plan} emptyDescription="Validation creates them." emptyAction={<button type="button">Validate</button>} />
      </MemoryRouter>,
    );
    expect(screen.getByText(/no migrations in this plan yet/i)).toBeInTheDocument();
    expect(screen.getByText('Validation creates them.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Validate' })).toBeInTheDocument();
  });
});
