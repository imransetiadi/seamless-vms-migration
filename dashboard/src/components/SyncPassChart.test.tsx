import { render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { SyncPass } from '../api/types';
import { SyncPassChart } from './SyncPassChart';

// jsdom has no layout: the chart width comes from here (0 = not measured yet)
const layout = vi.hoisted(() => ({ width: 0 }));
vi.mock('../lib/useElementWidth', () => ({ useElementWidth: () => [() => undefined, layout.width] }));

const GiB = 2 ** 30;
const MiB = 2 ** 20;

function pass(number: number, kind: SyncPass['kind'] = 'delta'): SyncPass {
  return {
    number,
    kind,
    started_at: '2026-10-08T10:00:00Z',
    ended_at: '2026-10-08T10:05:00Z',
    bytes_scanned: 100 * GiB,
    bytes_changed: GiB,
    bytes_transferred: GiB,
    duration_s: 300,
  };
}

/** A long wait's history (SDD §5.4): the full pass, deltas up to `keptFirst`, then the latest 20 from `latestFrom`. */
function history(keptFirst: number, latestFrom: number): SyncPass[] {
  const first = Array.from({ length: keptFirst - 1 }, (_, i) => pass(i + 2));
  const latest = Array.from({ length: 20 }, (_, i) => pass(latestFrom + i));
  return [pass(1, 'full'), ...first, ...latest];
}

/** The value labels above the bars (Recharts draws a LabelList in its own group). */
function barValues(container: HTMLElement): string[] {
  return [...container.querySelectorAll('.recharts-label-list text')].map((t) => t.textContent ?? '');
}

function caption(container: HTMLElement): HTMLElement {
  const element = container.querySelector('figcaption');
  if (!element) throw new Error('no figcaption');
  return element;
}

describe('SyncPassChart', () => {
  afterEach(() => {
    layout.width = 0;
  });

  it('says which passes a long wait dropped and that the bytes they transferred still count (SDD §5.4, §16)', () => {
    const { container } = render(<SyncPassChart passes={history(5, 27)} maxPasses={5} droppedBytes={21 * GiB} />);
    expect(caption(container)).toHaveTextContent(
      'Passes 6–26 are no longer listed: the history keeps the first 5 passes and the latest 20. The 21.0 GiB they transferred still counts toward the total.',
    );
  });

  it('names a single dropped pass', () => {
    const { container } = render(<SyncPassChart passes={history(5, 7)} maxPasses={5} droppedBytes={GiB} />);
    expect(caption(container)).toHaveTextContent(
      'Pass 6 is no longer listed: the history keeps the first 5 passes and the latest 20. The 1.0 GiB it transferred still counts toward the total.',
    );
  });

  it('counts dropped passes that are not one range, and leaves out what it does not know', () => {
    const passes = history(5, 27).filter((p) => p.number !== 3);
    const { container } = render(<SyncPassChart passes={passes} />);
    expect(caption(container)).toHaveTextContent('22 earlier passes are no longer listed.');
    expect(caption(container)).not.toHaveTextContent(/keeps the first|still counts/);
  });

  it('labels each bar with its value while the bars have room for it', () => {
    layout.width = 1100;
    const { container } = render(<SyncPassChart passes={[pass(1, 'full'), pass(2), pass(3), pass(4)]} />);
    expect(barValues(container)).toEqual(['1.0 GiB', '1.0 GiB', '1.0 GiB']);
  });

  it('leaves the values to the tooltip and the table when the bars are too close for labels', () => {
    layout.width = 1100;
    const { container } = render(<SyncPassChart passes={history(5, 27)} />);
    expect(barValues(container)).toEqual([]);
    // the data table still lists every pass's change
    expect(container.querySelectorAll('tbody tr')).toHaveLength(25);
  });

  it('names the running pass the page passes in, as the API lists only passes that ended (SDD §16)', () => {
    const { container } = render(<SyncPassChart passes={[pass(1, 'full'), pass(2)]} running={{ number: 3, kind: 'delta' }} />);
    expect(caption(container)).toHaveTextContent('Pass 3 (delta) is running.');
  });

  it('names the running full copy before any pass ended instead of showing an empty chart', () => {
    const { container } = render(<SyncPassChart passes={[]} running={{ number: 1, kind: 'full' }} />);
    expect(caption(container)).toHaveTextContent('Pass 1 (full) is running.');
    expect(container).toHaveTextContent('Delta passes appear here after the first full copy.');
  });

  it('draws the final sync, after the source stopped, as its own bar and never calls it a delta (SDD §16, ADV-16)', () => {
    layout.width = 1100;
    const final: SyncPass = { ...pass(3, 'final'), bytes_changed: 50 * MiB, bytes_transferred: 50 * MiB, duration_s: 81 };
    const { container } = render(<SyncPassChart passes={[pass(1, 'full'), { ...pass(2), bytes_changed: GiB / 2 }, final]} thresholdBytes={GiB} />);
    expect(caption(container)).toHaveTextContent('Last delta (pass 2) changed 512 MiB — below the convergence threshold.');
    expect(caption(container)).toHaveTextContent('Final sync (pass 3), after the source stopped: 50.0 MiB in 1m 21s.');
    expect(caption(container)).not.toHaveTextContent(/delta \(pass 3\)/i);
    // two series: the deltas and the final sync, each named in the legend
    const series = [...container.querySelectorAll('.recharts-bar')];
    expect(series).toHaveLength(2);
    expect(series[0]?.querySelectorAll('.recharts-rectangle')).toHaveLength(1);
    expect(series[1]?.querySelectorAll('.recharts-rectangle')).toHaveLength(1);
    expect(screen.getByText('Delta pass')).toBeInTheDocument();
    expect(screen.getByText('Final sync, source stopped')).toBeInTheDocument();
    expect(barValues(container)).toEqual(['512 MiB', '50.0 MiB']);
  });

  it('names only the kinds of bar it draws: a migration that converged after its full copy has no delta (ADV-16)', () => {
    layout.width = 1100;
    const final: SyncPass = { ...pass(2, 'final'), bytes_changed: 72 * MiB, bytes_transferred: 72 * MiB, duration_s: 71 };
    const { container } = render(<SyncPassChart passes={[pass(1, 'full'), final]} thresholdBytes={GiB} maxPasses={5} sloS={600} convergedBy="slo" estimateS={311} />);
    const legend = screen.getByRole('list', { name: 'Legend' });
    expect(within(legend).getAllByRole('listitem').map((li) => li.textContent)).toEqual(['Final sync, source stopped']);
    // the threshold line is one rule of three: it is labelled as the threshold, not as convergence
    expect(container.querySelector('.recharts-reference-line')).toHaveTextContent('Threshold 1 GiB');
    expect(container).not.toHaveTextContent(/converged below/i);
  });

  it('names the rules that allow the cutover (SDD §5.3, §16, ADV-16)', () => {
    const { container } = render(<SyncPassChart passes={[pass(1, 'full'), pass(2)]} thresholdBytes={GiB} maxPasses={5} sloS={600} />);
    expect(caption(container)).toHaveTextContent('Cutover is allowed when a pass changes less than 1.0 GiB, when the estimated downtime fits the SLO, or after 5 passes.');
    // a partial list would misstate the rules: nothing while the plan is unknown
    const { container: unknown } = render(<SyncPassChart passes={[pass(1, 'full'), pass(2)]} />);
    expect(caption(unknown)).not.toHaveTextContent(/cutover is allowed/i);
    for (const partial of [{ thresholdBytes: GiB }, { thresholdBytes: GiB, maxPasses: 5 }, { thresholdBytes: GiB, sloS: 600 }]) {
      const { container: some } = render(<SyncPassChart passes={[pass(1, 'full'), pass(2)]} {...partial} />);
      expect(caption(some), JSON.stringify(partial)).not.toHaveTextContent(/cutover is allowed/i);
    }
  });

  it('says that CBT passes report no bytes for a VMware warm migration, and draws no threshold (SDD §5.4, §16)', () => {
    layout.width = 1100;
    const cbt = (number: number, kind: SyncPass['kind'] = 'delta'): SyncPass => ({ ...pass(number, kind), bytes_scanned: 0, bytes_changed: 0, bytes_transferred: 0 });
    const { container } = render(<SyncPassChart strategy="vmware_warm" passes={[cbt(1, 'full'), cbt(2), cbt(3)]} thresholdBytes={GiB} maxPasses={5} sloS={600} />);
    expect(caption(container)).toHaveTextContent('CBT passes report no bytes: the estimated downtime or the pass limit decides.');
    expect(caption(container)).not.toHaveTextContent(/changes less than|changed 0|threshold/);
    // nothing to draw: the passes are listed in the table
    expect(container.querySelector('.recharts-wrapper')).toBeNull();
    expect(container.querySelectorAll('tbody tr')).toHaveLength(3);
  });

  it('draws simulated VMware passes that report bytes, without the threshold the rule does not use', () => {
    layout.width = 1100;
    const { container } = render(<SyncPassChart strategy="vmware_warm" passes={[pass(1, 'full'), pass(2)]} thresholdBytes={GiB} maxPasses={5} sloS={600} />);
    expect(container.querySelector('.recharts-wrapper')).not.toBeNull();
    expect(container.querySelector('.recharts-reference-line')).toBeNull();
    expect(caption(container)).toHaveTextContent('Last delta (pass 2) changed 1.0 GiB.');
  });

  it.each([
    ['threshold', 'Converged on the threshold: pass 2 changed less than 1.0 GiB.'],
    ['slo', 'Converged on the SLO: the estimated downtime, 5m 12s, fits the 10m 00s SLO.'],
    ['max_passes', 'Converged on the pass limit: 5 passes.'],
    ['force', 'Forced by rina before convergence: “The change window closes in 20 minutes”.'],
  ] as const)('names the rule that allowed the cutover: %s (SDD §5.3, ADV-16)', (rule, text) => {
    const force = { actor: 'rina', at: '2026-10-08T10:06:00Z', reason: 'The change window closes in 20 minutes', delta_bytes: GiB / 2, estimated_downtime_s: 312 };
    const { container } = render(
      <SyncPassChart
        passes={[pass(1, 'full'), { ...pass(2), bytes_changed: GiB / 2 }, pass(3, 'final')]}
        thresholdBytes={GiB}
        maxPasses={5}
        sloS={600}
        convergedBy={rule}
        estimateS={312}
        force={rule === 'force' ? force : null}
      />,
    );
    expect(caption(container)).toHaveTextContent(text);
  });

  it('says nothing about dropped passes while the history is complete', () => {
    const { container } = render(<SyncPassChart passes={[pass(1, 'full'), pass(2), pass(3)]} maxPasses={5} droppedBytes={0} />);
    expect(caption(container)).not.toHaveTextContent(/no longer listed/);
  });
});
