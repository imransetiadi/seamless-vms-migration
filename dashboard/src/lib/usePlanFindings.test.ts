import { renderHook } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { buildFixtures } from '../api/mockData';
import type { Migration } from '../api/types';
import { usePlanFindings } from './usePlanFindings';

const NOW = Date.parse('2026-10-08T12:00:00Z');
const fx = buildFixtures(NOW);
const migrations = fx.migrations.filter((m) => m.plan_id === 'plan-4f2a9c1e');
const withFindings = migrations.find((m) => m.findings.length > 0) as Migration;

describe('usePlanFindings', () => {
  it('keeps the findings and their groups when a live event changed only a migration’s progress', () => {
    expect(withFindings).toBeDefined();
    const { result, rerender } = renderHook(({ list }) => usePlanFindings(list), { initialProps: { list: migrations } });
    const before = result.current;
    expect(before.rows.length).toBeGreaterThan(0);

    rerender({ list: migrations.map((m) => (m.id === withFindings.id ? { ...m, progress_pct: 42, bytes_transferred: 1 } : m)) });

    expect(result.current.rows).toBe(before.rows);
    expect(result.current.groups).toBe(before.groups);
  });

  it('rebuilds them when a migration’s findings or VM name changed, or migrations came or went', () => {
    const { result, rerender } = renderHook(({ list }) => usePlanFindings(list), { initialProps: { list: migrations } });

    const dropped = migrations.map((m) => (m.id === withFindings.id ? { ...m, findings: [] } : m));
    rerender({ list: dropped });
    expect(result.current.rows.some((f) => f.migrationId === withFindings.id)).toBe(false);

    const renamed = dropped.map((m) => (m.id === withFindings.id ? { ...m, findings: withFindings.findings, vm: { ...m.vm, name: 'renamed-vm' } } : m));
    rerender({ list: renamed });
    expect(result.current.rows.filter((f) => f.migrationId === withFindings.id).map((f) => f.vmName)).toEqual(
      withFindings.findings.map(() => 'renamed-vm'),
    );
    expect(result.current.groups.flatMap((g) => g.items).some((i) => i.vmName === 'renamed-vm')).toBe(true);

    // a rename alone, the findings as they were
    rerender({ list: renamed.map((m) => (m.id === withFindings.id ? { ...m, vm: { ...m.vm, name: 'renamed-again' } } : m)) });
    expect(result.current.rows.filter((f) => f.migrationId === withFindings.id).map((f) => f.vmName)).toEqual(
      withFindings.findings.map(() => 'renamed-again'),
    );

    const removed = renamed.filter((m) => m.id !== withFindings.id);
    rerender({ list: removed });
    expect(result.current.rows.some((f) => f.migrationId === withFindings.id)).toBe(false);

    rerender({ list: [...removed, { ...withFindings, id: 'mig-added' }] });
    expect(result.current.rows.some((f) => f.migrationId === 'mig-added')).toBe(true);

    // the last migration goes: the others keep their places
    rerender({ list: removed });
    expect(result.current.rows.some((f) => f.migrationId === 'mig-added')).toBe(false);
  });
});
