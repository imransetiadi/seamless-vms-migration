import { render, screen, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import { FINDING_CATALOG } from '../api/findingCatalog';
import { groupFindings } from '../lib/findings';
import { FindingGroupsList, FindingsList, type FindingRow } from './FindingsList';

/** A finding of `code` as the catalog words it (SDD §9.3), on the VM `vmName`. */
function finding(code: string, vmName: string): FindingRow {
  const entry = FINDING_CATALOG[code]!;
  return { code, severity: entry.severity, message: `${vmName}: ${code}`, remediation: entry.remediation, strategies: entry.strategies, vmName, migrationId: `mig-${vmName}` };
}

describe('the findings lists', () => {
  it('mark each network finding with the Technology Preview badge, and no other (SDD §16, §23.9, plan task N12c)', () => {
    const rows = [finding('SRC_VM_LB_MEMBER', 'web-01'), finding('NET_DNS_TTL_LONG', 'web-02'), finding('SRC_VM_ERROR_STATE', 'web-03')];
    const item = (code: string) => screen.getByText(code, { selector: 'code' }).closest('li')!;
    const marked = () => {
      for (const code of ['SRC_VM_LB_MEMBER', 'NET_DNS_TTL_LONG']) expect(within(item(code)).getByText('Technology Preview')).toBeInTheDocument();
      expect(within(item('SRC_VM_ERROR_STATE')).queryByText('Technology Preview')).not.toBeInTheDocument();
    };
    // a migration's findings
    const { unmount } = render(
      <MemoryRouter>
        <FindingsList findings={rows} />
      </MemoryRouter>,
    );
    marked();
    unmount();
    // a plan's, grouped by code
    render(
      <MemoryRouter>
        <FindingGroupsList groups={groupFindings(rows)} />
      </MemoryRouter>,
    );
    marked();
  });

  // a credential finding is about one project, which its message names (SDD §16, §27.7, plan task TC6)
  it('names the project a credential finding is about', () => {
    // the plan's two project credentials, both expiring: made-up Keystone project ids, the API's words
    const projects = { source: 'tenant-a (6c1f0e2a9b8d47c3a5e4f1d2c3b4a596)', destination: 'tenant-c (d4e5f6a7b8c94d0e8f1a2b3c4d5e6f70)' };
    const words = (side: 'source' | 'destination') =>
      `The credential of project ${projects[side]} at ${side === 'source' ? 'RHOSP 17.1' : 'RHOSO 18.0'}, the plan's ${side} project, expires at 2026-11-01T12:00:00Z, within 7 days.`;
    const rows = ['web-01', 'web-02'].flatMap((vm) =>
      (['source', 'destination'] as const).map((side) => ({ ...finding('PROJECT_CREDENTIAL_EXPIRING', vm), message: words(side) })),
    );
    // a plan's list: one entry per project, its message shown above its VMs - never hidden in the VMs' details
    const { unmount } = render(
      <MemoryRouter>
        <FindingGroupsList groups={groupFindings(rows)} />
      </MemoryRouter>,
    );
    expect(screen.getAllByText('PROJECT_CREDENTIAL_EXPIRING', { selector: 'code' })).toHaveLength(2);
    for (const side of ['source', 'destination'] as const) {
      const entry = screen.getByText(words(side)).closest('li')!;
      expect(screen.getByText(words(side))).toBeVisible();
      expect(within(entry).getByText('2 VMs')).toBeInTheDocument();
      expect(within(entry).getAllByRole('link').map((link) => link.textContent)).toEqual(['web-01', 'web-02']);
    }
    unmount();
    // the unrestricted and the role findings alike: one entry per project, the source's first
    for (const code of ['PROJECT_CREDENTIAL_UNRESTRICTED', 'PROJECT_CREDENTIAL_ROLE_MISSING']) {
      const named = ['web-02', 'web-01'].flatMap((vm) =>
        (['destination', 'source'] as const).map((side) => ({ ...finding(code, vm), message: `${code}: project ${projects[side]}` })),
      );
      expect(groupFindings(named).map((g) => [g.code, g.items[0]?.message, g.items.map((i) => i.vmName), g.sameMessage])).toEqual([
        [code, `${code}: project ${projects.source}`, ['web-01', 'web-02'], true],
        [code, `${code}: project ${projects.destination}`, ['web-01', 'web-02'], true],
      ]);
    }
    // a migration's list: each credential's finding with its own message
    render(
      <MemoryRouter>
        <FindingsList findings={rows.slice(0, 2)} />
      </MemoryRouter>,
    );
    expect(screen.getByText(words('source'))).toBeVisible();
    expect(screen.getByText(words('destination'))).toBeVisible();
  });

  it("say in a network badge's detail that the network cut-over has not yet run on the lab's clouds (SDD §16, §23.10, FR-47)", () => {
    render(
      <MemoryRouter>
        <FindingsList findings={[finding('SRC_VM_LB_MEMBER', 'web-01')]} />
      </MemoryRouter>,
    );
    // word for word: no network element claims a lab validation until the LAB-NET cases pass
    expect(screen.getByRole('button', { name: 'Technology Preview' })).toHaveAccessibleDescription(
      "Technology Preview, validated in simulation: the network cut-over has not yet run on the lab's clouds.",
    );
  });
});

describe('the extent findings (SDD §16, §28.9, plan task CE10)', () => {
  it("mark each extent finding with the Technology Preview badge, whose detail says the tracking has not yet run on the lab's Ceph clusters", () => {
    render(
      <MemoryRouter>
        <FindingsList findings={[finding('EXTENT_TRACKING_MISSED', 'db-01'), finding('CEPH_ACCESS_MISSING', 'db-02'), finding('EXTENT_TRACKING_PARTIAL', 'db-03')]} />
      </MemoryRouter>,
    );
    const badges = screen.getAllByRole('button', { name: 'Technology Preview' });
    expect(badges).toHaveLength(3);
    for (const badge of badges) {
      expect(badge).toHaveAccessibleDescription("Technology Preview, validated in simulation: changed-extent tracking has not yet run on the lab's Ceph clusters.");
    }
  });
});
