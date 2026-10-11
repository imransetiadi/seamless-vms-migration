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
    // the plan's approval is named by its kind (SDD §16, ADV-20)
    expect(settings).toHaveTextContent(/cutover approval/i);
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
    // Start is hidden once the plan runs (ADV-19)
    expect(within(screen.getByRole('group', { name: /plan actions/i })).queryByRole('button', { name: /^start|^resume/i })).not.toBeInTheDocument();
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

  it('announces the validation report from a live region already on the page (SDD §16)', async () => {
    const user = userEvent.setup({ delay: null });
    renderPlan('plan-0e9f6a17');
    const validate = await actionButton(/^validate$/i);
    const regions = screen.getAllByRole('status');

    await user.click(validate);
    const report = await screen.findByText(/validation finished/i);
    // a live region inserted together with its text is not reliably read
    expect(regions).toContain(report.closest('[role="status"]'));
  });

  it('announces a saved plan edit (SDD §16)', async () => {
    const user = userEvent.setup({ delay: null });
    renderPlan('plan-c81d44a0');
    await user.click(await actionButton(/^edit plan$/i));
    const dialog = await screen.findByRole('dialog', { name: /edit plan/i });
    const regions = screen.getAllByRole('status');
    const name = within(dialog).getByLabelText(/^name/i);
    await user.clear(name);
    await user.type(name, 'Analytics, renamed');
    await user.click(within(dialog).getByRole('button', { name: /^save changes$/i }));

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    const announced = regions.find((r) => /plan saved/i.test(r.textContent ?? ''));
    expect(announced).toHaveTextContent('Plan saved. It is a draft again: validate it before starting.');
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
    // the empty table's Validate is gone with it: focus moves to the plan's Validate, never to the page (SDD §16)
    expect(await actionButton(/^validate$/i)).toHaveFocus();
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
    expect(alert).toHaveTextContent(`${held.vm.name} (plan "${first.name}", ${first.id}, ${held.phase})`);
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

  it('says how many completed migrations its downtime figures leave out (SDD §5.5, ADV-16)', async () => {
    const server = createTestServer();
    const m = server.migrations.find((x) => x.id === 'mig-3c1a0f9e21')!;
    m.data_issues = ['DOWNTIME_BELOW_FINAL_SYNC: 6 s of downtime around a 258 s final sync'];
    renderWithApp(<PlanDetail />, { route: `/plans/${m.plan_id}`, path: '/plans/:planId', token: 'viewer', server });
    const metrics = await screen.findByRole('region', { name: /plan metrics/i });
    await waitFor(() => expect(within(metrics).getByRole('group', { name: 'Avg downtime' })).toHaveTextContent('1 left out: data inconsistent'));
  });

  it('says how many database VMs have no application probe, with each one\'s suggestion (SDD §16, ADV-18)', async () => {
    const user = userEvent.setup({ delay: null });
    const server = createTestServer();
    const plan = server.plans.find((p) => p.id === 'plan-c81d44a0')!;
    expect(plan.status).toBe('validated');
    const db = server.migrations.find((m) => m.vm.name === 'analytics-db')!;
    const node = server.migrations.find((m) => m.vm.name === 'analytics-node-1')!;
    // a second database, whose engine the rules name
    node.findings = [...node.findings, { code: 'APP_PROBE_MISSING', severity: 'warning', message: 'analytics-node-1 is a database VM without an application probe.', remediation: null, strategies: [] }];
    node.suggested_probes = [{ kind: 'tcp', port: 5432, path: '/', expect_status: null }];
    expect(db.findings.map((f) => f.code)).toContain('APP_PROBE_MISSING');
    expect(db.suggested_probes).toEqual([]);
    renderWithApp(<PlanDetail />, { route: `/plans/${plan.id}`, path: '/plans/:planId', token: 'operator', server });

    const panel = await screen.findByRole('region', { name: 'Application probes' });
    expect(panel).toHaveTextContent('2 database VMs have no application probe.');
    const dbRow = within(panel).getByRole('listitem', { name: 'analytics-db' });
    expect(dbRow).toHaveTextContent('No suggestion: add a probe for its service on its migration page.');
    const nodeRow = within(panel).getByRole('listitem', { name: 'analytics-node-1' });
    expect(nodeRow).toHaveTextContent('Suggested: TCP 5432');
    await user.click(within(nodeRow).getByRole('button', { name: 'Use suggested probe' }));
    const dialog = await screen.findByRole('alertdialog', { name: 'Probe analytics-node-1 on TCP 5432?' });
    expect(dialog).toHaveTextContent('The plan returns to draft: validate it again before it starts.');
    await user.click(within(dialog).getByRole('button', { name: 'Use suggested probe' }));
    await waitFor(() => expect(plan.probe_overrides[node.vm.source_id]).toEqual([{ kind: 'tcp', port: 5432, path: '/', expect_status: null }]));
    expect(plan.status).toBe('draft');
  });

  it('keeps Use suggested probe from a viewer, and from a plan that cannot be edited, saying why', async () => {
    const server = createTestServer();
    const running = server.plans.find((p) => p.status === 'running')!;
    const db = server.migrations.find((m) => m.plan_id === running.id && m.findings.some((f) => f.code === 'APP_PROBE_MISSING'));
    expect(db).toBeDefined();
    db!.suggested_probes = [{ kind: 'tcp', port: 3306, path: '/', expect_status: null }];
    renderWithApp(<PlanDetail />, { route: `/plans/${running.id}`, path: '/plans/:planId', token: 'operator', server });
    const panel = await screen.findByRole('region', { name: 'Application probes' });
    const use = within(within(panel).getByRole('listitem', { name: db!.vm.name })).getByRole('button', { name: 'Use suggested probe' });
    expect(use).toHaveAttribute('aria-disabled', 'true');
    expect(use).toHaveAccessibleDescription(/Only a draft or validated plan can be edited \(this one is running\)/);
  });

  it('shows the status its migrations tell, and Finalize instead of Start once they all ended (SDD §8.1, §16, ADV-19)', async () => {
    const server = createTestServer();
    const plan = server.plans.find((p) => p.id === 'plan-2d5c8b93')!;
    plan.status = 'validated'; // a re-validation of a finished plan stores it so
    renderWithApp(<PlanDetail />, { route: `/plans/${plan.id}`, path: '/plans/:planId', token: 'approver', server });
    const header = (await screen.findByRole('heading', { level: 1, name: plan.name })).closest('header')!;
    await waitFor(() => expect(header).toHaveTextContent('Completed'));
    expect(header).not.toHaveTextContent('Validated');
    const actions = await screen.findByRole('group', { name: /plan actions/i });
    expect(within(actions).queryByRole('button', { name: /^start|^resume/i })).not.toBeInTheDocument();
    // nothing to pause either
    expect(within(actions).queryByRole('button', { name: 'Pause' })).not.toBeInTheDocument();
    expect(within(actions).getByRole('button', { name: 'Finalize all' })).not.toHaveAttribute('aria-disabled');
  });

  it('lists the VMs awaiting finalize, oldest first, with what needs a look, and finalizes them with one approval (SDD §8.1, ADV-19)', async () => {
    const user = userEvent.setup({ delay: null });
    const server = createTestServer();
    const plan = server.plans.find((p) => p.id === 'plan-2d5c8b93')!;
    const cache = server.migrations.find((m) => m.vm.name === 'web-cache-01')!;
    expect(cache.phase).toBe('completed');
    cache.source_running_at = '2026-10-08T11:00:00Z';
    renderWithApp(<PlanDetail />, { route: `/plans/${plan.id}`, path: '/plans/:planId', token: 'approver', server });
    const queue = await screen.findByRole('region', { name: 'Awaiting finalize' });
    expect(queue).toHaveTextContent('1 VM awaits finalize: the source VM is still there until it is finalized.');
    const row = within(queue).getByRole('listitem', { name: 'web-cache-01' });
    // completed 8 days ago: past the plan's 72 h
    expect(row).toHaveTextContent('Finalize overdue');
    expect(row).toHaveTextContent('Source VM running');
    // its one wave holds every VM listed: the header's Finalize all does what Finalize wave would
    expect(within(queue).queryByRole('button', { name: /^finalize wave/i })).not.toBeInTheDocument();

    await user.click(within(screen.getByRole('group', { name: /plan actions/i })).getByRole('button', { name: 'Finalize all' }));
    const dialog = await screen.findByRole('alertdialog', { name: `Finalize 1 VM of ${plan.name}?` });
    expect(dialog).toHaveTextContent('Finalizing is irreversible: this VM can no longer be rolled back, and the temporary resources of its source VM are cleaned up.');
    expect(dialog).toHaveTextContent('The source VM of web-cache-01 runs again: what it wrote since is not on RHOSO. Stop it, or roll it back instead.');
    // one VM: "the source VM", not "VMs"
    expect(within(dialog).getByRole('checkbox', { name: /^also delete the source VM\b/i })).not.toBeChecked();
    const confirm = within(dialog).getByRole('button', { name: 'Finalize all' });
    expect(confirm).toHaveAttribute('aria-disabled', 'true');
    await user.type(within(dialog).getByLabelText(`Type ${plan.name} to confirm`), plan.name);
    await user.click(confirm);
    await waitFor(() => expect(cache.phase).toBe('finalized'));
    expect(await screen.findByText('1 VM finalized.')).toBeInTheDocument();
  });

  it('says why a VM in the finalize queue needs a look on hover, focus and tap, never only in a title (SDD §16)', async () => {
    const server = createTestServer();
    const plan = server.plans.find((p) => p.id === 'plan-2d5c8b93')!;
    const cache = server.migrations.find((m) => m.vm.name === 'web-cache-01')!;
    cache.source_running_at = '2026-10-08T11:00:00Z';
    renderWithApp(<PlanDetail />, { route: `/plans/${plan.id}`, path: '/plans/:planId', token: 'approver', server });
    const queue = await screen.findByRole('region', { name: 'Awaiting finalize' });
    const row = within(queue).getByRole('listitem', { name: 'web-cache-01' });
    expect(within(row).getByRole('button', { name: 'Source VM running' })).toHaveAccessibleDescription(/^Running again since .+: its data diverges from RHOSO$/);
    expect(within(row).getByRole('button', { name: 'Finalize overdue' })).toHaveAccessibleDescription(`In Completed longer than the plan's ${plan.finalize_warning_h} h`);
    expect(row.querySelector('[title]')).toBeNull();
  });

  it('names each VM a bulk finalize skipped, with why, until dismissed (SDD §8.1, §16)', async () => {
    const user = userEvent.setup({ delay: null });
    const server = createTestServer();
    const plan = server.plans.find((p) => p.id === 'plan-2d5c8b93')!;
    const cache = server.migrations.find((m) => m.vm.name === 'web-cache-01')!;
    const internals = server as unknown as { migrationAction: (...args: unknown[]) => unknown };
    const real = internals.migrationAction.bind(server);
    internals.migrationAction = (me, m, action, input) => {
      if (action === 'finalize') throw new Error('source cleanup failed: HTTP 409');
      return real(me, m, action, input);
    };
    renderWithApp(<PlanDetail />, { route: `/plans/${plan.id}`, path: '/plans/:planId', token: 'approver', server });
    await user.click(within(await screen.findByRole('group', { name: /plan actions/i })).getByRole('button', { name: 'Finalize all' }));
    const dialog = await screen.findByRole('alertdialog', { name: `Finalize 1 VM of ${plan.name}?` });
    await user.type(within(dialog).getByLabelText(`Type ${plan.name} to confirm`), plan.name);
    await user.click(within(dialog).getByRole('button', { name: 'Finalize all' }));
    const notice = await screen.findByRole('region', { name: 'Not finalized' });
    expect(notice).toHaveTextContent('1 VM was not finalized:');
    expect(within(notice).getByRole('listitem')).toHaveTextContent('web-cache-01: finalize failed: source cleanup failed: HTTP 409');
    expect(screen.getByText('0 VMs finalized. 1 not finalized: the page lists why.')).toBeInTheDocument();
    expect(cache.phase).toBe('completed');
    await user.click(within(notice).getByRole('button', { name: 'Dismiss' }));
    expect(screen.queryByRole('region', { name: 'Not finalized' })).not.toBeInTheDocument();
  });

  it('finalizes one wave by its name, and keeps Finalize from an operator (SDD §8.1, ADV-19)', async () => {
    const user = userEvent.setup({ delay: null });
    const server = createTestServer();
    const plan = server.plans.find((p) => p.id === 'plan-4f2a9c1e')!;
    const web01 = server.migrations.find((m) => m.vm.name === 'web-01')!;
    const wave = plan.waves.find((w) => w.id === web01.wave_id)!;
    const view = renderWithApp(<PlanDetail />, { route: `/plans/${plan.id}`, path: '/plans/:planId', token: 'operator', server });
    let queue = await screen.findByRole('region', { name: 'Awaiting finalize' });
    const blocked = within(queue).getByRole('button', { name: `Finalize wave ${wave.name}` });
    expect(blocked).toHaveAttribute('aria-disabled', 'true');
    expect(blocked).toHaveAccessibleDescription('Requires the approver role.');
    view.unmount();

    renderWithApp(<PlanDetail />, { route: `/plans/${plan.id}`, path: '/plans/:planId', token: 'approver', server });
    queue = await screen.findByRole('region', { name: 'Awaiting finalize' });
    await user.click(within(queue).getByRole('button', { name: `Finalize wave ${wave.name}` }));
    const dialog = await screen.findByRole('alertdialog', { name: `Finalize 1 VM of ${wave.name}?` });
    await user.type(within(dialog).getByLabelText(`Type ${wave.name} to confirm`), wave.name);
    await user.click(within(dialog).getByRole('button', { name: 'Finalize wave' }));
    await waitFor(() => expect(web01.phase).toBe('finalized'));
  });

  it('offers Finalize all in the queue of a running plan once it spans waves, and deletes the sources when asked (SDD §8.1, ADV-19)', async () => {
    const user = userEvent.setup({ delay: null });
    const server = createTestServer();
    const plan = server.plans.find((p) => p.id === 'plan-4f2a9c1e')!;
    const web01 = server.migrations.find((m) => m.vm.name === 'web-01')!;
    // a VM of another wave completes too
    const other = server.migrations.find((m) => m.plan_id === plan.id && m.wave_id && m.wave_id !== web01.wave_id && m.phase === 'syncing')!;
    other.phase = 'completed';
    other.phase_history = [
      ...other.phase_history,
      { from_phase: 'verifying', to_phase: 'completed', at: new Date(Date.now() - 3_600_000).toISOString(), reason: 'verification passed', actor: 'orchestrator' },
    ];
    renderWithApp(<PlanDetail />, { route: `/plans/${plan.id}`, path: '/plans/:planId', token: 'approver', server });
    const queue = await screen.findByRole('region', { name: 'Awaiting finalize' });
    expect(queue).toHaveTextContent('2 VMs await finalize: their source VMs are still there until they are finalized.');
    expect(within(queue).getAllByRole('button', { name: /^finalize wave/i })).toHaveLength(2);
    // the plan still runs: Finalize all is the queue's, not the header's
    expect(within(screen.getByRole('group', { name: /plan actions/i })).queryByRole('button', { name: 'Finalize all' })).not.toBeInTheDocument();

    await user.click(within(queue).getByRole('button', { name: 'Finalize all' }));
    const dialog = await screen.findByRole('alertdialog', { name: `Finalize 2 VMs of ${plan.name}?` });
    expect(dialog).toHaveTextContent('Finalizing is irreversible: these VMs can no longer be rolled back, and the temporary resources of their source VMs are cleaned up.');
    await user.click(within(dialog).getByRole('checkbox', { name: /^also delete the source VMs/i }));
    await user.type(within(dialog).getByLabelText(`Type ${plan.name} to confirm`), plan.name);
    await user.click(within(dialog).getByRole('button', { name: 'Finalize all' }));
    await waitFor(() => expect(other.phase).toBe('finalized'));
    expect(web01.phase).toBe('finalized');
    expect([web01, other].map((m) => m.phase_history.at(-1)?.reason)).toEqual(['finalized; source deleted', 'finalized; source deleted']);
    expect(await screen.findByText('2 VMs finalized.')).toBeInTheDocument();
  });

  it('hides Start once the plan runs: Pause is its action, Resume once paused (ADV-19)', async () => {
    const server = createTestServer();
    const plan = server.plans.find((p) => p.id === 'plan-4f2a9c1e')!;
    expect(plan.status).toBe('running');
    const view = renderWithApp(<PlanDetail />, { route: `/plans/${plan.id}`, path: '/plans/:planId', token: 'operator', server });
    let actions = await screen.findByRole('group', { name: /plan actions/i });
    await waitFor(() => expect(within(actions).getByRole('button', { name: 'Pause' })).not.toHaveAttribute('aria-disabled'));
    expect(within(actions).queryByRole('button', { name: /^start|^resume/i })).not.toBeInTheDocument();
    view.unmount();

    plan.status = 'paused';
    renderWithApp(<PlanDetail />, { route: `/plans/${plan.id}`, path: '/plans/:planId', token: 'operator', server });
    actions = await screen.findByRole('group', { name: /plan actions/i });
    expect(await within(actions).findByRole('button', { name: 'Resume' })).toBeInTheDocument();
  });

  it.each([
    ['plan-c81d44a0', 'Start', 'Start this plan?', 'Start plan'],
    ['plan-7b3e0d52', 'Resume', 'Resume this plan?', 'Resume plan'],
  ])('moves focus to the plan heading when %s goes with the action (%s), and back to the button on Cancel (SDD §16)', async (planId, label, title, confirmLabel) => {
    const user = userEvent.setup({ delay: null });
    const { server } = renderPlan(planId);
    const name = server.plans.find((p) => p.id === planId)!.name;
    const button = await actionButton(new RegExp(`^${label}$`));
    await user.click(button);
    await user.click(within(await screen.findByRole('alertdialog', { name: title })).getByRole('button', { name: 'Cancel' }));
    expect(button).toHaveFocus();

    await user.click(button);
    await user.click(within(await screen.findByRole('alertdialog', { name: title })).getByRole('button', { name: confirmLabel }));
    await waitFor(() => expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument());
    expect(button).not.toBeInTheDocument();
    expect(screen.getByRole('heading', { level: 1, name })).toHaveFocus();
  });

  it('moves focus to the plan heading after Finalize all and Finalize wave, whose buttons go once the VMs are finalized (SDD §16)', async () => {
    const user = userEvent.setup({ delay: null });
    const server = createTestServer();
    const all = server.plans.find((p) => p.id === 'plan-2d5c8b93')!;
    const view = renderWithApp(<PlanDetail />, { route: `/plans/${all.id}`, path: '/plans/:planId', token: 'approver', server });
    await user.click(within(await screen.findByRole('group', { name: /plan actions/i })).getByRole('button', { name: 'Finalize all' }));
    let dialog = await screen.findByRole('alertdialog', { name: `Finalize 1 VM of ${all.name}?` });
    await user.type(within(dialog).getByLabelText(`Type ${all.name} to confirm`), all.name);
    await user.click(within(dialog).getByRole('button', { name: 'Finalize all' }));
    await waitFor(() => expect(within(screen.getByRole('group', { name: /plan actions/i })).queryByRole('button', { name: 'Finalize all' })).not.toBeInTheDocument());
    expect(screen.getByRole('heading', { level: 1, name: all.name })).toHaveFocus();
    view.unmount();

    const plan = server.plans.find((p) => p.id === 'plan-4f2a9c1e')!;
    const web01 = server.migrations.find((m) => m.vm.name === 'web-01')!;
    const wave = plan.waves.find((w) => w.id === web01.wave_id)!;
    renderWithApp(<PlanDetail />, { route: `/plans/${plan.id}`, path: '/plans/:planId', token: 'approver', server });
    const queue = await screen.findByRole('region', { name: 'Awaiting finalize' });
    await user.click(within(queue).getByRole('button', { name: `Finalize wave ${wave.name}` }));
    dialog = await screen.findByRole('alertdialog', { name: `Finalize 1 VM of ${wave.name}?` });
    await user.type(within(dialog).getByLabelText(`Type ${wave.name} to confirm`), wave.name);
    await user.click(within(dialog).getByRole('button', { name: 'Finalize wave' }));
    await waitFor(() => expect(screen.queryByRole('button', { name: `Finalize wave ${wave.name}` })).not.toBeInTheDocument());
    expect(screen.getByRole('heading', { level: 1, name: plan.name })).toHaveFocus();
  });

  it('returns focus to the button on a later Cancel once an action took it to the plan heading (SDD §16)', async () => {
    const user = userEvent.setup({ delay: null });
    const server = createTestServer();
    const plan = server.plans.find((p) => p.id === 'plan-c81d44a0')!;
    const view = renderWithApp(<PlanDetail />, { route: `/plans/${plan.id}`, path: '/plans/:planId', token: 'operator', server });
    await user.click(await actionButton(/^start$/i));
    await user.click(within(await screen.findByRole('alertdialog', { name: 'Start this plan?' })).getByRole('button', { name: 'Start plan' }));
    await waitFor(() => expect(screen.getByRole('heading', { level: 1, name: plan.name })).toHaveFocus());
    await user.click(await actionButton(/^pause$/i));
    await user.click(within(await screen.findByRole('alertdialog', { name: 'Pause this plan?' })).getByRole('button', { name: 'Pause plan' }));
    const resume = await actionButton(/^resume$/i);
    await waitFor(() => expect(resume).not.toHaveAttribute('aria-disabled'));
    await user.click(resume);
    await user.click(within(await screen.findByRole('alertdialog', { name: 'Resume this plan?' })).getByRole('button', { name: 'Cancel' }));
    expect(resume).toHaveFocus();
    view.unmount();

    const rollout = server.plans.find((p) => p.id === 'plan-4f2a9c1e')!;
    const web01 = server.migrations.find((m) => m.vm.name === 'web-01')!;
    const other = server.migrations.find((m) => m.plan_id === rollout.id && m.wave_id && m.wave_id !== web01.wave_id && m.phase === 'syncing')!;
    other.phase = 'completed';
    other.phase_history = [
      ...other.phase_history,
      { from_phase: 'verifying', to_phase: 'completed', at: new Date(Date.now() - 3_600_000).toISOString(), reason: 'verification passed', actor: 'orchestrator' },
    ];
    const [first, second] = [web01.wave_id, other.wave_id].map((id) => rollout.waves.find((w) => w.id === id)!);
    renderWithApp(<PlanDetail />, { route: `/plans/${rollout.id}`, path: '/plans/:planId', token: 'approver', server });
    const queue = await screen.findByRole('region', { name: 'Awaiting finalize' });
    await user.click(within(queue).getByRole('button', { name: `Finalize wave ${first!.name}` }));
    const dialog = await screen.findByRole('alertdialog', { name: `Finalize 1 VM of ${first!.name}?` });
    await user.type(within(dialog).getByLabelText(`Type ${first!.name} to confirm`), first!.name);
    await user.click(within(dialog).getByRole('button', { name: 'Finalize wave' }));
    await waitFor(() => expect(screen.getByRole('heading', { level: 1, name: rollout.name })).toHaveFocus());
    const next = await within(queue).findByRole('button', { name: `Finalize wave ${second!.name}` });
    await user.click(next);
    await user.click(within(await screen.findByRole('alertdialog', { name: `Finalize 1 VM of ${second!.name}?` })).getByRole('button', { name: 'Cancel' }));
    expect(next).toHaveFocus();
  });

  it('moves focus to the plan heading when Dismiss takes the not-finalized notice away (SDD §16)', async () => {
    const user = userEvent.setup({ delay: null });
    const server = createTestServer();
    const plan = server.plans.find((p) => p.id === 'plan-2d5c8b93')!;
    const internals = server as unknown as { migrationAction: (...args: unknown[]) => unknown };
    const real = internals.migrationAction.bind(server);
    internals.migrationAction = (me, m, action, input) => {
      if (action === 'finalize') throw new Error('source cleanup failed: HTTP 409');
      return real(me, m, action, input);
    };
    renderWithApp(<PlanDetail />, { route: `/plans/${plan.id}`, path: '/plans/:planId', token: 'approver', server });
    await user.click(within(await screen.findByRole('group', { name: /plan actions/i })).getByRole('button', { name: 'Finalize all' }));
    const dialog = await screen.findByRole('alertdialog', { name: `Finalize 1 VM of ${plan.name}?` });
    await user.type(within(dialog).getByLabelText(`Type ${plan.name} to confirm`), plan.name);
    await user.click(within(dialog).getByRole('button', { name: 'Finalize all' }));
    const notice = await screen.findByRole('region', { name: 'Not finalized' });
    await user.click(within(notice).getByRole('button', { name: 'Dismiss' }));
    expect(notice).not.toBeInTheDocument();
    expect(screen.getByRole('heading', { level: 1, name: plan.name })).toHaveFocus();
  });

  it('names the advisor policy in force beside the cutover approval, with its mode, threshold and a link (SDD §16, ADV-20)', async () => {
    const view = renderPlan('plan-c81d44a0', 'viewer');
    let settings = await screen.findByRole('region', { name: 'Settings' });
    // the Finance policy covers the DC1 source
    const finance = await within(settings).findByRole('link', { name: 'Finance — approval required' });
    expect(finance).toHaveAttribute('href', '/advisor/policies#policy-pol-finance');
    expect(settings).toHaveTextContent('Asks a person for every migration strategy decision');
    expect(settings).toHaveTextContent('Cutover approvalYes — an approver other than the requester must approve each cutover');
    view.unmount();

    // no stored policy covers the VMware source: the built-in default
    renderPlan('plan-7b3e0d52', 'viewer');
    settings = await screen.findByRole('region', { name: 'Settings' });
    // named as the card the link lands on
    const builtin = await within(settings).findByRole('link', { name: 'Built-in default' });
    expect(builtin).toHaveAttribute('href', '/advisor/policies#policy-global-default');
    expect(settings).toHaveTextContent('Applies a migration strategy decision at 60% confidence or more');
  });

  it('splits Completed into Awaiting finalize and Finalized (SDD §4.2, ADV-19)', async () => {
    renderPlan('plan-2d5c8b93', 'viewer');
    const metrics = await screen.findByRole('region', { name: /plan metrics/i });
    await waitFor(() => expect(within(metrics).getByRole('group', { name: 'Awaiting finalize' })).toHaveTextContent('Awaiting finalize1'));
    expect(within(metrics).getByRole('group', { name: 'Finalized' })).toHaveTextContent('Finalized1');
    expect(within(metrics).queryByRole('group', { name: 'Completed' })).not.toBeInTheDocument();
  });

  it('sets the finalize reminder in the plan form (SDD §16, ADV-19)', async () => {
    const user = userEvent.setup({ delay: null });
    const server = createTestServer();
    const plan = server.plans.find((p) => p.id === 'plan-c81d44a0')!;
    renderWithApp(<PlanDetail />, { route: `/plans/${plan.id}`, path: '/plans/:planId', token: 'operator', server });
    await user.click(await actionButton(/^edit plan$/i));
    const dialog = await screen.findByRole('dialog', { name: /edit plan/i });
    await user.click(within(dialog).getByText(/^advanced:/i));
    const reminder = within(dialog).getByLabelText(/finalize reminder \(hours\)/i);
    expect(reminder).toHaveValue(72);
    await user.clear(reminder);
    await user.type(reminder, '48');
    await user.click(within(dialog).getByRole('button', { name: /^save changes$/i }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(plan.finalize_warning_h).toBe(48);
    // and comes back into the form
    await user.click(await actionButton(/^edit plan$/i));
    const again = await screen.findByRole('dialog', { name: /edit plan/i });
    await user.click(within(again).getByText(/^advanced:/i));
    expect(within(again).getByLabelText(/finalize reminder \(hours\)/i)).toHaveValue(48);
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
    for (const label of ['In progress', 'Awaiting finalize', 'Finalized', 'Failed']) {
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
    // the list loaded before the failed refetch still stands: the waves and findings keep showing it (SDD §16)
    expect(within(screen.getByRole('region', { name: /waves/i })).queryByText('Progress unknown')).not.toBeInTheDocument();
    expect(screen.getByRole('region', { name: 'Findings' })).toHaveTextContent(/\d+ blockers/);
  }, 15_000);

  it('shows wave progress and findings as unknown, not empty or zero, when the migrations cannot be loaded (SDD §16)', async () => {
    const server = createTestServer();
    const plan = server.plans.find((p) => p.id === 'plan-4f2a9c1e')!;
    expect(plan.waves).toHaveLength(3);
    const handle = server.handle.bind(server);
    server.handle = (method, path, query, body, token) =>
      method === 'GET' && path === '/migrations'
        ? { status: 400, body: { error: { code: 'bad_request', message: 'refused for the test' } } }
        : handle(method, path, query, body, token);
    renderWithApp(<PlanDetail />, { route: `/plans/${plan.id}`, path: '/plans/:planId', token: 'viewer', server });
    expect(await screen.findByText(/migrations are unavailable/i)).toBeInTheDocument();

    // each wave keeps what the plan says about it; its state and progress come from the migrations
    const waves = within(screen.getByRole('region', { name: /waves/i })).getAllByRole('listitem');
    expect(waves).toHaveLength(3);
    for (const [i, wave] of waves.entries()) {
      expect(wave).toHaveTextContent(plan.waves.find((w) => w.order === i + 1)!.name);
      expect(within(wave).getByText('Unknown')).toBeInTheDocument();
      expect(within(wave).getByText('Progress unknown')).toBeInTheDocument();
      expect(within(wave).queryByRole('progressbar')).not.toBeInTheDocument();
      expect(wave).not.toHaveTextContent(/Active|Waiting|Complete|0\/0/);
    }
    const findings = screen.getByRole('region', { name: 'Findings' });
    expect(findings).toHaveTextContent('Unknown: the findings could not be loaded.');
    expect(findings).not.toHaveTextContent(/0 blockers|no findings yet/i);
  });

  it('counts the migrations to start from the statistics when the list cannot be loaded, else says unknown (SDD §16)', async () => {
    const user = userEvent.setup({ delay: null });
    const server = createTestServer();
    const plan = server.plans.find((p) => p.id === 'plan-c81d44a0')!;
    const count = server.migrations.filter((m) => m.plan_id === plan.id).length;
    expect(count).toBe(4);
    const refused = new Set(['/migrations']);
    const handle = server.handle.bind(server);
    server.handle = (method, path, query, body, token) =>
      method === 'GET' && refused.has(path)
        ? { status: 400, body: { error: { code: 'bad_request', message: 'refused for the test' } } }
        : handle(method, path, query, body, token);
    const view = renderWithApp(<PlanDetail />, { route: `/plans/${plan.id}`, path: '/plans/:planId', token: 'operator', server });
    await screen.findByText(/migrations are unavailable/i);
    await waitFor(() => expect(within(screen.getByRole('region', { name: /plan metrics/i })).getByRole('group', { name: 'Migrations' })).toHaveTextContent('Migrations4'));

    await user.click(await actionButton(/^start$/i));
    expect(await screen.findByRole('alertdialog', { name: /start this plan/i })).toHaveTextContent(/\b4 migrations in 1 wave\./);
    view.unmount();

    refused.add('/stats');
    renderWithApp(<PlanDetail />, { route: `/plans/${plan.id}`, path: '/plans/:planId', token: 'operator', server });
    await screen.findByText(/statistics are unavailable/i);
    await screen.findByText(/migrations are unavailable/i);
    await user.click(await actionButton(/^start$/i));
    const dialog = await screen.findByRole('alertdialog', { name: /start this plan/i });
    expect(dialog).toHaveTextContent(/An unknown number of migrations in 1 wave\./);
    expect(dialog).not.toHaveTextContent(/\b0 migrations/);
  });

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

  it('announces the wave size error from a live region already in the dialog (SDD §16)', async () => {
    const user = userEvent.setup({ delay: null });
    renderPlan('plan-0e9f6a17');
    await user.click(await actionButton(/auto-plan waves/i));
    const dialog = await screen.findByRole('alertdialog', { name: /auto-plan waves|replace the waves/i });
    const regions = within(dialog).queryAllByRole('status');
    const size = within(dialog).getByLabelText(/maximum vms per wave/i);
    await user.clear(size);
    await user.type(size, '0');
    expect(size).toHaveAccessibleDescription('Enter a whole number from 1 to 1000.');
    const error = document.getElementById(size.getAttribute('aria-describedby')!)!;
    expect(regions).toContain(error.closest('[role="status"]'));
  });

  it('says why Plan waves is unavailable while the wave size is not a whole number from 1 to 1000 (SDD §12, §16)', async () => {
    const user = userEvent.setup({ delay: null });
    const { server } = renderPlan('plan-0e9f6a17');
    await user.click(await actionButton(/auto-plan waves/i));
    const dialog = await screen.findByRole('alertdialog', { name: /auto-plan waves|replace the waves/i });
    // it warns before it runs: the plan returns to draft and is validated again, clearing approvals
    expect(dialog).toHaveTextContent(/returns to draft/i);
    expect(dialog).toHaveTextContent(/clears approvals and cutover requests/i);
    const size = within(dialog).getByLabelText(/maximum vms per wave/i);
    const confirm = within(dialog).getByRole('button', { name: /^plan waves$/i });
    await user.clear(size);
    await user.type(size, '1001');
    expect(confirm).toHaveAttribute('aria-disabled', 'true');
    expect(confirm).toHaveAccessibleDescription('Enter a whole number from 1 to 1000.');
    // the API takes up to 1000 (SDD §12): a size above the old cap of 100 is fine
    await user.clear(size);
    await user.type(size, '500');
    expect(confirm).not.toHaveAttribute('aria-disabled');
    await user.click(confirm);
    await waitFor(() => expect(server.plans.find((p) => p.id === 'plan-0e9f6a17')!.waves.length).toBeGreaterThan(0));
  });

  it.each([
    ['plan-c81d44a0', /^start$/i, /^start plan$/i, 'Plan started.'],
    ['plan-7b3e0d52', /^resume$/i, /^resume plan$/i, 'Plan resumed.'],
    ['plan-95a7e3f1', /^start again$/i, /^start again$/i, 'Plan started again.'],
    ['plan-4f2a9c1e', /^pause$/i, /^pause plan$/i, 'Plan paused.'],
  ])('announces the outcome of the action on %s (SDD §16)', async (planId, action, confirmLabel, message) => {
    const user = userEvent.setup({ delay: null });
    renderPlan(planId);
    await user.click(await actionButton(action));
    const dialog = await screen.findByRole('alertdialog');
    await user.click(within(dialog).getByRole('button', { name: confirmLabel }));
    expect(await screen.findByText(message)).toHaveAttribute('role', 'status');
  });

  it('returns focus to the header Auto-plan waves when the empty waves board it was opened from goes (SDD §16)', async () => {
    const user = userEvent.setup({ delay: null });
    renderPlan('plan-0e9f6a17');
    const waves = await screen.findByRole('region', { name: /^waves$/i });
    await user.click(within(waves).getByRole('button', { name: /auto-plan waves/i }));
    const dialog = await screen.findByRole('alertdialog', { name: /auto-plan waves/i });
    await user.click(within(dialog).getByRole('button', { name: /^plan waves$/i }));
    await waitFor(() => expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument());
    await within(waves).findByText('Pilot');
    expect(within(waves).queryByRole('button', { name: /auto-plan waves/i })).not.toBeInTheDocument();
    expect(await actionButton(/auto-plan waves/i)).toHaveFocus();
  });

  it('announces how many waves were planned (SDD §16)', async () => {
    const user = userEvent.setup({ delay: null });
    const { server } = renderPlan('plan-0e9f6a17');
    await user.click(await actionButton(/auto-plan waves/i));
    const dialog = await screen.findByRole('alertdialog');
    await user.click(within(dialog).getByRole('button', { name: /^plan waves$/i }));
    await waitFor(() => expect(server.plans.find((p) => p.id === 'plan-0e9f6a17')!.waves.length).toBeGreaterThan(0));
    const n = server.plans.find((p) => p.id === 'plan-0e9f6a17')!.waves.length;
    expect(await screen.findByText(`Waves planned: ${n} wave${n === 1 ? '' : 's'}.`)).toHaveAttribute('role', 'status');
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

  it('keeps the destination of a plan with a failed migration, saying why (SDD §12, §16)', async () => {
    const user = userEvent.setup({ delay: null });
    const server = createTestServer();
    const failed = server.migrations.find((m) => m.plan_id === 'plan-c81d44a0')!;
    Object.assign(failed, { phase: 'failed', downtime_started_at: null, downtime_ended_at: null }); // its source kept running
    renderWithApp(<PlanDetail />, { route: '/plans/plan-c81d44a0', path: '/plans/:planId', token: 'operator', server });
    await user.click(await actionButton(/^edit plan$/i));
    const dialog = await screen.findByRole('dialog', { name: /edit plan/i });
    const destination = within(dialog).getByLabelText(/^destination/i);
    expect(destination).toBeDisabled();
    expect(destination).toHaveAccessibleDescription(
      `The failed migration of ${failed.vm.name} still needs this destination for its rollback: roll it back or cancel it first.`,
    );
    // the rest stays editable, to fix the cause before a retry
    expect(within(dialog).getByLabelText(/downtime slo/i)).toBeEnabled();
  });

  it('edits a plan stored with sync values the old form refused: threshold 0, 60 passes, 100 disks (SDD §16)', async () => {
    const user = userEvent.setup({ delay: null });
    const server = createTestServer();
    const plan = server.plans.find((p) => p.id === 'plan-c81d44a0')!;
    // as the API or `seamless plan apply` may store them
    plan.convergence_threshold_bytes = 0;
    plan.max_sync_passes = 60;
    plan.estimator_overrides = { ...plan.estimator_overrides, parallel_disks: 100 };
    renderWithApp(<PlanDetail />, { route: `/plans/${plan.id}`, path: '/plans/:planId', token: 'operator', server });
    await user.click(await actionButton(/^edit plan$/i));
    const dialog = await screen.findByRole('dialog', { name: /edit plan/i });
    expect(within(dialog).getByLabelText(/convergence threshold/i)).toHaveValue(0);
    expect(within(dialog).getByLabelText(/max sync passes/i)).toHaveValue(60);
    expect(within(dialog).getByLabelText(/disks scanned in parallel/i)).toHaveValue(100);

    const name = within(dialog).getByLabelText(/^name/i);
    await user.clear(name);
    await user.type(name, 'Analytics, renamed');
    await user.click(within(dialog).getByRole('button', { name: /^save changes$/i }));

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    const saved = server.plans.find((p) => p.id === plan.id)!;
    expect(saved.name).toBe('Analytics, renamed');
    expect(server.events.filter((e) => e.kind === 'plan.updated').at(-1)?.data).toEqual({ fields: ['name'] });
    expect([saved.convergence_threshold_bytes, saved.max_sync_passes, saved.estimator_overrides.parallel_disks]).toEqual([0, 60, 100]);
  });

  it('edits a plan whose mapping names contain =, -> or → and keeps them (SDD §16)', async () => {
    const user = userEvent.setup({ delay: null });
    const server = createTestServer();
    const plan = server.plans.find((p) => p.id === 'plan-c81d44a0')!;
    // names the API accepts, as the API or `seamless plan apply` may store them
    plan.mappings = { ...plan.mappings, flavors: { 'm1=gpu': 'm2->gpu' }, networks: { 'net→a': 'net-b' } };
    renderWithApp(<PlanDetail />, { route: `/plans/${plan.id}`, path: '/plans/:planId', token: 'operator', server });
    await user.click(await actionButton(/^edit plan$/i));
    const dialog = await screen.findByRole('dialog', { name: /edit plan/i });
    expect(within(dialog).getByLabelText(/flavor mappings/i)).toHaveValue('"m1=gpu" = "m2->gpu"');

    const name = within(dialog).getByLabelText(/^name/i);
    await user.clear(name);
    await user.type(name, 'Renamed');
    await user.click(within(dialog).getByRole('button', { name: /^save changes$/i }));

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    const saved = server.plans.find((p) => p.id === plan.id)!;
    expect([saved.name, saved.mappings.flavors, saved.mappings.networks]).toEqual(['Renamed', { 'm1=gpu': 'm2->gpu' }, { 'net→a': 'net-b' }]);
    expect(server.events.filter((e) => e.kind === 'plan.updated').at(-1)?.data).toEqual({ fields: ['name'] });
  }, 30_000);

  it('lists a selected VM that left the source, with Remove, so the plan can be saved without it (SDD §16)', async () => {
    const user = userEvent.setup({ delay: null });
    const server = createTestServer();
    const plan = server.plans.find((p) => p.id === 'plan-c81d44a0')!;
    const kept = [...plan.vm_ids];
    // a VM deleted from the source after the plan was made: the inventory no longer lists it
    plan.vm_ids = [...kept, 'os-gone-01'];
    renderWithApp(<PlanDetail />, { route: `/plans/${plan.id}`, path: '/plans/:planId', token: 'operator', server });
    await user.click(await actionButton(/^edit plan$/i));
    const dialog = await screen.findByRole('dialog', { name: /edit plan/i });
    const gone = await within(dialog).findByRole('group', { name: /a selected vm is no longer in the source/i });
    expect(gone).toHaveTextContent('os-gone-01');

    await user.click(within(gone).getByRole('button', { name: 'Remove os-gone-01 from the plan' }));
    expect(within(dialog).queryByRole('group', { name: /no longer in the source/i })).not.toBeInTheDocument();
    // the removed button's place: focus moves to the VMs, never to the page
    expect(within(dialog).getByText('VMs', { selector: 'legend' }).closest('legend')).toHaveFocus();
    await user.click(within(dialog).getByRole('button', { name: /^save changes$/i }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(server.plans.find((p) => p.id === plan.id)!.vm_ids).toEqual(kept);
    expect(server.events.filter((e) => e.kind === 'plan.updated').at(-1)?.data).toEqual({ fields: ['vm_ids'] });
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

  it('edits the guest write rate, prefilled; overrides the form does not show are kept (SDD §16)', async () => {
    const user = userEvent.setup({ delay: null });
    const server = createTestServer();
    const plan = server.plans.find((p) => p.id === 'plan-c81d44a0')!;
    plan.estimator_overrides = { change_rate_bps: 4 * 2 ** 20, max_aggregate_scan_bps: 800 * 2 ** 20, v2v_s: 240 };
    renderWithApp(<PlanDetail />, { route: '/plans/plan-c81d44a0', path: '/plans/:planId', token: 'operator', server });

    await user.click(await actionButton(/^edit plan$/i));
    const dialog = await screen.findByRole('dialog', { name: /edit plan/i });
    await user.click(within(dialog).getByText(/^advanced:/i));
    const rate = within(dialog).getByLabelText(/guest write rate/i);
    expect(rate).toHaveValue(4);
    expect(within(dialog).getByLabelText(/aggregate scan cap/i)).toHaveValue(800);
    await user.clear(rate);
    await user.type(rate, '6');
    // a cleared field goes back to the planning default: its override is removed
    await user.clear(within(dialog).getByLabelText(/aggregate scan cap/i));
    await user.click(within(dialog).getByRole('button', { name: /^save changes$/i }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(plan.estimator_overrides).toEqual({ change_rate_bps: 6 * 2 ** 20, v2v_s: 240 });
  });

  it("edits the application probes of every VM, prefilled; each VM's own probes are kept (SDD §16, ADV-18)", async () => {
    const user = userEvent.setup({ delay: null });
    const server = createTestServer();
    const plan = server.plans.find((p) => p.id === 'plan-c81d44a0')!;
    const own = { [plan.vm_ids[0]!]: [{ kind: 'tcp' as const, port: 5432, path: '/', expect_status: null }] };
    plan.verification = { ...plan.verification, app_probes: [{ kind: 'http', port: 80, path: '/', expect_status: null }] };
    plan.probe_overrides = own;
    renderWithApp(<PlanDetail />, { route: '/plans/plan-c81d44a0', path: '/plans/:planId', token: 'operator', server });

    await user.click(await actionButton(/^edit plan$/i));
    const dialog = await screen.findByRole('dialog', { name: /edit plan/i });
    await user.click(within(dialog).getByText(/^advanced:/i));
    const probes = within(dialog).getByRole('group', { name: 'Application probes of every VM' });
    expect(within(probes).getByRole('combobox', { name: /^probe 1 kind$/i })).toHaveValue('http');
    await user.click(within(probes).getByRole('button', { name: 'Add probe' }));
    await user.selectOptions(within(probes).getByRole('combobox', { name: /^probe 2 kind$/i }), 'https');
    await user.type(within(probes).getByRole('spinbutton', { name: /^probe 2 port$/i }), '443');
    const path = within(probes).getByRole('textbox', { name: /^probe 2 path$/i });
    await user.clear(path);
    await user.type(path, '/health');
    await user.click(within(dialog).getByRole('button', { name: /^save changes$/i }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(plan.verification.app_probes).toEqual([
      { kind: 'http', port: 80, path: '/', expect_status: null },
      { kind: 'https', port: 443, path: '/health', expect_status: null },
    ]);
    expect(plan.probe_overrides).toEqual(own);
  });

  it('drops the own probes of a VM taken out of the plan (SDD §7.5)', async () => {
    const user = userEvent.setup({ delay: null });
    const server = createTestServer();
    const plan = server.plans.find((p) => p.id === 'plan-c81d44a0')!;
    const db = server.migrations.find((m) => m.vm.name === 'analytics-db')!;
    const node = server.migrations.find((m) => m.vm.name === 'analytics-node-1')!;
    const probe = { kind: 'tcp' as const, port: 5432, path: '/', expect_status: null };
    plan.probe_overrides = { [db.vm.source_id]: [probe], [node.vm.source_id]: [probe] };
    // analytics-db leaves the plan: cancel its migration so the plan may drop it (SDD §5.4)
    db.phase = 'cancelled';
    renderWithApp(<PlanDetail />, { route: '/plans/plan-c81d44a0', path: '/plans/:planId', token: 'operator', server });
    await user.click(await actionButton(/^edit plan$/i));
    const dialog = await screen.findByRole('dialog', { name: /edit plan/i });
    await user.click(await within(dialog).findByRole('checkbox', { name: 'Select analytics-db' }));
    await user.click(within(dialog).getByRole('button', { name: /^save changes$/i }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(plan.probe_overrides).toEqual({ [node.vm.source_id]: [probe] });
  });

  it('refuses to save a probe the API would refuse, and says why', async () => {
    const user = userEvent.setup({ delay: null });
    renderPlan('plan-c81d44a0');
    await user.click(await actionButton(/^edit plan$/i));
    const dialog = await screen.findByRole('dialog', { name: /edit plan/i });
    await user.click(within(dialog).getByText(/^advanced:/i));
    const probes = within(dialog).getByRole('group', { name: 'Application probes of every VM' });
    await user.click(within(probes).getByRole('button', { name: 'Add probe' }));
    await user.type(within(probes).getByRole('spinbutton', { name: /^probe 1 port$/i }), '70000');
    await user.click(within(dialog).getByRole('button', { name: /^save changes$/i }));
    expect(await within(dialog).findAllByText('Probe 1: use a port from 1 to 65535.')).not.toHaveLength(0);
    expect(screen.getByRole('dialog', { name: /edit plan/i })).toBeInTheDocument();
  });

  it('edits the measured step times, prefilled; a cleared one goes back to the planning default (SDD §16)', async () => {
    const user = userEvent.setup({ delay: null });
    const server = createTestServer();
    const plan = server.plans.find((p) => p.id === 'plan-c81d44a0')!;
    plan.estimator_overrides = { shutdown_s: 30, boot_s: 90, v2v_s: 240 };
    renderWithApp(<PlanDetail />, { route: '/plans/plan-c81d44a0', path: '/plans/:planId', token: 'operator', server });

    await user.click(await actionButton(/^edit plan$/i));
    const dialog = await screen.findByRole('dialog', { name: /edit plan/i });
    await user.click(within(dialog).getByText(/^advanced:/i));
    expect(within(dialog).getByLabelText(/source shutdown/i)).toHaveValue(30);
    expect(within(dialog).getByLabelText(/^boot/i)).toHaveValue(90);
    await user.clear(within(dialog).getByLabelText(/source shutdown/i));
    await user.type(within(dialog).getByLabelText(/snapshot/i), '25');
    await user.click(within(dialog).getByRole('button', { name: /^save changes$/i }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(plan.estimator_overrides).toEqual({ snapshot_s: 25, boot_s: 90, v2v_s: 240 });
    expect(server.events.filter((e) => e.kind === 'plan.updated').at(-1)?.data).toEqual({ fields: ['estimator_overrides'] });
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
    // the removed row's button is gone: focus moves to its section, never to the page (SDD §16)
    expect(within(overrides).getByText('Per-VM strategy', { selector: 'legend' })).toHaveFocus();
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

  it('lists no cancelled migration as waiting for advisor approval, whatever hold it stored (SDD §14.6)', async () => {
    const server = createTestServer();
    server.migrations.find((x) => x.id === 'mig-b2c4d6e871')!.phase = 'cancelled';
    renderWithApp(<PlanDetail />, { route: '/plans/plan-c81d44a0', path: '/plans/:planId', token: 'admin', server });
    // the plan's migrations have loaded
    expect((await screen.findAllByRole('link', { name: 'analytics-node-1' })).length).toBeGreaterThan(0);
    expect(screen.queryByRole('region', { name: 'Waiting for advisor approval' })).not.toBeInTheDocument();
  });

  it('lists the migrations waiting for advisor approval and keeps the panel, focused, after the last decision (SDD §14.6, §16, ADV-04)', async () => {
    const user = userEvent.setup({ delay: null });
    const { server } = renderPlan('plan-c81d44a0', 'admin');
    const panel = await screen.findByRole('region', { name: 'Waiting for advisor approval' });
    expect(await within(panel).findByRole('link', { name: 'analytics-node-2' })).toHaveAttribute('href', '/migrations/mig-b2c4d6e871');
    expect(await within(panel).findByText('Decision: Waiting for approval — policy pol-finance requires approval')).toBeInTheDocument();
    expect(panel).toHaveTextContent('Recommended: Storage handover · 86% confidence (threshold 60%)');
    await user.click(within(panel).getByRole('button', { name: 'Approve' }));
    await user.click(within(await screen.findByRole('alertdialog', { name: 'Approve Storage handover for analytics-node-2?' })).getByRole('button', { name: 'Approve' }));
    expect(await within(panel).findByText('Nothing waits for approval any more.')).toBeInTheDocument();
    expect(within(panel).getByText('Waiting for advisor approval')).toHaveFocus();
    expect(screen.getByText('Approved: analytics-node-2 runs Storage handover.')).toBeInTheDocument();
    expect(server.migrations.find((m) => m.id === 'mig-b2c4d6e871')!.advisor_hold).toBeNull();
  });

  it('shows the waiting migrations without the actions below advisor.approve, and no panel when nothing waits', async () => {
    const { unmount } = renderPlan('plan-c81d44a0', 'operator');
    const panel = await screen.findByRole('region', { name: 'Waiting for advisor approval' });
    await within(panel).findByText('Decision: Waiting for approval — policy pol-finance requires approval');
    expect(within(panel).queryByRole('button', { name: /approve|override/i })).not.toBeInTheDocument();
    unmount();
    renderPlan('plan-4f2a9c1e', 'admin');
    await screen.findByRole('region', { name: 'Migrations' });
    expect(screen.queryByRole('region', { name: 'Waiting for advisor approval' })).not.toBeInTheDocument();
  });

  it('lets an approver turn off Distinct approver, read-only below; the settings say who approves (SDD §16, ADV-17)', async () => {
    const user = userEvent.setup({ delay: null });
    const operator = renderPlan('plan-c81d44a0', 'operator');
    expect(await screen.findByText('Yes — an approver other than the requester must approve each cutover')).toBeInTheDocument();
    await user.click(await actionButton(/^edit plan$/i));
    let dialog = await screen.findByRole('dialog', { name: /edit plan/i });
    const readOnly = within(dialog).getByRole('checkbox', { name: /require a distinct approver/i });
    expect(readOnly).toBeChecked();
    expect(readOnly).toBeDisabled();
    expect(readOnly).toHaveAccessibleDescription(/changing the approval policy requires the approver role/i);
    operator.unmount();

    const { server } = renderPlan('plan-c81d44a0', 'approver');
    await user.click(await actionButton(/^edit plan$/i));
    dialog = await screen.findByRole('dialog', { name: /edit plan/i });
    const box = within(dialog).getByRole('checkbox', { name: /require a distinct approver/i });
    await waitFor(() => expect(box).toBeEnabled());
    await user.click(box);
    await user.click(within(dialog).getByRole('button', { name: /^save changes$/i }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(server.plans.find((p) => p.id === 'plan-c81d44a0')?.require_distinct_approver).toBe(false);
    expect(server.events.filter((e) => e.kind === 'plan.updated').at(-1)?.data).toEqual({ fields: ['require_distinct_approver'] });
    expect(await screen.findByText('Yes — an approver must approve each cutover')).toBeInTheDocument();
  });
});
