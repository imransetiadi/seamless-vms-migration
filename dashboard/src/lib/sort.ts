export type SortDirection = 'ascending' | 'descending';

export interface SortState<K extends string> {
  key: K;
  direction: SortDirection;
}

/** Next sort state when a header is activated: new column → ascending, same column → flip. */
export function nextSort<K extends string>(current: SortState<K> | null, key: K): SortState<K> {
  if (current?.key === key) return { key, direction: current.direction === 'ascending' ? 'descending' : 'ascending' };
  return { key, direction: 'ascending' };
}

/** Stable sort by a key extractor; strings compare naturally ("web-2" < "web-10"). */
export function sortBy<T, K extends string>(
  items: readonly T[],
  sort: SortState<K> | null,
  value: (item: T, key: K) => string | number | null,
): T[] {
  if (!sort) return [...items];
  const factor = sort.direction === 'ascending' ? 1 : -1;
  return items
    .map((item, index) => ({ item, index, v: value(item, sort.key) }))
    .sort((a, b) => {
      if (a.v === b.v) return a.index - b.index;
      if (a.v === null) return 1;
      if (b.v === null) return -1;
      const cmp =
        typeof a.v === 'number' && typeof b.v === 'number'
          ? a.v - b.v
          : String(a.v).localeCompare(String(b.v), undefined, { numeric: true, sensitivity: 'base' });
      return cmp * factor || a.index - b.index;
    })
    .map((entry) => entry.item);
}
