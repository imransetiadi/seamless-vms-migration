import { act, fireEvent, screen } from '@testing-library/react';
import { useState } from 'react';
import { describe, expect, it } from 'vitest';
import type { Migration, Role } from '../api/types';
import { createTestServer, renderWithApp } from '../test/utils';
import { DemoSourcePower } from './DemoSourcePower';

/** The migration page's mount of the button (MigrationDetail: `key={m.id}`), its migration updated as the live stream does. */
function mount(start: Migration) {
  const server = createTestServer();
  const operator: Role = 'operator';
  let update: (m: Migration) => void = () => undefined;
  function Page() {
    const [m, setM] = useState<Migration>(start);
    update = setM;
    return <DemoSourcePower key={m.id} migration={m} role={operator} />;
  }
  renderWithApp(<Page />, { server, token: 'operator' });
  return { update: (m: Migration) => act(() => update(m)), label: () => screen.getByRole('button', { name: /source VM \(demo\)/ }).textContent };
}

describe('DemoSourcePower', () => {
  const completedWithStoppedSource = () => createTestServer().migrations.find((m) => m.phase === 'completed' && !m.source_running_at)!;

  it('follows the migration once it changes after a press: rolled back, retried and cut over again, its source is stopped (SDD §16, audit DB-02)', async () => {
    const done = completedWithStoppedSource();
    const page = mount(done);
    fireEvent.click(await screen.findByRole('button', { name: 'Power on the source VM (demo)' }));
    await screen.findByRole('button', { name: 'Power off the source VM (demo)' });
    // the press holds until the watch answers: nothing about the migration changed yet
    page.update({ ...done, updated_at: '2026-10-08T12:00:30Z' });
    expect(page.label()).toBe('Power off the source VM (demo)');
    // the rollback starts the source and drops the demo power (SDD §8.1), the retry's new cutover stops it again
    page.update({ ...done, phase: 'rolling_back' });
    page.update({ ...done, phase: 'rolled_back' });
    page.update({ ...done, phase: 'completed', source_running_at: null, attempts: done.attempts + 1 });
    expect(page.label()).toBe('Power on the source VM (demo)');
  });

  it("follows the source's recorded power after a press: the watch records it running, another tab powers it off (SDD §16, audit DB-02)", async () => {
    const done = completedWithStoppedSource();
    const page = mount(done);
    fireEvent.click(await screen.findByRole('button', { name: 'Power on the source VM (demo)' }));
    await screen.findByRole('button', { name: 'Power off the source VM (demo)' });
    page.update({ ...done, source_running_at: '2026-10-08T12:01:00Z' });
    expect(page.label()).toBe('Power off the source VM (demo)');
    // migration.source_stopped from another tab's press: the alert goes, and so does this tab's "Power off"
    page.update({ ...done, source_running_at: null });
    expect(page.label()).toBe('Power on the source VM (demo)');
  });

  it('follows a refetch that lands on the next attempt directly - a tab in the background missed the rollback (SDD §16, audit DB-02)', async () => {
    const done = completedWithStoppedSource();
    const page = mount(done);
    fireEvent.click(await screen.findByRole('button', { name: 'Power on the source VM (demo)' }));
    await screen.findByRole('button', { name: 'Power off the source VM (demo)' });
    page.update({ ...done, attempts: done.attempts + 1 });
    expect(page.label()).toBe('Power on the source VM (demo)');
  });
});
