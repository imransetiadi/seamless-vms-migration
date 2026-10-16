import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { createTestServer, recordScrolls, renderWithApp } from '../test/utils';
import AdvisorPolicies from './AdvisorPolicies';

const FINANCE = 'Finance — approval required';

const card = (name: string) => screen.getByRole('article', { name });

describe('Advisor policies page (SDD §14.6, §16, ADV-04)', () => {
  it('scrolls to and focuses the policy a link names, the built-in default too (SDD §16, ADV-20)', async () => {
    const view = renderWithApp(<AdvisorPolicies />, { route: '/advisor/policies#policy-pol-finance', token: 'viewer' });
    const finance = await screen.findByRole('article', { name: 'Finance — approval required' });
    expect(finance).toHaveAttribute('id', 'policy-pol-finance');
    await waitFor(() => expect(finance).toHaveFocus());
    view.unmount();

    renderWithApp(<AdvisorPolicies />, { route: '/advisor/policies#policy-global-default', token: 'viewer' });
    const builtin = await screen.findByRole('article', { name: 'Built-in default' });
    expect(builtin).toHaveAttribute('id', 'policy-global-default');
    await waitFor(() => expect(builtin).toHaveFocus());
  });

  it('asks a new policy for three facts of evidence, and says what they are (SDD §14.4, ADV-02)', async () => {
    const user = userEvent.setup({ delay: null });
    renderWithApp(<AdvisorPolicies />, { route: '/advisor/policies', token: 'admin' });
    await user.click(await screen.findByRole('button', { name: /new policy/i }));
    const dialog = await screen.findByRole('dialog');
    const minimum = within(dialog).getByLabelText(/^minimum evidence/i);
    expect(minimum).toHaveValue(3);
    expect(minimum).toHaveAccessibleDescription(
      "Fewer facts that support or oppose the decision send it for approval; each recommendation's evidence card lists them.",
    );
  });

  it('lists the policies with the built-in default, which is read-only', async () => {
    renderWithApp(<AdvisorPolicies />, { route: '/advisor/policies', token: 'viewer' });
    expect(await screen.findByRole('heading', { level: 2, name: '1 policy' })).toBeInTheDocument();
    const finance = card(FINANCE);
    expect(finance).toHaveTextContent('Plans from RHOSP 17.1 — DC1');
    expect(finance).toHaveTextContent('Always asks for approval');
    expect(finance).toHaveTextContent('Migration strategy');
    expect(finance).toHaveTextContent(/Override reason\s*Required/);
    expect(finance).toHaveTextContent('by rina');
    const builtIn = card('Built-in default');
    expect(builtIn).toHaveTextContent('Plans no other policy covers');
    expect(builtIn).toHaveTextContent('Applies above the threshold');
    expect(builtIn).toHaveTextContent(/Threshold\s*60%/);
    expect(builtIn).toHaveTextContent(/Override reason\s*Optional/);
    expect(builtIn).toHaveTextContent(/read-only/i);
    expect(within(builtIn).queryAllByRole('button')).toEqual([]);
  });

  it('keeps the policy actions in view without advisor.policy.manage and says why they are unavailable', async () => {
    renderWithApp(<AdvisorPolicies />, { route: '/advisor/policies', token: 'approver' });
    await screen.findByRole('heading', { level: 2, name: '1 policy' });
    const reason = 'Managing advisor policies requires the admin role.';
    for (const button of [
      screen.getByRole('button', { name: 'New policy' }),
      within(card(FINANCE)).getByRole('button', { name: `Edit ${FINANCE}` }),
      within(card(FINANCE)).getByRole('button', { name: `Delete ${FINANCE}` }),
    ]) {
      expect(button).toHaveAttribute('aria-disabled', 'true');
      expect(button).toHaveAccessibleDescription(reason);
    }
  });

  it('names the field of each number it refuses: the summary lists them together', async () => {
    const user = userEvent.setup({ delay: null });
    renderWithApp(<AdvisorPolicies />, { route: '/advisor/policies' });
    await screen.findByRole('heading', { level: 2, name: '1 policy' });
    await user.click(screen.getByRole('button', { name: 'New policy' }));
    const dialog = await screen.findByRole('dialog', { name: 'New policy' });
    await user.type(within(dialog).getByLabelText(/^Name/), 'Numbers');
    await user.clear(within(dialog).getByLabelText('Threshold (%)'));
    await user.type(within(dialog).getByLabelText('Threshold (%)'), '101');
    await user.clear(within(dialog).getByLabelText(/^minimum evidence/i));
    await user.type(within(dialog).getByLabelText(/^minimum evidence/i), '1.5');
    await user.click(within(dialog).getByRole('button', { name: 'Create policy' }));
    const summary = await within(dialog).findByRole('alert');
    expect(within(summary).getAllByRole('link').map((a) => a.textContent)).toEqual([
      'Enter the threshold as a percent from 0 to 100.',
      'Enter the minimum evidence as a whole number from 0 to 100.',
    ]);
  });

  it('creates a policy for chosen plans once the form says what to fix', async () => {
    const user = userEvent.setup({ delay: null });
    const { server } = renderWithApp(<AdvisorPolicies />, { route: '/advisor/policies' });
    await screen.findByRole('heading', { level: 2, name: '1 policy' });
    await user.click(screen.getByRole('button', { name: 'New policy' }));
    const dialog = await screen.findByRole('dialog', { name: 'New policy' });
    expect(within(dialog).getByLabelText(/^Name/)).toHaveFocus();
    await user.selectOptions(within(dialog).getByLabelText('Applies to'), 'plans');
    await user.click(within(dialog).getByRole('button', { name: 'Create policy' }));
    const summary = await within(dialog).findByRole('alert');
    expect(summary).toHaveTextContent('Fix 2 problems to save the policy');
    expect(within(summary).getAllByRole('link').map((a) => a.textContent)).toEqual(['Enter a policy name.', 'Choose at least one plan.']);
    expect(server.policies).toHaveLength(1);

    await user.type(within(dialog).getByLabelText(/^Name/), 'Spark — stricter');
    await user.click(within(dialog).getByRole('checkbox', { name: 'Shared Ceph handover — analytics' }));
    await user.click(within(dialog).getByRole('checkbox', { name: 'Migration strategy' }));
    await user.selectOptions(within(dialog).getByLabelText('When the advisor decides'), 'auto_apply_above_threshold');
    await user.clear(within(dialog).getByLabelText('Threshold (%)'));
    await user.type(within(dialog).getByLabelText('Threshold (%)'), '80');
    await user.click(within(dialog).getByRole('checkbox', { name: /require a reason to override/i }));
    await user.click(within(dialog).getByRole('button', { name: 'Create policy' }));

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(screen.getByRole('status')).toHaveTextContent('Policy Spark — stricter created.');
    expect(server.policies.at(-1)).toMatchObject({
      name: 'Spark — stricter',
      scope: { provider_ids: [], plan_ids: ['plan-c81d44a0'] },
      decision_types: ['migration_strategy'],
      mode: 'auto_apply_above_threshold',
      threshold: 0.8,
      // the form's default for a new policy (ADV-02)
      min_evidence_for_auto_apply: 3,
      require_override_reason: false,
      updated_by: 'rina',
    });
    // a plan's policy is the most specific: listed first, whatever its name
    expect(await screen.findByRole('heading', { level: 2, name: '2 policies' })).toBeInTheDocument();
    const names = screen.getAllByRole('article').map((a) => within(a).getByRole('heading', { level: 3 }).textContent);
    expect(names).toEqual(['Spark — stricter', FINANCE, 'Built-in default']);
    expect(card('Spark — stricter')).toHaveTextContent('Plan Shared Ceph handover — analytics');
  });

  it('scrolls a failed save into view (SDD §16)', async () => {
    const recorder = recordScrolls();
    try {
      const user = userEvent.setup({ delay: null });
      const server = createTestServer();
      const handle = server.handle.bind(server);
      server.handle = (method, path, query, body, token) =>
        method === 'PUT' && path.startsWith('/advisor/policies/')
          ? { status: 503, body: { error: { code: 'unavailable', message: 'database unavailable' } } }
          : handle(method, path, query, body, token);
      renderWithApp(<AdvisorPolicies />, { route: '/advisor/policies', server });
      await screen.findByRole('heading', { level: 2, name: '1 policy' });
      await user.click(within(card(FINANCE)).getByRole('button', { name: `Edit ${FINANCE}` }));
      const dialog = await screen.findByRole('dialog', { name: 'Edit policy' });
      await user.click(within(dialog).getByRole('button', { name: 'Save policy' }));
      const alert = await within(dialog).findByRole('alert');
      expect(alert).toHaveTextContent('The policy was not saved');
      expect(recorder.scrolls.map((s) => s.element)).toContain(alert);
    } finally {
      recorder.restore();
    }
  });

  it('keeps Cancel focusable while the policy is saved, saying why (SDD §16)', async () => {
    const user = userEvent.setup({ delay: null });
    renderWithApp(<AdvisorPolicies />, {
      route: '/advisor/policies',
      // the save never answers: the dialog stays busy
      wrapFetch: (mock) => (input, init) => ((init?.method ?? 'GET') === 'GET' ? mock(input, init) : new Promise<Response>(() => {})),
    });
    await screen.findByRole('heading', { level: 2, name: '1 policy' });
    await user.click(within(card(FINANCE)).getByRole('button', { name: `Edit ${FINANCE}` }));
    const dialog = await screen.findByRole('dialog', { name: 'Edit policy' });
    await user.click(within(dialog).getByRole('button', { name: 'Save policy' }));
    const cancel = within(dialog).getByRole('button', { name: 'Cancel' });
    await waitFor(() => expect(cancel).toHaveAttribute('aria-disabled', 'true'));
    expect(cancel).not.toBeDisabled();
    expect(cancel).toHaveAccessibleDescription('Wait for the request to finish.');
  });

  it('edits a policy from its current values and replaces it', async () => {
    const user = userEvent.setup({ delay: null });
    const { server } = renderWithApp(<AdvisorPolicies />, { route: '/advisor/policies' });
    await screen.findByRole('heading', { level: 2, name: '1 policy' });
    await user.click(within(card(FINANCE)).getByRole('button', { name: `Edit ${FINANCE}` }));
    const dialog = await screen.findByRole('dialog', { name: 'Edit policy' });
    expect(within(dialog).getByLabelText(/^Name/)).toHaveValue(FINANCE);
    expect(within(dialog).getByLabelText('Applies to')).toHaveValue('providers');
    expect(within(dialog).getByRole('checkbox', { name: 'RHOSP 17.1 — DC1' })).toBeChecked();
    expect(within(dialog).getByLabelText('When the advisor decides')).toHaveValue('always_require_approval');
    expect(within(dialog).getByLabelText('Threshold (%)')).toHaveValue(60);
    await user.clear(within(dialog).getByLabelText('Threshold (%)'));
    await user.type(within(dialog).getByLabelText('Threshold (%)'), '75');
    await user.click(within(dialog).getByRole('button', { name: 'Save policy' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(screen.getByRole('status')).toHaveTextContent(`Policy ${FINANCE} saved.`);
    expect(server.policies).toHaveLength(1);
    expect(server.policies[0]).toMatchObject({ id: 'pol-finance', threshold: 0.75, scope: { provider_ids: ['rhosp17-dc1'], plan_ids: [] } });
    expect(server.events.at(-1)).toMatchObject({ kind: 'policy.updated', actor: 'user:rina' });
  });

  it('deletes a policy after a confirmation, then the list heading takes the focus', async () => {
    const user = userEvent.setup({ delay: null });
    const { server } = renderWithApp(<AdvisorPolicies />, { route: '/advisor/policies' });
    await screen.findByRole('heading', { level: 2, name: '1 policy' });
    await user.click(within(card(FINANCE)).getByRole('button', { name: `Delete ${FINANCE}` }));
    const confirm = await screen.findByRole('alertdialog', { name: `Delete ${FINANCE}?` });
    expect(confirm).toHaveTextContent(/keeps waiting until someone approves or overrides it/);
    await user.click(within(confirm).getByRole('button', { name: 'Delete policy' }));
    const heading = await screen.findByRole('heading', { level: 2, name: '0 policies' });
    await waitFor(() => expect(heading).toHaveFocus());
    expect(server.policies).toEqual([]);
    expect(screen.getByRole('status')).toHaveTextContent(`Policy ${FINANCE} deleted.`);
    expect(screen.getByText('No policies yet')).toBeInTheDocument();
    expect(screen.getByText(/applies on its own from 60% confidence/)).toBeInTheDocument();
  });

  it('requires a distinct approver by default, and says so on the card (ADV-17)', async () => {
    const user = userEvent.setup({ delay: null });
    const { server } = renderWithApp(<AdvisorPolicies />, { route: '/advisor/policies' });
    await screen.findByRole('heading', { level: 2, name: '1 policy' });
    expect(card(FINANCE)).toHaveTextContent(/Distinct approver\s*Required/);
    await user.click(within(card(FINANCE)).getByRole('button', { name: `Edit ${FINANCE}` }));
    const dialog = await screen.findByRole('dialog', { name: 'Edit policy' });
    const box = within(dialog).getByRole('checkbox', { name: /require a distinct approver/i });
    expect(box).toBeChecked();
    await user.click(box);
    await user.click(within(dialog).getByRole('button', { name: 'Save policy' }));
    await waitFor(() => expect(server.policies[0]!.require_distinct_approver).toBe(false));
    await waitFor(() => expect(card(FINANCE)).toHaveTextContent(/Distinct approver\s*Not required/));
  });
});

describe('the advisor policy texts name a provider stored blank by its id (SDD §1 naming, §4.2, review RV22-01)', () => {
  it("the policy card's scope reads the provider's id, never its blank name", async () => {
    const server = createTestServer();
    // stored before the API refused a blank name (review RV20-11): every text names it by its id
    server.providers.find((p) => p.id === 'rhosp17-dc1')!.name = '   ';
    renderWithApp(<AdvisorPolicies />, { route: '/advisor/policies', token: 'viewer', server });
    await screen.findByRole('heading', { level: 2, name: '1 policy' });
    expect(screen.getByRole('article', { name: FINANCE })).toHaveTextContent('Plans from rhosp17-dc1');
  });

  it("the policy dialog's source-provider choice reads the provider's id, never its blank name", async () => {
    const user = userEvent.setup({ delay: null });
    const server = createTestServer();
    server.providers.find((p) => p.id === 'rhosp17-dc1')!.name = '   ';
    renderWithApp(<AdvisorPolicies />, { route: '/advisor/policies', server });
    await screen.findByRole('heading', { level: 2, name: '1 policy' });
    await user.click(within(screen.getByRole('article', { name: FINANCE })).getByRole('button', { name: `Edit ${FINANCE}` }));
    const dialog = await screen.findByRole('dialog', { name: 'Edit policy' });
    expect(within(dialog).getByRole('checkbox', { name: 'rhosp17-dc1' })).toBeChecked();
  });
});

