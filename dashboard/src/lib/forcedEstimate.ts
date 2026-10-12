import type { Migration, Plan, Strategy, VMRef } from '../api/types';

/*
 * The downtime a Force cutover records (SDD §5.4): the control plane's formula
 * (seamless_migrate.planning.estimator — params_for_plan, scan_seconds, final_downtime_parts,
 * first_pass_seconds, estimate_forced_downtime), ported so the Force cutover dialog shows the figure
 * the API will record, and checked against the case table the control plane generates (§16).
 */

const GIB = 2 ** 30;

/** The estimator's defaults (`EstimatorParams`, SDD §9.1). */
const DEFAULTS = {
  link_bps: 131072000,
  scan_bps: 524288000,
  change_rate_bps: 2097152,
  shutdown_s: 60,
  boot_s: 120,
  create_s: 60,
  snapshot_s: 30,
  handover_per_volume_s: 20,
  v2v_s: 300,
  v2v_inplace_s: 120,
  convergence_threshold_bytes: 1073741824,
  max_passes: 5,
  parallel_disks: 4,
  max_aggregate_scan_bps: null as number | null,
};

export type EstimatorParams = typeof DEFAULTS;
export type PlanEstimatorSettings = Pick<Plan, 'link_bps' | 'convergence_threshold_bytes' | 'max_sync_passes' | 'estimator_overrides'>;

const INT_FIELDS = new Set(['convergence_threshold_bytes', 'max_passes', 'parallel_disks']);

/** `params_for_plan`, then the migration's calibrated scan rate as the orchestrator applies it. */
export function paramsFor(plan: PlanEstimatorSettings, observedScanBps?: number | null): EstimatorParams {
  const p: EstimatorParams = { ...DEFAULTS };
  const fields = p as Record<string, number | null>;
  for (const [key, value] of Object.entries(plan.estimator_overrides ?? {})) {
    // unknown keys are ignored (the API refuses them)
    if (!Object.prototype.hasOwnProperty.call(DEFAULTS, key)) continue;
    fields[key] = INT_FIELDS.has(key) ? Math.trunc(Number(value)) : Number(value);
  }
  // the plan's own fields win over its overrides
  p.link_bps = Number(plan.link_bps);
  p.convergence_threshold_bytes = Math.trunc(plan.convergence_threshold_bytes);
  p.max_passes = Math.trunc(plan.max_sync_passes);
  if (observedScanBps) p.scan_bps = Number(observedScanBps);
  return p;
}

type DiskVM = Pick<VMRef, 'disks' | 'disk_bytes' | 'used_bytes' | 'change_rate_bps'>;

/** Time to read and hash every disk once: `max(Dmax/S, D/min(S·P, A))`. */
export function scanSeconds(vm: DiskVM, p: EstimatorParams): number {
  if (!vm.disks.length) return 0;
  const largest = Math.max(...vm.disks.map((d) => d.size_gb)) * GIB;
  let aggregate = p.scan_bps * Math.max(1, Math.trunc(p.parallel_disks));
  if (p.max_aggregate_scan_bps !== null && p.max_aggregate_scan_bps > 0) aggregate = Math.min(aggregate, p.max_aggregate_scan_bps);
  return Math.max(largest / p.scan_bps, vm.disk_bytes / aggregate);
}

/** The expected duration of a warm migration's first pass (SDD §9.1). */
export function firstPassSeconds(vm: DiskVM, strategy: Strategy, p: EstimatorParams): number {
  if (strategy === 'warm') return p.snapshot_s + Math.max(vm.used_bytes / p.link_bps, scanSeconds(vm, p));
  if (strategy === 'vmware_warm') return vm.used_bytes / p.link_bps;
  throw new Error(`${strategy} has no pre-copy passes`);
}

/** The downtime if cut over after a pass of `lastPassS` seconds (`estimate_final_downtime`, SDD §5.3). */
export function finalDowntime(vm: DiskVM, strategy: Strategy, lastPassS: number, p: EstimatorParams): number {
  const rate = vm.change_rate_bps ?? p.change_rate_bps;
  const delta = Math.min(vm.disk_bytes, rate * Math.max(0, lastPassS));
  const [copy, conversion] =
    strategy === 'vmware_warm' ? [delta / p.link_bps, p.v2v_inplace_s] : [p.snapshot_s + Math.max(scanSeconds(vm, p), delta / p.link_bps), 0];
  // in the control plane's order: shutdown, copy, conversion, create, boot
  return p.shutdown_s + copy + conversion + p.create_s + p.boot_s;
}

/**
 * What a Force cutover of `m` records now (SDD §5.4): the downtime after the running pass — as long as
 * the last pass that ended, or, in pre-copy before any pass ended, the expected first pass.
 */
export function forcedDowntime(
  m: Pick<Migration, 'vm' | 'strategy' | 'sync_passes' | 'observed_scan_bps'>,
  plan: PlanEstimatorSettings,
): number {
  const p = paramsFor(plan, m.observed_scan_bps);
  const last = m.sync_passes.filter((s) => s.ended_at !== null).sort((a, b) => a.number - b.number).at(-1);
  return finalDowntime(m.vm, m.strategy, last ? (last.duration_s ?? 0) : firstPassSeconds(m.vm, m.strategy, p), p);
}
