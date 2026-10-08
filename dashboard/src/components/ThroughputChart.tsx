import { Activity } from 'lucide-react';
import { useMemo } from 'react';
import { Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis, type TooltipProps } from 'recharts';
import type { ThroughputPoint } from '../api/types';
import { formatBytes, formatRate, formatTime } from '../lib/format';
import { niceByteTicks } from '../lib/ticks';
import { useChartColors } from '../theme/useChartColors';
import { ChartDataTable, ChartTooltipBody } from './ChartParts';
import { EmptyState } from './EmptyState';

function hhmm(ms: number): string {
  return formatTime(ms).slice(0, 5);
}

/** Aggregate transfer rate per minute (Stats.throughput_series): one series, so no legend box. */
export function ThroughputChart({ series }: { series: ThroughputPoint[] }) {
  const colors = useChartColors();
  const data = useMemo(
    () => series.map((p) => ({ t: Date.parse(p.ts), bps: p.bps })).filter((p) => Number.isFinite(p.t)),
    [series],
  );

  if (data.length === 0) {
    return <EmptyState icon={Activity} title="No throughput yet" description="Transfer rates appear once a pre-copy, sync or cutover runs." />;
  }

  const current = data.at(-1)?.bps ?? 0;
  const peak = Math.max(...data.map((p) => p.bps));
  const average = data.reduce((sum, p) => sum + p.bps, 0) / data.length;
  const summary = `Now ${formatRate(current)}, peak ${formatRate(peak)}, average ${formatRate(average)} over the last ${data.length} minutes.`;
  const yTicks = niceByteTicks(peak);
  const yTick = (v: number) => (v === 0 ? '0' : `${formatBytes(v).replace('.0 ', ' ')}/s`);

  const renderTooltip = ({ active, payload, label }: TooltipProps<number, string>) => {
    if (!active || !payload?.length) return null;
    return (
      <ChartTooltipBody
        title={hhmm(Number(label))}
        rows={[{ key: 'bps', value: formatRate(Number(payload[0]?.value ?? 0)), label: 'throughput', color: colors.series1 }]}
      />
    );
  };

  return (
    <figure className="m-0 flex min-w-0 flex-col gap-2">
      <figcaption className="text-sm text-muted-foreground">{summary}</figcaption>
      <div className="h-56 w-full min-w-0 overflow-hidden">
        <ResponsiveContainer width="100%" height="100%" initialDimension={{ width: 320, height: 224 }}>
          <AreaChart
            data={data}
            margin={{ top: 8, right: 8, bottom: 0, left: 0 }}
            accessibilityLayer
            title="Throughput per minute"
            desc={summary}
          >
            <CartesianGrid vertical={false} stroke={colors.grid} strokeWidth={1} />
            <XAxis
              dataKey="t"
              type="number"
              scale="time"
              domain={['dataMin', 'dataMax']}
              tickFormatter={hhmm}
              tick={{ fill: colors.axis, fontSize: 12 }}
              axisLine={{ stroke: colors.grid }}
              tickLine={false}
              minTickGap={40}
            />
            <YAxis
              ticks={yTicks}
              domain={[0, yTicks.at(-1) ?? 'auto']}
              tickFormatter={yTick}
              tick={{ fill: colors.axis, fontSize: 12 }}
              axisLine={false}
              tickLine={false}
              width={76}
            />
            <Tooltip content={renderTooltip} cursor={{ stroke: colors.axis, strokeWidth: 1 }} isAnimationActive={false} />
            <Area
              type="monotone"
              dataKey="bps"
              name="Throughput"
              stroke={colors.series1}
              strokeWidth={2}
              fill={colors.series1}
              fillOpacity={0.1}
              dot={false}
              activeDot={{ r: 4, stroke: colors.surface, strokeWidth: 2, fill: colors.series1 }}
              isAnimationActive={false}
            />
          </AreaChart>
        </ResponsiveContainer>
      </div>
      <ChartDataTable
        caption="Throughput per minute"
        columns={['Minute', 'Throughput']}
        rows={[...data].reverse().map((p) => ({ key: String(p.t), header: <span className="num">{hhmm(p.t)}</span>, cells: [formatRate(p.bps)] }))}
      />
    </figure>
  );
}
