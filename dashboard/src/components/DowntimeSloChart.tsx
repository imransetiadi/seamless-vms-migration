import { CircleCheck, Timer, TriangleAlert } from 'lucide-react';
import { useMemo } from 'react';
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  LabelList,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
  type TooltipProps,
} from 'recharts';
import { STRATEGIES, type Strategy } from '../api/types';
import { formatDuration } from '../lib/format';
import { STRATEGY_LABELS } from '../lib/status';
import { formatDurationTick, niceDurationTicks } from '../lib/ticks';
import { useElementWidth } from '../lib/useElementWidth';
import { useChartColors } from '../theme/useChartColors';
import { ChartDataTable, ChartTooltipBody } from './ChartParts';
import { EmptyState } from './EmptyState';

export interface DowntimeSloChartProps {
  /** Average actual downtime per strategy (Stats.downtime_by_strategy). */
  byStrategy: Partial<Record<Strategy, number>>;
  /** The SLO the bars are judged against (seconds). */
  sloS: number | null;
  /** Explains which SLO is shown, e.g. "Strictest SLO". */
  sloLabel?: string;
}

/**
 * Average downtime per strategy against the downtime SLO: bars over the SLO switch to the danger
 * status colour and say so in text; the SLO is a neutral dashed reference line with a label.
 */
export function DowntimeSloChart({ byStrategy, sloS, sloLabel = 'SLO' }: DowntimeSloChartProps) {
  const colors = useChartColors();
  const [measureRef, width] = useElementWidth<HTMLDivElement>();
  const narrow = width > 0 && width < 480;
  const data = useMemo(
    () =>
      STRATEGIES.filter((s) => typeof byStrategy[s] === 'number').map((s) => {
        const downtime = byStrategy[s] ?? 0;
        const over = sloS !== null && downtime > sloS;
        return { strategy: s, label: STRATEGY_LABELS[s], downtime, over, text: `${formatDuration(downtime)}${over ? ' · over SLO' : ''}` };
      }),
    [byStrategy, sloS],
  );

  if (data.length === 0) {
    return <EmptyState icon={Timer} title="No completed cutovers yet" description="Downtime per strategy appears after the first verified cutover." />;
  }

  const ticks = niceDurationTicks(Math.max(...data.map((d) => d.downtime), sloS ?? 0) * 1.05);
  const overCount = data.filter((d) => d.over).length;
  const summary =
    sloS === null
      ? `Average downtime for ${data.length} strategies.`
      : `${overCount === 0 ? 'Every strategy is within' : `${overCount} of ${data.length} strategies exceed`} the ${sloLabel} of ${formatDuration(sloS)}.`;

  const renderTooltip = ({ active, payload }: TooltipProps<number, string>) => {
    const row = payload?.[0]?.payload as (typeof data)[number] | undefined;
    if (!active || !row) return null;
    return (
      <ChartTooltipBody
        title={row.label}
        rows={[
          { key: 'avg', value: formatDuration(row.downtime), label: 'average downtime', color: row.over ? colors.danger : colors.series1 },
          ...(sloS !== null ? [{ key: 'slo', value: formatDuration(sloS), label: row.over ? 'SLO (exceeded)' : 'SLO (met)' }] : []),
        ]}
      />
    );
  };

  return (
    <figure className="m-0 flex min-w-0 flex-col gap-2">
      <figcaption className="text-sm text-muted-foreground">{summary}</figcaption>
      <ul aria-label="Legend" className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
        <li className="flex items-center gap-1.5">
          <span aria-hidden className="size-2.5 rounded-xs" style={{ background: colors.series1 }} />
          <CircleCheck aria-hidden className="size-3.5 text-status-success" />
          Within SLO
        </li>
        <li className="flex items-center gap-1.5">
          <span aria-hidden className="size-2.5 rounded-xs" style={{ background: colors.danger }} />
          <TriangleAlert aria-hidden className="size-3.5 text-status-danger" />
          Over SLO
        </li>
        {sloS !== null && (
          <li className="flex items-center gap-1.5">
            <span aria-hidden className="h-0 w-4 border-t-2 border-dashed" style={{ borderColor: colors.reference }} />
            {sloLabel} {formatDuration(sloS)}
          </li>
        )}
      </ul>
      <div ref={measureRef} className="w-full min-w-0 overflow-hidden" style={{ height: data.length * 44 + 56 }}>
        <ResponsiveContainer width="100%" height="100%" initialDimension={{ width: 320, height: 200 }}>
          <BarChart
            data={data}
            layout="vertical"
            margin={{ top: 18, right: narrow ? 12 : 120, bottom: 0, left: 0 }}
            accessibilityLayer
            title="Average downtime per strategy"
            desc={summary}
          >
            <CartesianGrid horizontal={false} stroke={colors.grid} strokeWidth={1} />
            <XAxis
              type="number"
              domain={[0, ticks.at(-1) ?? 'auto']}
              ticks={ticks}
              tickFormatter={formatDurationTick}
              tick={{ fill: colors.axis, fontSize: 12 }}
              axisLine={{ stroke: colors.grid }}
              tickLine={false}
            />
            <YAxis
              type="category"
              dataKey="label"
              width={narrow ? 96 : 124}
              tick={{ fill: colors.axis, fontSize: 12 }}
              axisLine={false}
              tickLine={false}
            />
            <Tooltip content={renderTooltip} cursor={{ fill: colors.grid, fillOpacity: 0.35 }} isAnimationActive={false} />
            {sloS !== null && (
              <ReferenceLine
                x={sloS}
                stroke={colors.reference}
                strokeDasharray="4 4"
                strokeWidth={1.5}
                ifOverflow="extendDomain"
                label={{ value: `${sloLabel} ${formatDuration(sloS)}`, position: 'top', fill: colors.axis, fontSize: 12 }}
              />
            )}
            <Bar dataKey="downtime" name="Average downtime" barSize={20} radius={[0, 4, 4, 0]} isAnimationActive={false}>
              {data.map((d) => (
                <Cell key={d.strategy} fill={d.over ? colors.danger : colors.series1} />
              ))}
              {!narrow && <LabelList dataKey="text" position="right" fill={colors.foreground} fontSize={12} />}
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      </div>
      <ChartDataTable
        caption="Average downtime per strategy"
        columns={['Strategy', 'Average downtime', 'SLO', 'Status']}
        rows={data.map((d) => ({
          key: d.strategy,
          header: d.label,
          cells: [
            formatDuration(d.downtime),
            sloS === null ? '—' : formatDuration(sloS),
            d.over ? (
              <span className="inline-flex items-center gap-1 text-status-danger">
                <TriangleAlert aria-hidden className="size-3.5" />
                Over SLO
              </span>
            ) : (
              <span className="inline-flex items-center gap-1 text-status-success">
                <CircleCheck aria-hidden className="size-3.5" />
                Within SLO
              </span>
            ),
          ],
        }))}
      />
    </figure>
  );
}
