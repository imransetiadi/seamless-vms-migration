import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { renderWithApp } from '../test/utils';
import Plans from './Plans';

describe('Plans page', () => {
  it('lists plans with status in text', async () => {
    const { server } = renderWithApp(<Plans />, { route: '/plans', path: '/plans', token: 'viewer' });
    const table = await screen.findByRole('table', { name: /migration plans/i });
    expect(within(table).getAllByRole('row')).toHaveLength(server.plans.length + 1);
    expect(within(table).getByText('Paused')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /new plan/i })).toHaveAttribute('aria-disabled', 'true');
  });

  it('summarises validation errors and moves focus to the summary', async () => {
    const user = userEvent.setup();
    renderWithApp(<Plans />, { route: '/plans', path: '/plans', token: 'operator' });

    await user.click(await screen.findByRole('button', { name: /new plan/i }));
    const dialog = await screen.findByRole('dialog', { name: /new migration plan/i });
    await user.click(within(dialog).getByRole('button', { name: /^create plan/i }));

    const summary = await within(dialog).findByRole('alert');
    expect(summary).toHaveTextContent(/fix 4 problems/i);
    await waitFor(() => expect(summary).toHaveFocus());
    expect(within(dialog).getByLabelText(/^name/i)).toHaveAttribute('aria-invalid', 'true');
  });

  it('creates a draft plan with the selected VMs', async () => {
    const user = userEvent.setup();
    const { server } = renderWithApp(<Plans />, { route: '/plans', path: '/plans', token: 'operator' });

    await user.click(await screen.findByRole('button', { name: /new plan/i }));
    const dialog = await screen.findByRole('dialog', { name: /new migration plan/i });
    await user.type(within(dialog).getByLabelText(/^name/i), 'Wave test');
    await user.selectOptions(within(dialog).getByLabelText(/source provider/i), 'rhosp17-dc1');
    await user.selectOptions(within(dialog).getByLabelText(/destination/i), 'rhoso-prod');
    await user.click(await within(dialog).findByRole('checkbox', { name: 'Select web-01' }));
    await user.click(within(dialog).getByRole('checkbox', { name: 'Select web-02' }));
    await user.click(within(dialog).getByRole('button', { name: /create plan with 2 vms/i }));

    await waitFor(() => expect(server.plans.some((p) => p.name === 'Wave test')).toBe(true));
    const created = server.plans.find((p) => p.name === 'Wave test');
    expect(created?.vm_ids).toEqual(['os-0a11', 'os-0a12']);
    expect(created?.downtime_slo_s).toBe(300);
    expect(created?.status).toBe('draft');
  });
});
