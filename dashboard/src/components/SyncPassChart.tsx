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
import type { SyncPass } from '../api/types';
import { formatBytes, formatDateTime, formatDuration } from '../lib/format';
import { niceByteTicks } from '../lib/ticks';
import { useElementWidth } from '../lib/useElementWidth';
import { useChartColors } from '../theme/useChartColors';
import { ChartDataTable, ChartTooltipBody } from './ChartParts';
import { EmptyState } from './EmptyState';

export interface SyncPassChartProps {
  passes: SyncPass[];
  /** `Plan.convergence_threshold_bytes` (SDD §5.3). */
  thresholdBytes?: number | null;
  maxPasses?: number | null;
}

const byteTick = (v: number) => (v === 0 ? '0' : formatBytes(v).replace('.0 ', ' '));

/**
 * Warm convergence (SDD §5.3): bytes changed per delta/final pass against the convergence threshold.
 * The first full pass copies the whole disk, so it is summarised in text and the table rather than
 * drawn on the same linear scale (it would flatten every delta bar).
 */
export function SyncPassChart({ passes, thresholdBytes = null, maxPasses = null }: SyncPassChartProps) {
  const colors = useChartColors();
  const [measureRef, width] = useElementWidth<HTMLDivElement>();
  const narrow = width > 0 && width < 420;
  const sorted = useMemo(() => [...passes].sort((a, b) => a.number - b.number), [passes]);
  const deltas = sorted.filter((p) => p.kind !== 'full' && p.ended_at !== null);
  const data = deltas.map((p) => ({ label: `#${p.number} ${p.kind}`, number: p.number, changed: p.bytes_changed, duration: p.duration_s }));

  if (sorted.length === 0) {
    return <EmptyState icon={RefreshCw} title="No sync passes yet" description="Warm migrations copy while the VM runs; every pass appears here." />;
  }

  const full = sorted.find((p) => p.kind === 'full');
  const open = sorted.find((p) => p.ended_at === null);
  const last = deltas.at(-1);
  const previous = deltas.at(-2);
  const parts: string[] = [];
  if (full) parts.push(full.ended_at ? `Full copy: ${formatBytes(full.bytes_changed)} in ${formatDuration(full.duration_s)}.` : 'Full copy in progress.');
  if (last) {
    const verdict = thresholdBytes === null ? '' : last.bytes_changed <= thresholdBytes ? ' — below the convergence threshold' : ' — above the convergence threshold';
    parts.push(`Last delta (pass ${last.number}) changed ${formatBytes(last.bytes_changed)}${verdict}.`);
    if (previous && previous.bytes_changed > 0) {
      const change = ((last.bytes_changed - previous.bytes_changed) / previous.bytes_changed) * 100;
      parts.push(`${change <= 0 ? 'Down' : 'Up'} ${Math.abs(Math.round(change))}% from pass ${previous.number}.`);
    }
  }
  if (open) parts.push(`Pass ${open.number} (${open.kind}) is running.`);
  if (maxPasses) parts.push(`Cutover is allowed after at most ${maxPasses} passes.`);
  const summary = parts.join(' ');
  const ticks = niceByteTicks(Math.max(...data.map((d) => d.changed), thresholdBytes ?? 0) * 1.1);

  const renderTooltip = ({ active, payload }: TooltipProps<number, string>) => {
    const row = payload?.[0]?.payload as (typeof data)[number] | undefined;
    if (!active || !row) return null;
    return (
      <ChartTooltipBody
        title={`Pass ${row.number}`}
        rows={[
          { key: 'changed', value: formatBytes(row.changed), label: 'changed', color: colors.series1 },
          { key: 'duration', value: formatDuration(row.duration), label: 'duration' },
        ]}
      />
    );
  };

  return (
    <figure className="m-0 flex min-w-0 flex-col gap-2">
      <figcaption className="text-sm text-muted-foreground">{summary}</figcaption>
      {data.length === 0 ? (
        <p className="rounded-md border border-dashed border-border p-4 text-sm text-muted-foreground">Delta passes appear here after the first full copy.</p>
      ) : (
        <div ref={measureRef} className="h-56 w-full min-w-0 overflow-hidden">
          <ResponsiveContainer width="100%" height="100%" initialDimension={{ width: 320, height: 224 }}>
            <BarChart data={data} margin={{ top: 22, right: 12, bottom: 0, left: 0 }} accessibilityLayer title="Bytes changed per delta pass" desc={summary}>
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
              {thresholdBytes !== null && (
                <ReferenceLine
                  y={thresholdBytes}
                  stroke={colors.reference}
                  strokeDasharray="4 4"
                  strokeWidth={1.5}
                  ifOverflow="extendDomain"
                  label={{ value: `Converged below ${byteTick(thresholdBytes)}`, position: 'insideTopRight', fill: colors.axis, fontSize: 12 }}
                />
              )}
              <Bar dataKey="changed" name="Bytes changed" fill={colors.series1} barSize={24} radius={[4, 4, 0, 0]} isAnimationActive={false}>
                {!narrow && (
                  <LabelList
                    dataKey="changed"
                    content={(props) => {
                      // Custom text: Recharts would otherwise wrap the label to the bar width ("2.7 / GiB").
                      const x = Number(props.x ?? 0) + Number(props.width ?? 0) / 2;
                      const y = Number(props.y ?? 0) - 6;
                      return (
                        <text x={x} y={y} textAnchor="middle" fill={colors.foreground} fontSize={12}>
                          {formatBytes(Number(props.value ?? 0))}
                        </text>
                      );
                    }}
                  />
                )}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>
      )}
      <ChartDataTable
        caption="Sync passes"
        columns={['Pass', 'Kind', 'Started', 'Duration', 'Scanned', 'Changed', 'Transferred']}
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
          ],
        }))}
      />
    </figure>
  );
}
