import { Gauge } from 'lucide-react';
import type { Migration, Plan } from '../api/types';
import { ESTIMATOR_DEFAULTS, paramsFor } from '../lib/forcedEstimate';
import { formatDuration, formatRate } from '../lib/format';
import { isWarmStrategy } from '../lib/phase';

/** The estimator overrides listed besides the rates and the disks in parallel, when the plan sets them (SDD §16). */
const OTHER_OVERRIDES = [
  { key: 'max_aggregate_scan_bps', label: 'Aggregate scan cap', show: formatRate },
  { key: 'shutdown_s', label: 'Source shutdown', show: formatDuration },
  { key: 'snapshot_s', label: 'Snapshot', show: formatDuration },
  { key: 'create_s', label: 'Volume create', show: formatDuration },
  { key: 'boot_s', label: 'Boot', show: formatDuration },
  { key: 'handover_per_volume_s', label: 'Handover per volume', show: formatDuration },
  { key: 'v2v_s', label: 'Conversion', show: formatDuration },
  { key: 'v2v_inplace_s', label: 'In-place conversion', show: formatDuration },
] as const;

export interface CalibrationPanelProps {
  migration: Migration;
  plan: Plan | null | undefined;
}

function Row({ label, value, source }: { label: string; value: string; source: string }) {
  return (
    <div className="flex min-w-0 flex-wrap items-baseline justify-between gap-3 py-1.5">
      <dt className="text-sm text-muted-foreground">{label}</dt>
      <dd className="min-w-0 wrap-anywhere text-right">
        <span className="num text-sm font-medium">{value}</span>
        <span className="ml-2 text-xs text-muted-foreground">{source}</span>
      </dd>
    </div>
  );
}

/**
 * What the downtime estimate currently rests on (SDD §9.1 "Configuration and calibration"):
 * the guest write rate and the per-stream scan rate, each either a planning default, a plan
 * override, or a value measured by a delta pass of this very migration; the link speed; and the
 * plan's other estimator overrides — the disks in parallel shown on the OpenStack warm path in any case (§16).
 */
export function CalibrationPanel({ migration: m, plan }: CalibrationPanelProps) {
  const overrides = plan?.estimator_overrides ?? {};
  // what the estimate uses: the plan's parameters, the planning defaults without a plan (SDD §9.1)
  const p = plan ? paramsFor(plan) : ESTIMATOR_DEFAULTS;
  // a delta pass with byte counts calibrated the write rate (the VMware kit reports none)
  const measuredDelta = m.sync_passes.some(
    (p) => p.kind !== 'full' && p.ended_at !== null && (p.bytes_changed > 0 || p.bytes_scanned > 0),
  );
  const cbt = m.strategy === 'vmware_warm';
  const scanSource =
    m.observed_scan_bps !== null
      ? 'measured by the last delta pass'
      : 'scan_bps' in overrides
        ? 'plan override'
        : 'planning default';
  const scan = m.observed_scan_bps ?? p.scan_bps;
  const changeSource =
    m.vm.change_rate_bps !== null
      ? measuredDelta
        ? 'measured between snapshots'
        : 'from the inventory'
      : 'change_rate_bps' in overrides
        ? 'plan override'
        : 'planning default';
  const changeRate = m.vm.change_rate_bps ?? p.change_rate_bps;
  // the scan rate is calibrated for the OpenStack warm path only; CBT passes have no scan term
  const calibrated = m.observed_scan_bps !== null || (cbt && measuredDelta);
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
        {cbt ? (
          <Row label="Scan rate per disk stream" value="not needed" source="changed-block tracking knows the delta" />
        ) : (
          <Row label="Scan rate per disk stream" value={formatRate(scan)} source={scanSource} />
        )}
        {plan && <Row label="Link speed" value={formatRate(plan.link_bps)} source="plan" />}
        {(m.strategy === 'warm' || 'parallel_disks' in overrides) && (
          <Row
            label="Disks scanned in parallel"
            value={String(p.parallel_disks)}
            source={'parallel_disks' in overrides ? 'plan override' : 'planning default'}
          />
        )}
        {OTHER_OVERRIDES.filter(({ key }) => key in overrides).map(({ key, label, show }) => (
          <Row key={key} label={label} value={show(p[key])} source="plan override" />
        ))}
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
          <th scope="col" className="py-1 font-medium">Destination</th>
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
