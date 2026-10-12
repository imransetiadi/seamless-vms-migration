import { describe, expect, it } from 'vitest';
import { AUDIT_EVENT_KINDS, type Event } from '../api/types';
import { AUDIT_CSV_HEADER, auditActionLabel, auditCsv } from './audit';

function event(partial: Partial<Event>): Event {
  return { seq: 1, ts: '2026-10-10T08:00:00Z', kind: 'decision.auto_applied', plan_id: 'plan-1', migration_id: 'mig-1', actor: 'policy:global-default', message: '', data: { target_id: 'rec-000000000001', model_version: 'jev-1.13.0' }, ...partial };
}

describe('advisor audit export (SDD §14.5, ADV-05)', () => {
  it('writes the columns the spec names: time, actor, action, target, model version', () => {
    const csv = auditCsv([event({}), event({ seq: 2, kind: 'decision.fallback', actor: 'system:fallback', data: { target_id: 'rec-000000000002', model_version: 'rules' } })]);
    expect(csv.split('\r\n')).toEqual([
      AUDIT_CSV_HEADER,
      '2026-10-10T08:00:00Z,policy:global-default,decision.auto_applied,rec-000000000001,jev-1.13.0',
      '2026-10-10T08:00:00Z,system:fallback,decision.fallback,rec-000000000002,rules',
      '',
    ]);
    expect(AUDIT_CSV_HEADER).toBe('time,actor,action,target,model version');
  });

  it('quotes what CSV needs quoted and keeps a spreadsheet from running a cell as a formula', () => {
    const csv = auditCsv([event({ actor: 'user:"ops", team', data: { target_id: '=HYPERLINK("x")', model_version: '+1' } })]);
    expect(csv.split('\r\n')[1]).toBe(`2026-10-10T08:00:00Z,"user:""ops"", team",decision.auto_applied,"'=HYPERLINK(""x"")",'+1`);
  });

  it('leaves a field the event lacks empty', () => {
    expect(auditCsv([event({ data: {} })]).split('\r\n')[1]).toBe('2026-10-10T08:00:00Z,policy:global-default,decision.auto_applied,,');
  });

  it('names every audit action in words', () => {
    for (const kind of AUDIT_EVENT_KINDS) {
      expect(auditActionLabel(kind), kind).not.toBe(kind);
      // a recommendation, its decision, a policy, or a Force cutover (SDD §14.5)
      expect(auditActionLabel(kind), kind).toMatch(/^(Recommendation|Decision|Policy|Cutover)/);
    }
    expect(auditActionLabel('plan.updated')).toBe('plan.updated');
  });
});
