import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { MetricStrip } from './MetricStrip';

const metrics = (count: number) => Array.from({ length: count }, (_, i) => ({ label: `Figure ${i + 1}`, value: i }));

describe('MetricStrip', () => {
  it('lets the last of seven figures fill the rest of its row: 2 columns on a phone and 4 from sm leave no empty bordered cell', () => {
    render(<MetricStrip label="Key metrics" metrics={metrics(7)} />);
    const cells = screen.getAllByRole('group');
    expect(cells).toHaveLength(7);
    expect(cells[6]!.className.split(' ')).toEqual(expect.arrayContaining(['col-span-2', '2xl:col-span-1']));
    for (const cell of cells.slice(0, 6)) expect(cell.className).not.toContain('col-span');
  });

  it('lays eight figures out in full rows of 2 on a phone and of 4 from sm, never one row too narrow for their labels', () => {
    render(<MetricStrip label="Key metrics" metrics={metrics(8)} />);
    const classes = screen.getByRole('region', { name: 'Key metrics' }).className.split(' ');
    expect(classes).toEqual(expect.arrayContaining(['grid-cols-2', 'sm:grid-cols-4']));
    expect(classes.filter((c) => c.startsWith('2xl:') || c.startsWith('xl:') || c.startsWith('lg:'))).toEqual([]);
    for (const cell of screen.getAllByRole('group')) expect(cell.className).not.toContain('col-span');
  });

  it('spans nothing when the figures fill their rows', () => {
    render(<MetricStrip label="Key metrics" metrics={metrics(6)} />);
    for (const cell of screen.getAllByRole('group')) expect(cell.className).not.toContain('col-span');
  });
});
