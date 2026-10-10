import { render } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { SyncPass } from '../api/types';
import { SyncPassChart } from './SyncPassChart';

// jsdom has no layout: the chart width comes from here (0 = not measured yet)
const layout = vi.hoisted(() => ({ width: 0 }));
vi.mock('../lib/useElementWidth', () => ({ useElementWidth: () => [() => undefined, layout.width] }));

const GiB = 2 ** 30;

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

  it('says nothing about dropped passes while the history is complete', () => {
    const { container } = render(<SyncPassChart passes={[pass(1, 'full'), pass(2), pass(3)]} maxPasses={5} droppedBytes={0} />);
    expect(caption(container)).not.toHaveTextContent(/no longer listed/);
  });
});
