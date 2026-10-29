import { RefreshCw } from 'lucide-react';
import { useMemo } from 'react';
import {
  Bar,
  BarChart,
  CartesianGrid,
  LabelList,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
  type TooltipProps,
} from 'recharts';
import { SYNC_PASSES_LATEST, type ConvergedBy, type ForceCutover, type Strategy, type SyncPass } from '../api/types';
import { passExtentsText, tracksExtents } from '../lib/extents';
import { formatBytes, formatDateTime, formatDuration } from '../lib/format';
import { niceByteTicks } from '../lib/ticks';
import { useElementWidth } from '../lib/useElementWidth';
import { useChartColors } from '../theme/useChartColors';
import { ChartDataTable, ChartTooltipBody } from './ChartParts';
import { EmptyState } from './EmptyState';
import { TechPreviewBadge } from './TechPreviewBadge';

export interface SyncPassChartProps {
  passes: SyncPass[];
  /** `Plan.convergence_threshold_bytes` (SDD §5.3). */
  thresholdBytes?: number | null;
  maxPasses?: number | null;
  /** `Migration.sync_bytes_dropped`: what the passes dropped from a long wait's history transferred (SDD §5.4). */
  droppedBytes?: number;
  /** The pass running now, named from the phase (`runningStep`, SDD §16): the API lists only passes that ended. */
  running?: { number: number; kind: SyncPass['kind'] } | null;
  /** VMware warm passes are CBT passes: the real kit reports no bytes, and the threshold rule does not apply (SDD §5.4). */
  strategy?: Strategy;
  /** The plan's downtime SLO, for the SLO rule (SDD §5.3). */
  sloS?: number | null;
  /** `Migration.converged_by`: the rule that ended the passes (SDD §5.3). */
  convergedBy?: ConvergedBy | null;
  /** The migration's downtime estimate: once converged, the one the rule used (SDD §5.3). */
  estimateS?: number | null;
  /** `Migration.force_cutover` (SDD §5.4). */
  force?: ForceCutover | null;
}

/** A pass that reported byte counts: the real VMware kit's CBT passes report none (SDD §5.4). */
function reported(p: SyncPass): boolean {
  return p.bytes_scanned > 0 || p.bytes_changed > 0 || p.bytes_transferred > 0;
}

/** The rules that allow the cutover (SDD §5.3, §16), only once all are known: a partial list would misstate them. */
function rulesText(cbt: boolean, thresholdBytes: number | null, maxPasses: number | null, sloS: number | null): string | null {
  if (cbt) return 'CBT passes report no bytes: the estimated downtime or the pass limit decides.';
  if (thresholdBytes === null || !maxPasses || sloS === null) return null;
  return `Cutover is allowed when a pass changes less than ${formatBytes(thresholdBytes)}, when the estimated downtime fits the SLO, or after ${maxPasses} passes.`;
}

/** The rule that ended the passes, in words (SDD §5.3, §5.4). */
function convergedText(
  rule: ConvergedBy,
  last: SyncPass | undefined,
  { thresholdBytes, maxPasses, sloS, estimateS, force }: { thresholdBytes: number | null; maxPasses: number | null; sloS: number | null; estimateS: number | null; force: ForceCutover | null },
): string {
  switch (rule) {
    case 'threshold':
      return last && thresholdBytes !== null ? `Converged on the threshold: pass ${last.number} changed less than ${formatBytes(thresholdBytes)}.` : 'Converged on the threshold.';
    case 'slo':
      return estimateS !== null && sloS !== null
        ? `Converged on the SLO: the estimated downtime, ${formatDuration(estimateS)}, fits the ${formatDuration(sloS)} SLO.`
        : 'Converged on the SLO: the estimated downtime fits it.';
    case 'max_passes':
      return maxPasses ? `Converged on the pass limit: ${maxPasses} passes.` : 'Converged on the pass limit.';
    case 'force':
      return force ? `Forced by ${force.actor} before convergence: “${force.reason}”.` : 'Forced before convergence.';
  }
}

const byteTick = (v: number) => (v === 0 ? '0' : formatBytes(v).replace('.0 ', ' '));
/** Chart width beside the plot: the y axis (64 px) and the right margin (12 px). */
const PLOT_INSET = 76;
/** Room a bar needs for its value label ("35.6 MiB" at 12 px) before labels run into each other. */
const LABEL_SLOT_MIN = 60;

/**
 * The passes a long wait dropped from the history (SDD §5.4, §16) — the numbers missing below the last
 * listed pass, named as a range when they are one — and that the bytes they transferred still count.
 */
function droppedText(sorted: SyncPass[], maxPasses: number | null, droppedBytes: number): string | null {
  const gaps: [number, number][] = [];
  sorted.forEach((p, i) => {
    const previous = sorted[i - 1]?.number ?? 0;
    if (p.number > previous + 1) gaps.push([previous + 1, p.number - 1]);
  });
  const count = gaps.reduce((sum, [first, last]) => sum + last - first + 1, 0);
  const [first, last] = gaps[0] ?? [0, 0];
  if (count === 0) return null;
  const which = gaps.length > 1 ? `${count} earlier passes are` : count === 1 ? `Pass ${first} is` : `Passes ${first}–${last} are`;
  const why = maxPasses ? `: the history keeps the first ${maxPasses} passes and the latest ${SYNC_PASSES_LATEST}` : '';
  const counted = droppedBytes > 0 ? ` The ${formatBytes(droppedBytes)} ${count === 1 ? 'it' : 'they'} transferred still counts toward the total.` : '';
  return `${which} no longer listed${why}.${counted}`;
}

/**
 * Warm convergence (SDD §5.3): bytes changed per delta pass against the convergence threshold, and the
 * final sync — after the source stopped, so never a delta — as its own bar (ADV-16). The first full pass
 * copies the whole disk, so it is summarised in text and the table rather than drawn on the same linear
 * scale (it would flatten every delta bar). The text names the rules in force and the one that ended
 * the passes.
 */
export function SyncPassChart({
  passes,
  thresholdBytes = null,
  maxPasses = null,
  droppedBytes = 0,
  running = null,
  strategy,
  sloS = null,
  convergedBy = null,
  estimateS = null,
  force = null,
}: SyncPassChartProps) {
  const colors = useChartColors();
  const [measureRef, width] = useElementWidth<HTMLDivElement>();
  const narrow = width > 0 && width < 420;
  const sorted = useMemo(() => [...passes].sort((a, b) => a.number - b.number), [passes]);
  // a plan that tracks extents: each pass's use per disk goes in the data table (SDD §16, §28.5, plan task CE10)
  const extents = tracksExtents(sorted);
  const cbt = strategy === 'vmware_warm';
  const ended = sorted.filter((p) => p.ended_at !== null);
  const bars = ended.filter((p) => p.kind !== 'full');
  const data = bars.map((p) => ({
    label: `#${p.number} ${p.kind}`,
    number: p.number,
    final: p.kind === 'final',
    // one series each, so the final sync reads apart from the deltas: the other stays empty
    delta: p.kind === 'final' ? null : p.bytes_changed,
    finalSync: p.kind === 'final' ? p.bytes_changed : null,
    changed: p.bytes_changed,
    duration: p.duration_s,
  }));
  const drawable = bars.some(reported);
  // a long wait draws many bars: without room per bar the values move to the tooltip and the table
  const crowded = width > 0 && (width - PLOT_INSET) / Math.max(1, data.length) < LABEL_SLOT_MIN;

  if (sorted.length === 0 && !running) {
    return <EmptyState icon={RefreshCw} title="No sync passes yet" description="Warm migrations copy while the VM runs; every pass appears here." />;
  }

  const full = sorted.find((p) => p.kind === 'full');
  const deltas = ended.filter((p) => p.kind === 'delta');
  const last = deltas.at(-1);
  const previous = deltas.at(-2);
  const finalSync = ended.filter((p) => p.kind === 'final').at(-1);
  const parts: string[] = [];
  if (full) {
    parts.push(
      !full.ended_at
        ? 'Full copy in progress.'
        : reported(full)
          ? `Full copy: ${formatBytes(full.bytes_changed)} in ${formatDuration(full.duration_s)}.`
          : `Full copy in ${formatDuration(full.duration_s)}.`,
    );
  }
  if (last && reported(last)) {
    // the verdict is the rule's until a rule ended the passes; then the rule says it (SDD §5.3)
    const verdict =
      thresholdBytes === null || cbt || convergedBy
        ? ''
        : last.bytes_changed <= thresholdBytes
          ? ' — below the convergence threshold'
          : ' — above the convergence threshold';
    parts.push(`Last delta (pass ${last.number}) changed ${formatBytes(last.bytes_changed)}${verdict}.`);
    if (previous && previous.bytes_changed > 0) {
      const change = ((last.bytes_changed - previous.bytes_changed) / previous.bytes_changed) * 100;
      parts.push(`${change <= 0 ? 'Down' : 'Up'} ${Math.abs(Math.round(change))}% from pass ${previous.number}.`);
    }
  }
  if (finalSync) {
    const moved = reported(finalSync) ? `${formatBytes(finalSync.bytes_changed)} in ${formatDuration(finalSync.duration_s)}` : formatDuration(finalSync.duration_s);
    parts.push(`Final sync (pass ${finalSync.number}), after the source stopped: ${moved}.`);
  }
  if (running) parts.push(`Pass ${running.number} (${running.kind}) is running.`);
  const rules = rulesText(cbt, thresholdBytes, maxPasses, sloS);
  if (rules) parts.push(rules);
  if (convergedBy) {
    const beforeFinal = ended.filter((p) => p.kind !== 'final').at(-1);
    parts.push(convergedText(convergedBy, beforeFinal, { thresholdBytes, maxPasses, sloS, estimateS, force }));
  }
  const dropped = droppedText(sorted, maxPasses, droppedBytes);
  if (dropped) parts.push(dropped);
  const summary = parts.join(' ');
  const showThreshold = thresholdBytes !== null && !cbt;
  const ticks = niceByteTicks(Math.max(...data.map((d) => d.changed), showThreshold ? thresholdBytes : 0) * 1.1);
  const hasFinal = data.some((d) => d.final);
  const hasDelta = data.some((d) => !d.final);

  const renderTooltip = ({ active, payload }: TooltipProps<number, string>) => {
    const row = payload?.[0]?.payload as (typeof data)[number] | undefined;
    if (!active || !row) return null;
    return (
      <ChartTooltipBody
        title={row.final ? `Pass ${row.number}, final sync` : `Pass ${row.number}`}
        rows={[
          { key: 'changed', value: formatBytes(row.changed), label: 'changed', color: row.final ? colors.series2 : colors.series1 },
          { key: 'duration', value: formatDuration(row.duration), label: 'duration' },
        ]}
      />
    );
  };
  /** One series' value labels: a stacked series reports the stack's top for the other series' rows, so the row decides. */
  const valueLabel = (final: boolean) => (
    <LabelList
      content={(props) => {
        const row = data[Number(props.index)];
        if (!row || row.final !== final) return null;
        // Custom text: Recharts would otherwise wrap the label to the bar width ("2.7 / GiB").
        const x = Number(props.x ?? 0) + Number(props.width ?? 0) / 2;
        const y = Number(props.y ?? 0) - 6;
        return (
          <text x={x} y={y} textAnchor="middle" fill={colors.foreground} fontSize={12}>
            {formatBytes(row.changed)}
          </text>
        );
      }}
    />
  );

  return (
    <figure className="m-0 flex min-w-0 flex-col gap-2">
      <figcaption className="text-sm text-muted-foreground">{summary}</figcaption>
      {data.length === 0 ? (
        <p className="rounded-md border border-dashed border-border p-4 text-sm text-muted-foreground">Delta passes appear here after the first full copy.</p>
      ) : !drawable ? null : (
        <div ref={measureRef} className="h-56 w-full min-w-0 overflow-hidden">
          <ResponsiveContainer width="100%" height="100%" initialDimension={{ width: 320, height: 224 }}>
            <BarChart data={data} margin={{ top: 22, right: 12, bottom: 0, left: 0 }} accessibilityLayer title="Bytes changed per pass" desc={summary}>
              <CartesianGrid vertical={false} stroke={colors.grid} strokeWidth={1} />
              <XAxis dataKey="label" tick={{ fill: colors.axis, fontSize: 12 }} axisLine={{ stroke: colors.grid }} tickLine={false} />
              <YAxis
                ticks={ticks}
                domain={[0, ticks.at(-1) ?? 'auto']}
                tickFormatter={byteTick}
                tick={{ fill: colors.axis, fontSize: 12 }}
                axisLine={false}
                tickLine={false}
                width={64}
              />
              <Tooltip content={renderTooltip} cursor={{ fill: colors.grid, fillOpacity: 0.35 }} isAnimationActive={false} />
              {showThreshold && (
                <ReferenceLine
                  y={thresholdBytes}
                  stroke={colors.reference}
                  strokeDasharray="4 4"
                  strokeWidth={1.5}
                  ifOverflow="extendDomain"
                  label={{ value: `Threshold ${byteTick(thresholdBytes)}`, position: 'insideTopRight', fill: colors.axis, fontSize: 12 }}
                />
              )}
              <Bar dataKey="delta" name="Delta pass" stackId="pass" fill={colors.series1} barSize={24} radius={[4, 4, 0, 0]} isAnimationActive={false}>
                {!narrow && !crowded && valueLabel(false)}
              </Bar>
              <Bar dataKey="finalSync" name="Final sync" stackId="pass" fill={colors.series2} barSize={24} radius={[4, 4, 0, 0]} isAnimationActive={false}>
                {!narrow && !crowded && valueLabel(true)}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>
      )}
      {drawable && hasFinal && (
        <ul aria-label="Legend" className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
          {hasDelta && (
            <li className="inline-flex items-center gap-1.5">
              <span aria-hidden className="size-2.5 shrink-0 rounded-sm bg-chart-1" />
              Delta pass
            </li>
          )}
          <li className="inline-flex items-center gap-1.5">
            <span aria-hidden className="size-2.5 shrink-0 rounded-sm bg-chart-2" />
            Final sync, source stopped
          </li>
        </ul>
      )}
      {extents && (
        <p className="mt-2 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
          <TechPreviewBadge feature="extents" />
          Each pass's extent use per disk - read, checked or scanned with its reason - is in the data table.
        </p>
      )}
      <ChartDataTable
        caption="Sync passes"
        columns={['Pass', 'Kind', 'Started', 'Duration', 'Scanned', 'Changed', 'Transferred', ...(extents ? ['Extents'] : [])]}
        rows={sorted.map((p) => ({
          key: String(p.number),
          header: <span className="num">#{p.number}</span>,
          cells: [
            p.kind,
            formatDateTime(p.started_at),
            p.ended_at ? formatDuration(p.duration_s) : 'running',
            formatBytes(p.bytes_scanned),
            p.ended_at ? formatBytes(p.bytes_changed) : '—',
            formatBytes(p.bytes_transferred),
            ...(extents ? [passExtentsText(p)] : []),
          ],
        }))}
      />
    </figure>
  );
}
