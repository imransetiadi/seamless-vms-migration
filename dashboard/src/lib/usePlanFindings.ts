import { useState } from 'react';
import type { Migration, MigrationLike } from '../api/types';
import type { FindingRow } from '../components/FindingsList';
import { groupFindings, type FindingGroup } from './findings';

export interface PlanFindings {
  rows: FindingRow[];
  groups: FindingGroup[];
}

interface Built extends PlanFindings {
  /** What the rows were built from: each migration's id, VM name and findings, in list order. */
  from: Array<Pick<Migration, 'id' | 'findings'> & { name: string }>;
}

function sameSources(from: Built['from'], list: readonly MigrationLike[]): boolean {
  return (
    from.length === list.length &&
    list.every((m, i) => from[i]?.id === m.id && from[i]?.name === m.vm.name && from[i]?.findings === m.findings)
  );
}

function build(list: readonly MigrationLike[]): Built {
  const rows = list.flatMap((m) => m.findings.map((f) => ({ ...f, vmName: m.vm.name, migrationId: m.id })));
  return {
    rows,
    groups: groupFindings(rows),
    from: list.map((m) => ({ id: m.id, name: m.vm.name, findings: m.findings })),
  };
}

/**
 * A plan's findings, one row per VM finding, and their groups by code (SDD §16). The same objects come back
 * until a migration's findings or VM name change or migrations come or go: a live progress event patches a
 * migration every second, and that must not rebuild, re-sort and redraw the plan's whole findings list. Kept in
 * state, built again as the plan renders other sources (react.dev, "Adjusting some state when a prop changes").
 */
export function usePlanFindings(list: readonly MigrationLike[]): PlanFindings {
  const [built, setBuilt] = useState(() => build(list));
  if (sameSources(built.from, list)) return built;
  const rebuilt = build(list);
  setBuilt(rebuilt);
  return rebuilt;
}
