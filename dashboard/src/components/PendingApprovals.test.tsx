import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import { buildFixtures } from '../api/mockData';
import type { Migration } from '../api/types';
import { TEST_NOW } from '../test/utils';
import { PendingApprovals } from './PendingApprovals';

/** The fixtures' inbox (2 cutover approvals, 1 advisor decision) plus `extra` more migrations at the cutover gate. */
function renderInbox(extra: number) {
  const fx = buildFixtures(TEST_NOW);
  const gate = fx.migrations.find((m) => m.vm.name === 'app-billing-01')!;
  const clones: Migration[] = Array.from({ length: extra }, (_, i) => {
    const name = `bulk-${String(i).padStart(2, '0')}`;
    return { ...gate, id: `mig-bulk-${i}`, vm: { ...gate.vm, id: `vm-bulk-${i}`, name } };
  });
  const plans = new Map(fx.plans.map((p) => [p.id, p]));
  render(
    <MemoryRouter>
      <PendingApprovals migrations={[...fx.migrations, ...clones]} plans={plans} plansUnknown={false} />
    </MemoryRouter>,
  );
  return screen.getByRole('region', { name: 'Pending approvals' });
}

const rows = (inbox: HTMLElement) => within(inbox).queryAllByRole('listitem');

describe('PendingApprovals (SDD §16, ADV-20)', () => {
  it('shows the first eight rows, the rest behind Show all, folded back by Show fewer', async () => {
    const user = userEvent.setup({ delay: null });
    const inbox = renderInbox(12);
    expect(rows(inbox)).toHaveLength(8);
    // cutover approvals first, by name: the advisor decision is among the rest
    expect(within(inbox).queryByRole('listitem', { name: 'analytics-node-2' })).not.toBeInTheDocument();
    const more = within(inbox).getByRole('button', { name: 'Show all (15)' });
    expect(more).toHaveAttribute('aria-expanded', 'false');

    await user.click(more);
    expect(rows(inbox)).toHaveLength(15);
    expect(within(inbox).getByRole('listitem', { name: 'analytics-node-2' })).toBeInTheDocument();
    const fewer = within(inbox).getByRole('button', { name: 'Show fewer' });
    expect(fewer).toHaveAttribute('aria-expanded', 'true');

    await user.click(fewer);
    expect(rows(inbox)).toHaveLength(8);
    expect(within(inbox).getByRole('button', { name: 'Show all (15)' })).toBeInTheDocument();
  });

  it('caps the chosen kind, and choosing another kind folds the list back', async () => {
    const user = userEvent.setup({ delay: null });
    const inbox = renderInbox(12);
    await user.click(within(inbox).getByRole('button', { name: 'Show all (15)' }));
    expect(rows(inbox)).toHaveLength(15);

    await user.click(within(inbox).getByRole('radio', { name: 'Cutover approval (14)' }));
    expect(rows(inbox)).toHaveLength(8);
    expect(within(inbox).getByRole('button', { name: 'Show all (14)' })).toHaveAttribute('aria-expanded', 'false');

    await user.click(within(inbox).getByRole('radio', { name: 'Advisor decision (1)' }));
    expect(rows(inbox).map((li) => li.getAttribute('aria-label'))).toEqual(['analytics-node-2']);
    expect(within(inbox).queryByRole('button', { name: /show all|show fewer/i })).not.toBeInTheDocument();
  });

  it('offers no Show all when every row fits', () => {
    const inbox = renderInbox(5);
    expect(rows(inbox)).toHaveLength(8);
    expect(within(inbox).queryByRole('button', { name: /show all|show fewer/i })).not.toBeInTheDocument();
  });
});
