import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { renderWithApp } from '../test/utils';
import Overview from './Overview';

describe('Overview page (mock data)', () => {
  it('renders KPI tiles from /stats', async () => {
    const { server } = renderWithApp(<Overview />);

    expect(await screen.findByRole('heading', { level: 1, name: 'Overview' })).toBeInTheDocument();
    const total = await screen.findByRole('group', { name: 'Migrations' });
    expect(total).toHaveTextContent(String(server.migrations.length));
    const failed = server.migrations.filter((m) => m.phase === 'failed').length;
    expect(screen.getByRole('group', { name: 'Failed' })).toHaveTextContent(String(failed));
    expect(screen.getByRole('group', { name: 'SLO compliance' })).toHaveTextContent('%');
  });

  it('puts running cutovers first, with their downtime clock and a link', async () => {
    renderWithApp(<Overview />);

    const cutovers = await screen.findByRole('region', { name: /active cutovers/i });
    const link = await within(cutovers).findByRole('link', { name: 'web-03' });
    expect(link).toHaveAttribute('href', '/migrations/mig-3c1a0f9e23');
    expect(within(cutovers).getByRole('link', { name: 'web-02' })).toBeInTheDocument();
    expect(within(cutovers).getAllByText(/downtime/i).length).toBeGreaterThan(0);
  });

  it('lists what needs a human, with the reason', async () => {
    renderWithApp(<Overview />);

    const attention = await screen.findByRole('region', { name: /needs attention/i });
    await within(attention).findByRole('link', { name: 'legacy-rhel6-app' });
    expect(attention).toHaveTextContent(/source VM is stopped/i);
    expect(within(attention).getByRole('link', { name: 'app-billing-01' })).toBeInTheDocument();
    expect(attention).toHaveTextContent(/waiting for approval/i);
    expect(within(attention).getByRole('link', { name: 'gpu-render-01' })).toBeInTheDocument();
  });

  it('shows the phase distribution with labels and counts', async () => {
    renderWithApp(<Overview />);

    const distribution = await screen.findByRole('region', { name: /phase distribution/i });
    const cutover = await within(distribution).findByText('Awaiting cutover');
    expect(cutover.closest('li')).toHaveTextContent('1');
  });

  it('offers table views of the charts', async () => {
    const user = userEvent.setup();
    renderWithApp(<Overview />);

    const throughput = await screen.findByRole('region', { name: /throughput/i });
    // The summary is the visible caption (the SVG also carries it as <desc> for assistive tech).
    expect(await within(throughput).findByText(/peak/i, { selector: 'figcaption' })).toBeInTheDocument();
    await user.click(within(throughput).getByText(/show data table/i));
    expect(within(throughput).getAllByRole('row').length).toBeGreaterThan(10);

    const downtime = screen.getByRole('region', { name: /downtime vs slo/i });
    await user.click(within(downtime).getByText(/show data table/i));
    expect(within(downtime).getByRole('rowheader', { name: 'Warm' })).toBeInTheDocument();
  });

  it('scopes the dashboard to one plan', async () => {
    const user = userEvent.setup();
    renderWithApp(<Overview />);

    const select = await screen.findByLabelText('Plan');
    await screen.findByRole('option', { name: 'VMware exit — ERP' });
    await user.selectOptions(select, 'plan-7b3e0d52');
    const total = await screen.findByRole('group', { name: 'Migrations' });
    await within(total).findByText('5');
  });
});
