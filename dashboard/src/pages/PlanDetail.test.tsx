import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import type { Plan } from '../api/types';
import { createTestClient, createTestServer, renderWithApp } from '../test/utils';
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
    const user = userEvent.setup({ delay: null });
    renderPlan('plan-0e9f6a17');

    const start = await actionButton(/^start/i);
    expect(start).toHaveAttribute('aria-disabled', 'true');
    expect(start).toHaveAccessibleDescription(/validate the plan first/i);

    await user.click(await actionButton(/validate/i));
    expect(await screen.findByText(/validation finished/i)).toBeInTheDocument();
    expect(await actionButton(/^start/i)).not.toHaveAttribute('aria-disabled');
  });

  it('says that validation creates the migrations of a new plan, with Validate there too (SDD §16)', async () => {
    const server = createTestServer();
    const taken = new Set(server.migrations.map((m) => m.vm.source_id));
    const inventories = (server as unknown as { inventories: Record<string, Array<{ source_id: string }>> }).inventories;
    const free = inventories['rhosp17-dc1']!.filter((v) => !taken.has(v.source_id)).slice(0, 2).map((v) => v.source_id);
    const plan = await createTestClient(server, 'operator').post<Plan>('/plans', {
      name: 'Fresh wave',
      source_provider_id: 'rhosp17-dc1',
      destination_provider_id: 'rhoso-prod',
      vm_ids: free,
    });
    const user = userEvent.setup({ delay: null });
    renderWithApp(<PlanDetail />, { route: `/plans/${plan.id}`, path: '/plans/:planId', token: 'operator', server });

    const panel = await screen.findByRole('region', { name: /^migrations$/i });
    expect(await within(panel).findByText(/no migrations in this plan yet/i)).toBeInTheDocument();
    expect(panel).toHaveTextContent(/validation creates one migration per vm and runs the pre-flight checks/i);
    // pre-flight has not run: the findings must not say it passed (SDD §16)
    const findings = screen.getByRole('region', { name: /^findings$/i });
    expect(findings).toHaveTextContent(/no findings yet — pre-flight runs when the plan is validated/i);
    expect(findings).not.toHaveTextContent(/pre-flight passed/i);
    await user.click(within(panel).getByRole('button', { name: /^validate$/i }));
    expect(await within(panel).findByRole('table', { name: /migrations in fresh wave/i })).toBeInTheDocument();
    expect(server.migrations.filter((m) => m.plan_id === plan.id)).toHaveLength(2);
    await waitFor(() => expect(findings).not.toHaveTextContent(/pre-flight runs when the plan is validated/i));
  });

  it('says pre-flight passed only when every VM of the plan was checked and the plan is not a draft (SDD §16)', async () => {
    const server = createTestServer();
    const plan = server.plans.find((p) => p.id === 'plan-c81d44a0')!;
    const mine = server.migrations.filter((m) => m.plan_id === plan.id);
    plan.vm_ids = mine.map((m) => m.vm.source_id);
    for (const m of mine) {
      m.findings = [];
      m.phase = 'ready';
      m.phase_history = [
        { from_phase: null, to_phase: 'pending', at: '2026-10-08T10:00:00Z', reason: 'migration created', actor: 'sari' },
        { from_phase: 'pending', to_phase: 'validating', at: '2026-10-08T10:00:01Z', reason: 'plan validation started', actor: 'sari' },
        { from_phase: 'validating', to_phase: 'ready', at: '2026-10-08T10:00:02Z', reason: 'pre-flight passed', actor: 'sari' },
      ];
    }
    const findingsText = async () => {
      const view = renderWithApp(<PlanDetail />, { route: `/plans/${plan.id}`, path: '/plans/:planId', token: 'viewer', server });
      const region = await screen.findByRole('region', { name: /^findings$/i });
      await waitFor(() => expect(region).toHaveTextContent(/no findings/i));
      const text = region.textContent ?? '';
      view.unmount();
      return text;
    };

    plan.status = 'validated';
    expect(await findingsText()).toMatch(/no findings — pre-flight passed/i);
    // a VM added after the validation has no migration yet: nothing checked it
    const taken = new Set(server.migrations.map((m) => m.vm.source_id));
    const inventories = (server as unknown as { inventories: Record<string, Array<{ source_id: string }>> }).inventories;
    const added = inventories[plan.source_provider_id]!.find((v) => !taken.has(v.source_id))!.source_id;
    plan.vm_ids = [...plan.vm_ids, added];
    expect(await findingsText()).toMatch(/pre-flight runs when the plan is validated/i);
    // an edited plan is a draft again: it is validated again before it starts
    plan.vm_ids = plan.vm_ids.filter((id) => id !== added);
    plan.status = 'draft';
    expect(await findingsText()).toMatch(/pre-flight runs when the plan is validated/i);
  });

  it('says which plan holds a VM when validation is refused (SDD §5.4)', async () => {
    const server = createTestServer();
    const first = server.plans.find((p) => p.id === 'plan-4f2a9c1e')!;
    const held = server.migrations.find((m) => m.plan_id === first.id && !['pending', 'cancelled', 'finalized', 'rolled_back'].includes(m.phase))!;
    const second = await createTestClient(server, 'operator').post<Plan>('/plans', {
      name: 'Second wave',
      source_provider_id: first.source_provider_id,
      destination_provider_id: first.destination_provider_id,
      vm_ids: [held.vm.source_id],
    });
    const user = userEvent.setup({ delay: null });
    renderWithApp(<PlanDetail />, { route: `/plans/${second.id}`, path: '/plans/:planId', token: 'operator', server });

    await user.click(await actionButton(/validate/i));
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/the plan action failed/i);
    expect(alert).toHaveTextContent(`${held.vm.name} (plan "${first.name}", ${held.phase})`);
  });

  it('asks before Validate clears approvals and cutover requests, and says how many (SDD §5.4, §16)', async () => {
    const server = createTestServer();
    const plan = server.plans.find((p) => p.id === 'plan-c81d44a0')!;
    const mine = server.migrations.filter((m) => m.plan_id === plan.id);
    const first = mine[0]!;
    for (const m of mine.slice(0, 2)) {
      m.phase = 'ready';
      m.approvals = [{ actor: 'sari', at: '2026-10-08T11:00:00Z', comment: null }];
    }
    first.cutover_requested = true;
    const user = userEvent.setup({ delay: null });
    renderWithApp(<PlanDetail />, { route: `/plans/${plan.id}`, path: '/plans/:planId', token: 'operator', server });
    await screen.findByRole('table', { name: /migrations/i });

    await user.click(await actionButton(/validate/i));
    let dialog = await screen.findByRole('alertdialog', { name: /validate this plan again/i });
    expect(dialog).toHaveTextContent(/clears 2 approvals and 1 cutover request/i);
    await user.click(within(dialog).getByRole('button', { name: /^cancel$/i }));
    expect(first.approvals).toHaveLength(1);

    await user.click(await actionButton(/validate/i));
    dialog = await screen.findByRole('alertdialog', { name: /validate this plan again/i });
    await user.click(within(dialog).getByRole('button', { name: /^validate and clear$/i }));
    expect(await screen.findByText(/validation finished/i)).toBeInTheDocument();
    expect(first.approvals).toEqual([]);
    expect(first.cutover_requested).toBe(false);
  });

  it('counts the approvals on the server when Validate is clicked, not a list loaded before them (SDD §5.4)', async () => {
    const server = createTestServer();
    const plan = server.plans.find((p) => p.id === 'plan-c81d44a0')!;
    const m = server.migrations.find((x) => x.plan_id === plan.id)!;
    m.phase = 'ready';
    const user = userEvent.setup({ delay: null });
    renderWithApp(<PlanDetail />, { route: `/plans/${plan.id}`, path: '/plans/:planId', token: 'operator', server });
    await screen.findByRole('table', { name: /migrations/i });
    // an approver approves meanwhile (another tab or person): the list on screen does not show it yet
    m.approvals = [{ actor: 'sari', at: '2026-10-08T11:00:00Z', comment: null }];

    await user.click(await actionButton(/validate/i));
    const dialog = await screen.findByRole('alertdialog', { name: /validate this plan again/i });
    expect(dialog).toHaveTextContent(/clears 1 approval/i);
    expect(m.approvals).toHaveLength(1);
  });

  it('shows its KPIs as unknown, not zero, when the statistics cannot be loaded (SDD §16)', async () => {
    const server = createTestServer();
    const plan = server.plans.find((p) => p.id === 'plan-c81d44a0')!;
    const handle = server.handle.bind(server);
    server.handle = (method, path, query, body, token) =>
      method === 'GET' && path === '/stats'
        ? { status: 400, body: { error: { code: 'bad_request', message: 'refused for the test' } } }
        : handle(method, path, query, body, token);
    renderWithApp(<PlanDetail />, { route: `/plans/${plan.id}`, path: '/plans/:planId', token: 'viewer', server });
    expect(await screen.findByText(/statistics are unavailable/i)).toBeInTheDocument();
    const metrics = screen.getByRole('region', { name: /plan metrics/i });
    for (const label of ['In progress', 'Completed', 'Failed']) {
      expect(within(metrics).getByRole('group', { name: label })).toHaveTextContent(`${label}—`);
    }
    // the count still comes from the plan's migrations, which did load
    const count = server.migrations.filter((m) => m.plan_id === plan.id).length;
    expect(count).toBeGreaterThan(0);
    await waitFor(() => expect(within(metrics).getByRole('group', { name: 'Migrations' })).toHaveTextContent(`Migrations${count}`));
  });

  it('asks anyway when the migrations cannot be counted at the click (SDD §5.4)', async () => {
    const server = createTestServer();
    const plan = server.plans.find((p) => p.id === 'plan-c81d44a0')!;
    const handle = server.handle.bind(server);
    let failing = false;
    server.handle = (method, path, query, body, token) =>
      failing && method === 'GET' && path === '/migrations'
        ? { status: 503, body: { error: { code: 'unavailable', message: 'database unavailable' } } }
        : handle(method, path, query, body, token);
    const user = userEvent.setup({ delay: null });
    renderWithApp(<PlanDetail />, { route: `/plans/${plan.id}`, path: '/plans/:planId', token: 'operator', server });
    await screen.findByRole('table', { name: /migrations/i });
    failing = true;

    await user.click(await actionButton(/validate/i));
    // a 5xx is retried twice with backoff before the refetch reports the error
    const dialog = await screen.findByRole('alertdialog', { name: /validate this plan again/i }, { timeout: 8000 });
    expect(dialog).toHaveTextContent(/clears any approvals and cutover requests/i);
  }, 15_000);

  it('starts a failed plan again, saying that pre-staging failed and is retried (SDD §8, §16)', async () => {
    const user = userEvent.setup({ delay: null });
    const { server } = renderPlan('plan-95a7e3f1');
    const start = await actionButton(/^start again$/i);
    expect(start).not.toHaveAttribute('aria-disabled');

    await user.click(start);
    const dialog = await screen.findByRole('alertdialog', { name: /start this plan again/i });
    expect(dialog).toHaveTextContent(/pre-staging failed/i);
    expect(dialog).toHaveTextContent(/failed migrations stay failed until you retry or roll them back/i);
    await user.click(within(dialog).getByRole('button', { name: /^start again$/i }));
    await waitFor(() => expect(server.plans.find((p) => p.id === 'plan-95a7e3f1')?.status).toBe('running'));
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
    const user = userEvent.setup({ delay: null });
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
    const user = userEvent.setup({ delay: null });
    const { server } = renderPlan('plan-c81d44a0');
    await user.click(await actionButton(/^edit plan$/i));
    const dialog = await screen.findByRole('dialog', { name: /edit plan/i });
    expect(within(dialog).getByLabelText(/^name/i)).toHaveValue('Shared Ceph handover — analytics');
    expect(within(dialog).getByText(/saving returns the plan to draft/i)).toBeInTheDocument();
    expect(within(dialog).getByRole('checkbox', { name: /hand volumes over without copying/i })).toBeChecked();
    expect(within(dialog).getByLabelText(/^source provider/i)).toBeDisabled();
    // the plan's own migrations hold its VMs for this plan, not against it (SDD §5.4)
    expect(within(dialog).queryByRole('status', { name: /another plan holds/i })).not.toBeInTheDocument();

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

  it('shows an operator the approval policy read-only, with the reason (SDD §12, §16)', async () => {
    const user = userEvent.setup({ delay: null });
    renderPlan('plan-c81d44a0', 'operator');
    await user.click(await actionButton(/^edit plan$/i));
    const dialog = await screen.findByRole('dialog', { name: /edit plan/i });
    for (const name of [/require approval/i, /automatic cutover/i]) {
      const box = within(dialog).getByRole('checkbox', { name });
      expect(box).toBeDisabled();
      expect(box).toHaveAccessibleDescription(/changing the approval policy requires the approver role/i);
    }
    for (const label of [/cutover window start/i, /cutover window end/i]) {
      const field = within(dialog).getByLabelText(label);
      expect(field).toBeDisabled();
      expect(field).toHaveAccessibleDescription(/changing the cutover window requires the approver role/i);
    }
  });

  it('lets an approver change the approval policy; only that field is sent', async () => {
    const user = userEvent.setup({ delay: null });
    const { server } = renderPlan('plan-c81d44a0', 'approver');
    const before = server.plans.find((p) => p.id === 'plan-c81d44a0')?.auto_cutover;
    await user.click(await actionButton(/^edit plan$/i));
    const dialog = await screen.findByRole('dialog', { name: /edit plan/i });
    const auto = within(dialog).getByRole('checkbox', { name: /automatic cutover/i });
    await waitFor(() => expect(auto).toBeEnabled());
    expect(auto).not.toHaveAccessibleDescription(/requires the approver role/i);
    // the window keeps its own hint
    expect(within(dialog).getByLabelText(/cutover window start/i)).toHaveAccessibleDescription(/local time; leave empty for any time/i);
    await user.click(auto);
    await user.click(within(dialog).getByRole('button', { name: /^save changes$/i }));

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(server.plans.find((p) => p.id === 'plan-c81d44a0')?.auto_cutover).toBe(!before);
    expect(server.events.filter((e) => e.kind === 'plan.updated').at(-1)?.data).toEqual({ fields: ['auto_cutover'] });
  });

  it('edits keep-warm and pre-staging, prefilled; a resource the form does not show is kept (SDD §16)', async () => {
    const user = userEvent.setup({ delay: null });
    const server = createTestServer();
    const plan = server.plans.find((p) => p.id === 'plan-c81d44a0')!;
    plan.keep_warm_interval_s = 600;
    plan.prestage_resources = ['networks', 'subnets', 'flavors'];
    renderWithApp(<PlanDetail />, { route: '/plans/plan-c81d44a0', path: '/plans/:planId', token: 'operator', server });

    await user.click(await actionButton(/^edit plan$/i));
    let dialog = await screen.findByRole('dialog', { name: /edit plan/i });
    await user.click(within(dialog).getByText(/^advanced:/i));
    const keepWarm = within(dialog).getByLabelText(/^keep-warm interval/i);
    expect(keepWarm).toHaveValue(10);
    const prestage = within(dialog).getByRole('group', { name: /pre-staged at the destination/i });
    expect(within(prestage).getByRole('checkbox', { name: /^networks/i })).toBeChecked();
    expect(within(prestage).getByRole('checkbox', { name: /^routers/i })).not.toBeChecked();
    expect(prestage).toHaveTextContent(/also pre-staged: flavors/i);
    await user.clear(keepWarm);
    await user.type(keepWarm, '20');
    await user.click(within(dialog).getByRole('button', { name: /^save changes$/i }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(plan.keep_warm_interval_s).toBe(1200);
    expect(plan.prestage_resources).toEqual(['networks', 'subnets', 'flavors']);
    expect(server.events.filter((e) => e.kind === 'plan.updated').at(-1)?.data).toEqual({ fields: ['keep_warm_interval_s'] });

    // turning a default on keeps the canonical order and the extra entry
    await user.click(await actionButton(/^edit plan$/i));
    dialog = await screen.findByRole('dialog', { name: /edit plan/i });
    await user.click(within(dialog).getByText(/^advanced:/i));
    await user.click(within(within(dialog).getByRole('group', { name: /pre-staged at the destination/i })).getByRole('checkbox', { name: /^routers/i }));
    await user.click(within(dialog).getByRole('button', { name: /^save changes$/i }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(plan.prestage_resources).toEqual(['networks', 'subnets', 'routers', 'flavors']);
    expect(server.events.filter((e) => e.kind === 'plan.updated').at(-1)?.data).toEqual({ fields: ['prestage_resources'] });
  });

  it('shows and removes a per-VM strategy override when editing; only the overrides are sent (SDD §16)', async () => {
    const user = userEvent.setup({ delay: null });
    const server = createTestServer();
    const plan = server.plans.find((p) => p.id === 'plan-c81d44a0')!;
    const vmId = plan.vm_ids[0]!;
    const inventories = (server as unknown as { inventories: Record<string, Array<{ source_id: string; name: string }>> }).inventories;
    const vmName = inventories[plan.source_provider_id]!.find((v) => v.source_id === vmId)!.name;
    plan.strategy_overrides = { [vmId]: 'warm' };
    renderWithApp(<PlanDetail />, { route: '/plans/plan-c81d44a0', path: '/plans/:planId', token: 'operator', server });

    await user.click(await actionButton(/^edit plan$/i));
    const dialog = await screen.findByRole('dialog', { name: /edit plan/i });
    const overrides = within(dialog).getByRole('group', { name: /per-vm strategy/i });
    expect(await within(overrides).findByLabelText(new RegExp(`^strategy for ${vmName}$`, 'i'))).toHaveValue('warm');
    await user.click(within(overrides).getByRole('button', { name: new RegExp(`^remove the override for ${vmName}$`, 'i') }));
    await user.click(within(dialog).getByRole('button', { name: /^save changes$/i }));

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(plan.strategy_overrides).toEqual({});
    expect(server.events.filter((e) => e.kind === 'plan.updated').at(-1)?.data).toEqual({ fields: ['strategy_overrides'] });
  });

  it('edits the verification settings, prefilled from the plan; only verification is sent', async () => {
    const user = userEvent.setup({ delay: null });
    const server = createTestServer();
    const plan = server.plans.find((p) => p.id === 'plan-c81d44a0')!;
    // a pattern is a regular expression: its spaces are part of it and survive an unrelated edit
    plan.verification.console_success_patterns = ['login: ', 'Reached target .*Multi-User'];
    renderWithApp(<PlanDetail />, { route: '/plans/plan-c81d44a0', path: '/plans/:planId', token: 'operator', server });
    await user.click(await actionButton(/^edit plan$/i));
    const dialog = await screen.findByRole('dialog', { name: /edit plan/i });
    await user.click(within(dialog).getByText(/^advanced:/i));
    expect(within(dialog).getByLabelText(/^probe address/i)).toHaveValue(plan.verification.probe_address);
    const timeout = within(dialog).getByLabelText(/^verification timeout/i);
    expect(timeout).toHaveValue(plan.verification.timeout_s / 60);
    await user.clear(timeout);
    await user.type(timeout, '20');
    await user.click(within(dialog).getByRole('button', { name: /^save changes$/i }));

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(plan.verification.timeout_s).toBe(1200);
    expect(plan.verification.console_success_patterns).toEqual(['login: ', 'Reached target .*Multi-User']);
    expect(server.events.filter((e) => e.kind === 'plan.updated').at(-1)?.data).toEqual({ fields: ['verification'] });
  });

  it('explains why a plan cannot be edited', async () => {
    renderPlan('plan-4f2a9c1e');
    expect(await actionButton(/^edit plan$/i)).toHaveAttribute('aria-disabled', 'true');
  });
});
