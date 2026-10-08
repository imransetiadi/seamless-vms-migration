import { Gauge } from 'lucide-react';
import type { Migration, Plan } from '../api/types';
import { formatRate } from '../lib/format';
import { isWarmStrategy } from '../lib/phase';

/** Planning default of the per-stream scan rate (SDD §9.1 `EstimatorParams.scan_bps`). */
const DEFAULT_SCAN_BPS = 524_288_000;
/** Planning default of the guest write rate (SDD §9.1 `EstimatorParams.change_rate_bps`). */
const DEFAULT_CHANGE_RATE_BPS = 2_097_152;

export interface CalibrationPanelProps {
  migration: Migration;
  plan: Plan | null | undefined;
}

function Row({ label, value, source }: { label: string; value: string; source: string }) {
  return (
    <div className="flex items-baseline justify-between gap-3 py-1.5">
      <dt className="text-sm text-muted-foreground">{label}</dt>
      <dd className="text-right">
        <span className="num text-sm font-medium">{value}</span>
        <span className="ml-2 text-xs text-muted-foreground">{source}</span>
      </dd>
    </div>
  );
}

/**
 * What the downtime estimate currently rests on (SDD §9.1 "Configuration and calibration"):
 * the guest write rate and the per-stream scan rate, each either a planning default, a plan
 * override, or a value measured by a delta pass of this very migration.
 */
export function CalibrationPanel({ migration: m, plan }: CalibrationPanelProps) {
  const overrides = plan?.estimator_overrides ?? {};
  const scanSource =
    m.observed_scan_bps !== null
      ? 'measured by the last delta pass'
      : 'scan_bps' in overrides
        ? 'plan override'
        : 'planning default';
  const scan = m.observed_scan_bps ?? overrides.scan_bps ?? DEFAULT_SCAN_BPS;
  const changeSource =
    m.vm.change_rate_bps !== null
      ? m.sync_passes.some((p) => p.kind !== 'full' && p.ended_at !== null)
        ? 'measured between snapshots'
        : 'from the inventory'
      : 'change_rate_bps' in overrides
        ? 'plan override'
        : 'planning default';
  const changeRate = m.vm.change_rate_bps ?? overrides.change_rate_bps ?? DEFAULT_CHANGE_RATE_BPS;
  const calibrated = m.observed_scan_bps !== null;
  const warm = isWarmStrategy(m.strategy);

  return (
    <div>
      <p className="flex items-center gap-2 text-xs text-muted-foreground">
        <Gauge className="h-4 w-4" aria-hidden="true" />
        {calibrated
          ? 'Calibrated: the estimate uses rates measured on this VM.'
          : warm
            ? 'Not calibrated yet: the first delta pass measures the scan and write rates.'
            : 'Single-shot strategy: the estimate uses the planning defaults and plan overrides.'}
      </p>
      <dl className="mt-2 divide-y divide-border">
        <Row label="Guest write rate" value={formatRate(changeRate)} source={changeSource} />
        <Row label="Scan rate per disk stream" value={formatRate(scan)} source={scanSource} />
        {plan && <Row label="Link speed" value={formatRate(plan.link_bps)} source="plan" />}
        {'parallel_disks' in overrides && (
          <Row label="Disks scanned in parallel" value={String(overrides.parallel_disks)} source="plan override" />
        )}
      </dl>
    </div>
  );
}

export interface ResolvedMappingsProps {
  migration: Migration;
}

/** Mappings pre-flight matched automatically (SDD §7.2, §9.3): shown only when there are any. */
export function ResolvedMappings({ migration: m }: ResolvedMappingsProps) {
  const rm = m.resolved_mappings;
  const rows: Array<[string, string, string]> = [
    ...Object.entries(rm.flavors).map(([from, to]) => ['Flavor', from, to] as [string, string, string]),
    ...Object.entries(rm.networks).map(([from, to]) => ['Network', from, to] as [string, string, string]),
    ...Object.entries(rm.volume_types).map(([from, to]) => ['Volume type', from, to] as [string, string, string]),
    ...Object.entries(rm.projects).map(([from, to]) => ['Project', from, to] as [string, string, string]),
  ];
  if (rows.length === 0) return null;
  return (
    <table className="w-full text-sm">
      <caption className="sr-only">Mappings resolved automatically by pre-flight</caption>
      <thead>
        <tr className="text-left text-xs text-muted-foreground">
          <th scope="col" className="py-1 pr-3 font-medium">Kind</th>
          <th scope="col" className="py-1 pr-3 font-medium">Source</th>
          <th scope="col" className="py-1 font-medium">RHOSO</th>
        </tr>
      </thead>
      <tbody>
        {rows.map(([kind, from, to]) => (
          <tr key={`${kind}:${from}`} className="border-t border-border">
            <td className="py-1.5 pr-3">{kind}</td>
            <td className="py-1.5 pr-3 font-mono text-xs">{from}</td>
            <td className="py-1.5 font-mono text-xs">{to}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
