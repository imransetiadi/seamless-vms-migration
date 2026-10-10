import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { createTestServer, renderWithApp } from '../test/utils';
import AdvisorAudit from './AdvisorAudit';

const readBlob = (blob: Blob) =>
  new Promise<string>((resolve) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.readAsText(blob);
  });

async function auditTable() {
  return screen.findByRole('table', { name: /advisor audit events/i });
}

describe('Advisor audit page (SDD §14.5, ADV-05)', () => {
  it('lists who decided what, when and with which model version, newest first', async () => {
    renderWithApp(<AdvisorAudit />, { route: '/advisor/audit', token: 'viewer' });
    const table = await auditTable();
    expect(within(table).getAllByRole('columnheader').map((h) => h.textContent)).toEqual(['Time', 'Action', 'Actor', 'Target', 'Model version']);
    const rows = within(table).getAllByRole('row').slice(1);
    // ten seeded recommendations (created + a decision each, two also a person's) and one policy
    expect(rows).toHaveLength(23);
    const times = rows.map((r) => Date.parse(r.querySelector('time')!.getAttribute('datetime')!));
    expect(times).toEqual([...times].sort((a, b) => b - a));
    const kept = rows.find((r) => within(r).queryByText('Decision: below threshold, default kept') && within(r).queryByRole('link', { name: 'static-cdn-01' }))!;
    expect(kept).toHaveTextContent('policy:global-default');
    expect(kept).toHaveTextContent('jev-1.13.0');
    expect(within(kept).getByRole('link', { name: 'static-cdn-01' })).toHaveAttribute('href', '/advisor/recommendations/rec-d4e6f8a09001');
    // a policy change names the policy and leads to the policies page
    const policy = rows.find((r) => within(r).queryByText('Policy created'))!;
    expect(within(policy).getByRole('link', { name: 'Finance — approval required' })).toHaveAttribute('href', '/advisor/policies');
    expect(policy).toHaveTextContent('user:rina');
  });

  it('lists a Force cutover with its approver, its reason and the migration it forced (SDD §14.5, ADV-16)', async () => {
    const server = createTestServer();
    const warm = server.migrations.find((m) => m.phase === 'syncing')!;
    const forced = server.handle('POST', `/migrations/${warm.id}/force-cutover`, new URLSearchParams(), { reason: 'the window closes in ten minutes' }, 'approver');
    expect(forced.status).toBe(200);
    renderWithApp(<AdvisorAudit />, { route: '/advisor/audit', token: 'viewer', server });
    const table = await auditTable();
    const row = within(table)
      .getAllByRole('row')
      .find((r) => within(r).queryByText('Cutover forced'))!;
    expect(row).toHaveTextContent('the window closes in ten minutes');
    expect(row).toHaveTextContent('sari');
    expect(within(row).getByRole('link', { name: warm.vm.name })).toHaveAttribute('href', `/migrations/${warm.id}`);
  });

  it('says how many audit events its filters show from a live region already on the page, also when none match (SDD §16)', async () => {
    const user = userEvent.setup({ delay: null });
    renderWithApp(<AdvisorAudit />, { route: '/advisor/audit', token: 'viewer' });
    const table = await auditTable();
    const total = within(table).getAllByRole('row').length - 1;
    const status = screen.getByText(`Showing ${total} of ${total} audit events`);
    expect(status).toHaveAttribute('role', 'status');
    await user.selectOptions(screen.getByLabelText('Action'), 'decision.fallback');
    expect(status).toHaveTextContent(`Showing 1 of ${total} audit events`);
    await user.type(screen.getByLabelText('Search'), 'zzzz-no-match');
    expect(status).toHaveTextContent(`Showing 0 of ${total} audit events`);
    expect(screen.getByText('No audit events match')).toBeInTheDocument();
  });

  it('filters by action, actor and text', async () => {
    const user = userEvent.setup({ delay: null });
    renderWithApp(<AdvisorAudit />, { route: '/advisor/audit', token: 'viewer' });
    const table = await auditTable();
    const shown = () => within(table).getAllByRole('row').slice(1);
    await user.selectOptions(screen.getByLabelText('Action'), 'decision.fallback');
    expect(shown()).toHaveLength(1);
    expect(shown()[0]).toHaveTextContent('system:fallback');
    await user.selectOptions(screen.getByLabelText('Action'), 'all');
    await user.selectOptions(screen.getByLabelText('Actor'), 'system:advisor');
    expect(shown()).toHaveLength(10);
    await user.selectOptions(screen.getByLabelText('Actor'), 'all');
    await user.type(screen.getByLabelText('Search'), 'web-02');
    expect(shown()).toHaveLength(2);
  });

  it('downloads the shown events as CSV and as JSON', async () => {
    const user = userEvent.setup({ delay: null });
    const blobs: Blob[] = [];
    const create = vi.spyOn(URL, 'createObjectURL').mockImplementation((b) => {
      blobs.push(b as Blob);
      return 'blob:audit';
    });
    const revoke = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined);
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);
    try {
      renderWithApp(<AdvisorAudit />, { route: '/advisor/audit', token: 'viewer' });
      await auditTable();
      await user.selectOptions(screen.getByLabelText('Actor'), 'system:advisor');
      await user.click(screen.getByRole('button', { name: /download csv/i }));
      const csv = (await readBlob(blobs[0]!)).trim().split('\r\n');
      expect(csv[0]).toBe('time,actor,action,target,model version');
      expect(csv).toHaveLength(11);
      expect(csv.slice(1).every((line) => line.includes(',system:advisor,recommendation.created,rec-'))).toBe(true);
      await user.click(screen.getByRole('button', { name: /download json/i }));
      const json = JSON.parse(await readBlob(blobs[1]!)) as Array<{ kind: string }>;
      expect(json).toHaveLength(10);
      expect(new Set(json.map((e) => e.kind))).toEqual(new Set(['recommendation.created']));
    } finally {
      create.mockRestore();
      revoke.mockRestore();
      click.mockRestore();
    }
  });

  it('says the audit trail could not be loaded instead of that it is empty', async () => {
    const server = createTestServer();
    const handle = server.handle.bind(server);
    server.handle = (method, path, query, body, token) =>
      method === 'GET' && path === '/events' && query.get('audit') === 'true'
        ? { status: 400, body: { error: { code: 'bad_request', message: 'refused for the test' } } }
        : handle(method, path, query, body, token);
    renderWithApp(<AdvisorAudit />, { route: '/advisor/audit', token: 'viewer', server });
    expect(await screen.findByRole('alert')).toHaveTextContent(/audit trail could not be loaded/i);
    expect(screen.queryByText(/no audit events yet/i)).not.toBeInTheDocument();
    // the downloads say why there is nothing to download
    expect(screen.getByRole('button', { name: /download csv/i })).toHaveAccessibleDescription('The audit trail could not be loaded.');
  });
});
