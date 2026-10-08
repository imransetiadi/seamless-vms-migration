import { Table2 } from 'lucide-react';
import type { ReactNode } from 'react';
import { ScrollRegion } from './ScrollRegion';

export interface ChartTooltipRow {
  key: string;
  value: string;
  label: string;
  color?: string;
}

/** Tooltip body: values lead (strong), labels follow; rows keyed by a short stroke of the series colour. */
export function ChartTooltipBody({ title, rows }: { title: string; rows: ChartTooltipRow[] }) {
  return (
    <div className="min-w-32 rounded-md border border-border bg-card px-2.5 py-2 text-xs shadow-lg">
      <p className="mb-1 text-muted-foreground">{title}</p>
      <ul className="flex flex-col gap-0.5">
        {rows.map((row) => (
          <li key={row.key} className="flex items-center gap-2">
            {row.color && <span aria-hidden className="h-0.5 w-3 shrink-0 rounded-full" style={{ background: row.color }} />}
            <span className="num font-semibold text-foreground">{row.value}</span>
            <span className="text-muted-foreground">{row.label}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

export interface ChartDataTableProps {
  caption: string;
  columns: string[];
  rows: Array<{ key: string; header: ReactNode; cells: ReactNode[] }>;
}

/** The table twin of a chart (WCAG): every value is reachable without hovering. */
export function ChartDataTable({ caption, columns, rows }: ChartDataTableProps) {
  return (
    <details className="group mt-2 text-sm">
      <summary className="inline-flex min-h-8 cursor-pointer select-none items-center gap-1.5 rounded-sm text-muted-foreground transition-colors duration-200 hover:text-foreground">
        <Table2 aria-hidden className="size-3.5" />
        <span className="group-open:hidden">Show data table</span>
        <span className="hidden group-open:inline">Hide data table</span>
      </summary>
      <ScrollRegion label={caption} className="mt-2 max-h-72 overflow-y-auto rounded-md border border-border">
        <table className="data-table">
          <caption className="sr-only">{caption}</caption>
          <thead>
            <tr>
              {columns.map((column) => (
                <th key={column} scope="col">
                  {column}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.key}>
                <th scope="row">{row.header}</th>
                {row.cells.map((cell, index) => (
                  <td key={index} className="num">
                    {cell}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </ScrollRegion>
    </details>
  );
}
