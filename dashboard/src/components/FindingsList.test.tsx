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
