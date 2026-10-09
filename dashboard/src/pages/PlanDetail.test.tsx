import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { createTestServer, renderWithApp } from '../test/utils';
import PlanDetail from './PlanDetail';

function renderPlan(planId: string, token = 'operator') {
  return renderWithApp(<PlanDetail />, { route: `/plans/${planId}`, path: '/plans/:planId', token });
}

async function actionButton(name: RegExp) {
  const actions = await screen.findByRole('group', { name: /plan actions/i });
  return within(actions).getByRole('button', { name });
}

describe('PlanDetail', () => {
  it('shows the settings summary, waves board and migrations table', async () => {
    renderPlan('plan-4f2a9c1e');

    expect(await screen.findByRole('heading', { level: 1, name: 'DC1 → RHOSO production rollout' })).toBeInTheDocument();
    const settings = screen.getByRole('region', { name: /settings/i });
    expect(settings).toHaveTextContent('5m 00s');
    expect(settings).toHaveTextContent(/require approval/i);
    const waves = screen.getByRole('region', { name: /waves/i });
    expect(within(waves).getByText('Pilot — stateless web')).toBeInTheDocument();
    expect(within(waves).getByText('Middleware')).toBeInTheDocument();
    const table = await screen.findByRole('table', { name: /migrations/i });
    expect(within(table).getAllByRole('row')).toHaveLength(11);
  });

  it('formats the estimator overrides and marks link_bps as ignored', async () => {
    const server = createTestServer();
    const plan = server.plans.find((p) => p.id === 'plan-4f2a9c1e');
    if (!plan) throw new Error('fixture plan missing');
    plan.estimator_overrides = { scan_bps: 400 * 2 ** 20, parallel_disks: 2, link_bps: 10 * 2 ** 20 };
    renderWithApp(<PlanDetail />, { route: '/plans/plan-4f2a9c1e', path: '/plans/:planId', token: 'operator', server });
    const settings = await screen.findByRole('region', { name: /settings/i });
    await within(settings).findByText(/scan_bps=400 MiB\/s/);
    expect(settings).toHaveTextContent(/parallel_disks=2/);
    expect(settings).toHaveTextContent(/link_bps=10.0 MiB\/s \(ignored: the plan link bandwidth applies\)/);
  });

  it('enables only Pause for a running plan', async () => {
    renderPlan('plan-4f2a9c1e');

    const pause = await actionButton(/pause/i);
    expect(pause).not.toHaveAttribute('aria-disabled');
    expect(await actionButton(/^start|resume/i)).toHaveAttribute('aria-disabled', 'true');
    expect(await actionButton(/validate/i)).toHaveAttribute('aria-disabled', 'true');
    expect(await actionButton(/auto-plan waves/i)).toHaveAttribute('aria-disabled', 'true');
  });

  it('validates a draft plan and then allows starting it', async () => {
    const user = userEvent.setup();
    renderPlan('plan-0e9f6a17');

    const start = await actionButton(/^start/i);
    expect(start).toHaveAttribute('aria-disabled', 'true');
    expect(start).toHaveAccessibleDescription(/validate the plan first/i);

    await user.click(await actionButton(/validate/i));
    expect(await screen.findByText(/validation finished/i)).toBeInTheDocument();
    expect(await actionButton(/^start/i)).not.toHaveAttribute('aria-disabled');
  });

  it('keeps every plan action disabled for viewers, with the reason', async () => {
    renderPlan('plan-c81d44a0', 'viewer');

    for (const name of [/validate/i, /auto-plan waves/i, /^start/i, /pause/i]) {
      const button = await actionButton(name);
      expect(button).toHaveAttribute('aria-disabled', 'true');
      expect(button).toHaveAccessibleDescription(/operator role/i);
    }
  });

  it('pauses a running plan after confirmation', async () => {
    const user = userEvent.setup();
    renderPlan('plan-4f2a9c1e');

    await user.click(await actionButton(/pause/i));
    const dialog = await screen.findByRole('alertdialog', { name: /pause/i });
    await user.click(within(dialog).getByRole('button', { name: /pause plan/i }));
    expect(await screen.findAllByText('Paused')).not.toHaveLength(0);
    // gpu-render-01 is blocked, and POST /start answers 409 while any migration is blocked (SDD §12).
    const resume = await actionButton(/resume/i);
    expect(resume).toHaveAttribute('aria-disabled', 'true');
    expect(resume).toHaveAccessibleDescription(/1 migration is blocked/i);
    expect(await actionButton(/pause/i)).toHaveAttribute('aria-disabled', 'true');
  });

  it('edits a validated plan: prefilled, only the changed field is sent, the plan returns to draft', async () => {
    const user = userEvent.setup();
    const { server } = renderPlan('plan-c81d44a0');
    await user.click(await actionButton(/^edit plan$/i));
    const dialog = await screen.findByRole('dialog', { name: /edit plan/i });
    expect(within(dialog).getByLabelText(/^name/i)).toHaveValue('Shared Ceph handover — analytics');
    expect(within(dialog).getByText(/saving returns the plan to draft/i)).toBeInTheDocument();
    expect(within(dialog).getByRole('checkbox', { name: /hand volumes over without copying/i })).toBeChecked();
    expect(within(dialog).getByLabelText(/^source provider/i)).toBeDisabled();

    const slo = within(dialog).getByLabelText(/downtime slo/i);
    await user.clear(slo);
    await user.type(slo, '15');
    await user.click(within(dialog).getByRole('button', { name: /^save changes$/i }));

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    const plan = server.plans.find((p) => p.id === 'plan-c81d44a0');
    expect(plan?.downtime_slo_s).toBe(900);
    expect(plan?.status).toBe('draft');
    expect(server.events.filter((e) => e.kind === 'plan.updated').at(-1)?.data).toEqual({ fields: ['downtime_slo_s'] });
  });

  it('explains why a plan cannot be edited', async () => {
    renderPlan('plan-4f2a9c1e');
    expect(await actionButton(/^edit plan$/i)).toHaveAttribute('aria-disabled', 'true');
  });
});
