import { describe, expect, it } from 'vitest';
import { buildFixtures } from '../api/mockData';
import type { Migration } from '../api/types';
import { MIGRATION_SUMMARY_FIELDS, summaryOf } from './summary';

const fx = buildFixtures(Date.parse('2026-10-08T12:00:00Z'));

describe('summaryOf (SDD §4 MigrationSummary, perf audit PF-02)', () => {
  it('keeps the fields the Overview reads as the migration has them, the VM by id and name, and when its phase began', () => {
    expect(fx.migrations.length).toBeGreaterThan(5);
    for (const m of fx.migrations) {
      const summary = summaryOf(m);
      expect(Object.keys(summary).sort(), m.id).toEqual([...MIGRATION_SUMMARY_FIELDS].sort());
      expect(summary.vm).toEqual({ source_id: m.vm.source_id, name: m.vm.name });
      expect(summary.phase_since).toBe(m.phase_history.at(-1)?.at ?? m.created_at);
      for (const field of MIGRATION_SUMMARY_FIELDS) {
        if (field !== 'vm' && field !== 'phase_since') expect(summary[field], `${m.id} ${field}`).toEqual(m[field]);
      }
    }
  });

  it('takes the creation time for a migration without a phase change', () => {
    const m: Migration = { ...fx.migrations[0]!, phase_history: [] };
    expect(summaryOf(m).phase_since).toBe(m.created_at);
  });

  it("holds the API's 23 fields, no more (SDD §4)", () => {
    expect([...MIGRATION_SUMMARY_FIELDS].sort()).toEqual(
      [
        'id',
        'plan_id',
        'wave_id',
        'vm',
        'strategy',
        'phase',
        'phase_since',
        'progress_pct',
        'downtime_started_at',
        'downtime_ended_at',
        'approvals',
        'cutover_requested',
        'cutover_requested_by',
        'force_cutover',
        'force_window',
        'advisor_hold',
        'review_required',
        'review_reason',
        'error',
        'source_running_at',
        'findings',
        'created_at',
        'updated_at',
      ].sort(),
    );
  });
});
