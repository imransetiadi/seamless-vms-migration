import { fireEvent, render, waitFor } from '@testing-library/react';
import type { ReactElement } from 'react';
import { describe, expect, it, vi } from 'vitest';
import type { SyncPass } from '../api/types';
import { DowntimeSloChart } from './DowntimeSloChart';
import { SyncPassChart } from './SyncPassChart';

// jsdom has no layout: the charts take their width from here
vi.mock('../lib/useElementWidth', () => ({ useElementWidth: () => [() => undefined, 1100] }));

const GiB = 2 ** 30;

function pass(number: number): SyncPass {
  return {
    number,
    kind: number === 1 ? 'full' : 'delta',
    started_at: '2026-10-08T10:00:00Z',
    ended_at: '2026-10-08T10:05:00Z',
    bytes_scanned: 100 * GiB,
    bytes_changed: GiB,
    bytes_transferred: GiB,
    duration_s: 300,
  };
}

/** Hovers the chart and returns the band Recharts draws behind the hovered bar. */
async function hoverCursor(chart: ReactElement): Promise<Element> {
  const { container } = render(chart);
  const wrapper = container.querySelector<HTMLElement>('.recharts-wrapper');
  if (!wrapper) throw new Error('no chart drawn');
  // jsdom lays nothing out: give the chart its drawn size, so a pointer position maps onto a bar
  const width = Number.parseFloat(wrapper.style.width) || 1100;
  const height = Number.parseFloat(wrapper.style.height) || 224;
  vi.spyOn(wrapper, 'getBoundingClientRect').mockReturnValue(DOMRect.fromRect({ x: 0, y: 0, width, height }));
  Object.defineProperties(wrapper, { offsetWidth: { value: width }, offsetHeight: { value: height } });
  fireEvent.mouseMove(wrapper, { clientX: 400, clientY: 100 });
  return waitFor(() => {
    const cursor = container.querySelector('.recharts-tooltip-cursor');
    if (!cursor) throw new Error('no tooltip cursor drawn on hover');
    return cursor;
  });
}

describe('bar chart hover cursor (SDD §16)', () => {
  // The hovered bar's band is a fill alone: an outline around it reads as a selection the chart does not have.
  it('draws the sync pass chart cursor without an outline', async () => {
    expect(await hoverCursor(<SyncPassChart passes={[pass(1), pass(2), pass(3)]} maxPasses={5} droppedBytes={0} />)).toHaveAttribute(
      'stroke',
      'none',
    );
  });

  it('draws the downtime chart cursor without an outline', async () => {
    expect(await hoverCursor(<DowntimeSloChart byStrategy={{ cold: 418, warm: 217 }} sloS={600} />)).toHaveAttribute('stroke', 'none');
  });
});
