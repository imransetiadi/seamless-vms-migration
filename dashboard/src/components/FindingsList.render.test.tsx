import { render } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { buildFixtures } from '../api/mockData';
import { groupFindings } from '../lib/findings';
import { FindingGroupsList } from './FindingsList';

// counts the groups a render draws: each group draws one SeverityBadge (the real one); a plain counter, as the
// config's restoreMocks would reset a vi.fn between tests
const drawn = vi.hoisted(() => ({ groups: 0 }));
vi.mock('./StatusBadge', async (importOriginal) => {
  const original = await importOriginal<typeof import('./StatusBadge')>();
  return {
    ...original,
    SeverityBadge: (props: Parameters<typeof original.SeverityBadge>[0]) => {
      drawn.groups += 1;
      return original.SeverityBadge(props);
    },
  };
});

const fx = buildFixtures(Date.parse('2026-10-08T12:00:00Z'));
const groups = groupFindings(
  fx.migrations.flatMap((m) => m.findings.map((f) => ({ ...f, vmName: m.vm.name, migrationId: m.id }))),
);

function Page({ tick }: { tick: number }) {
  // what the plan page does on a live event: it renders again, passing the groups usePlanFindings kept
  return (
    <MemoryRouter>
      <p>{tick}</p>
      <FindingGroupsList groups={groups} />
    </MemoryRouter>
  );
}

describe('FindingGroupsList rendering', () => {
  beforeEach(() => {
    drawn.groups = 0;
  });

  it('is not drawn again when the page renders with the same groups', () => {
    expect(groups.length).toBeGreaterThan(1);
    const { rerender } = render(<Page tick={1} />);
    expect(drawn.groups).toBe(groups.length);
    drawn.groups = 0;

    rerender(<Page tick={2} />);

    expect(drawn.groups).toBe(0);
  });
});
