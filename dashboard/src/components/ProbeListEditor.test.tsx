import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { describe, expect, it } from 'vitest';
import { parseDrafts, type ProbeDraft } from '../lib/probes';
import { ProbeListEditor } from './ProbeListEditor';

function Harness({ initial = [] }: { initial?: ProbeDraft[] }) {
  const [drafts, setDrafts] = useState<ProbeDraft[]>(initial);
  return (
    <>
      <ProbeListEditor legend="Application probes" drafts={drafts} onChange={setDrafts} hint="Run after the boot checks." />
      <output data-testid="parsed">{JSON.stringify(parseDrafts(drafts))}</output>
    </>
  );
}

const parsed = () => JSON.parse(screen.getByTestId('parsed').textContent ?? '{}') as ReturnType<typeof parseDrafts>;

describe('ProbeListEditor (SDD §7.5, §16, ADV-18)', () => {
  it('adds a probe, edits it, and gives TCP no path or status', async () => {
    const user = userEvent.setup({ delay: null });
    render(<Harness />);
    const group = screen.getByRole('group', { name: 'Application probes' });
    expect(group).toHaveTextContent('No application probe: verification checks the boot only.');
    await user.click(within(group).getByRole('button', { name: 'Add probe' }));
    await user.type(within(group).getByRole('spinbutton', { name: /^probe 1 port$/i }), '5432');
    expect(within(group).queryByRole('textbox', { name: /^probe 1 path$/i })).not.toBeInTheDocument();
    expect(parsed()).toEqual({ probes: [{ kind: 'tcp', port: 5432, path: '/', expect_status: null }], problems: [] });

    await user.selectOptions(within(group).getByRole('combobox', { name: /^probe 1 kind$/i }), 'https');
    const path = within(group).getByRole('textbox', { name: /^probe 1 path$/i });
    await user.clear(path);
    await user.type(path, '/health');
    await user.type(within(group).getByRole('spinbutton', { name: /^probe 1 expected status$/i }), '204');
    expect(parsed().probes).toEqual([{ kind: 'https', port: 5432, path: '/health', expect_status: 204 }]);
    // back to TCP: no path or status goes with it
    await user.selectOptions(within(group).getByRole('combobox', { name: /^probe 1 kind$/i }), 'tcp');
    expect(parsed().probes).toEqual([{ kind: 'tcp', port: 5432, path: '/', expect_status: null }]);
    await user.selectOptions(within(group).getByRole('combobox', { name: /^probe 1 kind$/i }), 'https');

    await user.click(within(group).getByRole('button', { name: 'Remove probe 1' }));
    expect(parsed()).toEqual({ probes: [], problems: [] });
  });

  it('says what is wrong with a probe, as the API would refuse it', async () => {
    const user = userEvent.setup({ delay: null });
    render(<Harness initial={[{ kind: 'http', port: '70000', path: 'health', expectStatus: '' }]} />);
    expect(parsed().problems).toEqual(['Probe 1: use a port from 1 to 65535.', "Probe 1: the path starts with '/' (at most 200 characters)."]);
    await user.clear(screen.getByRole('spinbutton', { name: /^probe 1 port$/i }));
    expect(parsed().problems[0]).toBe('Probe 1: use a port from 1 to 65535.');
  });

  it('moves focus to the list legend when Remove probe takes its probe away (SDD §16)', async () => {
    const user = userEvent.setup({ delay: null });
    render(<Harness initial={[{ kind: 'tcp', port: '5432', path: '/', expectStatus: '' }, { kind: 'tcp', port: '6432', path: '/', expectStatus: '' }]} />);
    const group = screen.getByRole('group', { name: 'Application probes' });
    const remove = within(group).getByRole('button', { name: 'Remove probe 2' });
    await user.click(remove);
    expect(remove).not.toBeInTheDocument();
    expect(within(group).getByText('Application probes', { selector: 'legend' })).toHaveFocus();
  });

  it('stops at ten probes, saying why', async () => {
    const user = userEvent.setup({ delay: null });
    const ten = Array.from({ length: 10 }, (_, i): ProbeDraft => ({ kind: 'tcp', port: String(1000 + i), path: '/', expectStatus: '' }));
    render(<Harness initial={ten} />);
    const add = screen.getByRole('button', { name: 'Add probe' });
    expect(add).toHaveAttribute('aria-disabled', 'true');
    expect(add).toHaveAccessibleDescription('At most 10 probes for one VM.');
    await user.click(add);
    expect(parsed().probes).toHaveLength(10);
  });
});
