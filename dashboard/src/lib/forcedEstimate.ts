import type { Estimate, Migration, Plan, Strategy, SyncPass, VMRef } from '../api/types';

/*
 * The downtime a Force cutover records (SDD §5.4): the control plane's formula
 * (seamless_migrate.planning.estimator — params_for_plan, scan_seconds, final_downtime_parts,
 * first_pass_seconds, estimate_forced_downtime), ported so the Force cutover dialog shows the figure
 * the API will record, and checked against the case table the control plane generates (§16).
 * Its `estimate` too, the figures the offline mock shows (§9.1, §16), against a table of its own.
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
/** The planning defaults, for a view without a plan. */
export const ESTIMATOR_DEFAULTS: Readonly<EstimatorParams> = Object.freeze({ ...DEFAULTS });
export type PlanEstimatorSettings = Pick<Plan, 'link_bps' | 'convergence_threshold_bytes' | 'max_sync_passes' | 'estimator_overrides'>;

const INT_FIELDS = new Set(['convergence_threshold_bytes', 'max_passes', 'parallel_disks']);
/** The API's MAX_SYNC_PASSES (SDD §9.1, review RV51-07): the model never runs more, whatever a plan stored holds. */
export const MAX_SYNC_PASSES = 100;
/** The API's bounds on what the model divides by and adds (SDD §9.1, review RV51-22): a rate in B/s, a duration in s. */
export const MIN_RATE_BPS = 1;
export const MAX_DURATION_S = 1_000_000_000;

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

/** Fixed overhead of a VMware CBT delta pass (SDD §9.1: `Tk = 10 + Δk/L`). */
const VMWARE_PASS_OVERHEAD_S = 10;

/**
 * The convergence loop (`_precopy_passes`): pass k (k ≥ 2) transfers `Δk = min(D, c·T(k−1))` and runs while
 * the pass before it carried more than the threshold (Δ1 = the first pass's data) and k ≤ the pass limit —
 * the runtime rule of SDD §5.3 —, never past MAX_SYNC_PASSES. Returns the passes' durations and the final delta Δf.
 */
function precopyPasses(
  firstPassS: number,
  firstPassBytes: number,
  nextPassS: (delta: number) => number,
  diskBytes: number,
  rate: number,
  p: EstimatorParams,
): [number[], number] {
  const durations = [firstPassS];
  let last = firstPassS;
  let previous = firstPassBytes;
  const passes = Math.min(p.max_passes, MAX_SYNC_PASSES);
  while (previous > p.convergence_threshold_bytes && durations.length < passes) {
    const delta = Math.min(diskBytes, rate * last);
    last = nextPassS(delta);
    durations.push(last);
    previous = delta;
  }
  return [durations, Math.min(diskBytes, rate * last)];
}

/** One strategy's estimate as the control plane's `estimate` computes it (SDD §9.1); eligibility is the selector's. */
export function estimateStrategy(vm: DiskVM, strategy: Strategy, p: EstimatorParams, sloS: number): Omit<Estimate, 'eligible' | 'reasons'> {
  const rate = vm.change_rate_bps ?? p.change_rate_bps;
  const link = p.link_bps;
  let durations: number[] = [];
  let finalDelta = 0;
  let copy: number;
  let conversion = 0;
  switch (strategy) {
    case 'cold':
      copy = p.snapshot_s + vm.used_bytes / link;
      break;
    case 'warm': {
      const scan = scanSeconds(vm, p);
      const next = (delta: number) => p.snapshot_s + Math.max(scan, delta / link);
      [durations, finalDelta] = precopyPasses(firstPassSeconds(vm, strategy, p), vm.used_bytes, next, vm.disk_bytes, rate, p);
      copy = p.snapshot_s + Math.max(scan, finalDelta / link);
      break;
    }
    case 'storage_handover':
      copy = vm.disks.length * p.handover_per_volume_s;
      break;
    case 'vmware_cold':
      copy = vm.used_bytes / link;
      conversion = p.v2v_s;
      break;
    case 'vmware_warm': {
      const next = (delta: number) => VMWARE_PASS_OVERHEAD_S + delta / link;
      [durations, finalDelta] = precopyPasses(firstPassSeconds(vm, strategy, p), vm.used_bytes, next, vm.disk_bytes, rate, p);
      copy = finalDelta / link;
      conversion = p.v2v_inplace_s;
      break;
    }
  }
  const parts = { shutdown_s: p.shutdown_s, copy_s: copy, conversion_s: conversion, create_s: p.create_s, boot_s: p.boot_s };
  // summed in the control plane's order: shutdown, copy, conversion, create, boot
  const downtime = parts.shutdown_s + parts.copy_s + parts.conversion_s + parts.create_s + parts.boot_s;
  const precopy = durations.reduce((sum, d) => sum + d, 0);
  return {
    strategy,
    precopy_s: precopy,
    passes: durations.length,
    downtime_s: downtime,
    total_s: precopy + downtime,
    final_delta_bytes: Math.trunc(finalDelta),
    meets_slo: downtime <= sloS,
    downtime_parts: parts,
  };
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
 * A recorded pass's whole time, §9.1's `Tk` (`whole_pass_seconds`): a warm pass records the part after its
 * snapshot, so its snapshot is added back; a VMware CBT pass records the whole (review RV39-07).
 */
export function wholePassSeconds(pass: Pick<SyncPass, 'duration_s'>, strategy: Strategy, p: EstimatorParams): number {
  const recorded = pass.duration_s ?? 0;
  return strategy === 'warm' ? recorded + p.snapshot_s : recorded;
}

/**
 * What a Force cutover of `m` records now (SDD §5.4): the downtime after the running pass — as long as
 * the last pass that ended, its whole time, or, in pre-copy before any pass ended, the expected first pass.
 */
export function forcedDowntime(
  m: Pick<Migration, 'vm' | 'strategy' | 'sync_passes' | 'observed_scan_bps'>,
  plan: PlanEstimatorSettings,
): number {
  const p = paramsFor(plan, m.observed_scan_bps);
  const last = m.sync_passes.filter((s) => s.ended_at !== null).sort((a, b) => a.number - b.number).at(-1);
  return finalDowntime(m.vm, m.strategy, last ? wholePassSeconds(last, m.strategy, p) : firstPassSeconds(m.vm, m.strategy, p), p);
}
