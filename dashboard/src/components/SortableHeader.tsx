import { ArrowDown, ArrowUp, ChevronsUpDown } from 'lucide-react';
import { cn } from '../lib/cn';
import type { SortState } from '../lib/sort';

export interface SortableHeaderProps<K extends string> {
  label: string;
  sortKey: K;
  sort: SortState<K> | null;
  onSort: (key: K) => void;
  className?: string;
  align?: 'left' | 'right';
}

/** Column header with a sort button; `aria-sort` reports the current order (WCAG). */
export function SortableHeader<K extends string>({ label, sortKey, sort, onSort, className, align = 'left' }: SortableHeaderProps<K>) {
  const active = sort?.key === sortKey;
  const Icon = !active ? ChevronsUpDown : sort.direction === 'ascending' ? ArrowUp : ArrowDown;
  return (
    <th scope="col" aria-sort={active ? sort.direction : 'none'} className={cn(align === 'right' && 'text-right', className)}>
      <button
        type="button"
        onClick={() => onSort(sortKey)}
        className={cn(
          'inline-flex min-h-6 cursor-pointer items-center gap-1 rounded uppercase tracking-wide transition-colors duration-200 hover:text-foreground',
          active && 'text-foreground',
        )}
      >
        {label}
        <Icon aria-hidden className={cn('size-3.5 shrink-0', !active && 'opacity-60')} />
      </button>
    </th>
  );
}
