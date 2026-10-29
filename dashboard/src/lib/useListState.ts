import { useContext, useEffect, useMemo, useState, type SetStateAction } from 'react';
import { UNSAFE_LocationContext, UNSAFE_NavigationContext } from 'react-router-dom';

const sessions = new WeakMap<object, Map<string, Record<string, unknown>>>();

/** Per-visit list context keyed by router history entry (SDD §16); dialog selectors stay local. */
export function useListState(key?: string) {
  const context = useContext(UNSAFE_LocationContext);
  const navigation = useContext(UNSAFE_NavigationContext);
  const [visits] = useState(() => {
    const owner = navigation?.navigator;
    const existing = owner && sessions.get(owner);
    if (existing) return existing;
    const created = new Map<string, Record<string, unknown>>();
    if (owner) sessions.set(owner, created);
    return created;
  });
  const entry = key && context ? `${context.location.key}:${context.location.pathname}:${key}` : undefined;
  const pathname = context?.location.pathname ?? '';
  const address = pathname + (context?.location.search ?? '');
  const [stored, setStored] = useState(() => ({ entry, address, pathname, values: entry ? visits.get(entry) ?? {} : {} }));
  const changed = stored.entry !== entry;
  const inherited = stored.pathname === pathname && (context?.navigationType === 'REPLACE' || stored.address === address);
  const values = useMemo(() => changed ? (entry && visits.get(entry)) || (inherited ? stored.values : {}) : stored.values, [changed, entry, visits, inherited, stored.values]);
  if (changed) setStored({ entry, address, pathname, values });
  useEffect(() => {
    if (!entry || changed) return;
    visits.set(entry, values);
    // ponytail: retain 100 list visits; use sessionStorage if reload restoration becomes required.
    if (visits.size > 100) visits.delete(visits.keys().next().value!);
  }, [entry, values, visits, changed]);
  return {
    field<T>(name: string, fallback: T, valid?: (value: unknown) => boolean): [T, (next: SetStateAction<T>) => void] {
      const raw = values[name];
      const accepted = valid ? valid(raw) : typeof raw === typeof fallback && (typeof raw !== 'number' || Number.isFinite(raw) && raw > 0);
      const value = raw !== undefined && accepted ? raw as T : fallback;
      return [value, (next) => setStored(prev => {
        if (prev.entry !== entry) return prev;
        const before = prev.values[name] === undefined ? fallback : prev.values[name] as T;
        return { ...prev, values: { ...prev.values, [name]: typeof next === 'function' ? (next as (v: T) => T)(before) : next } };
      })];
    },
  };
}

/** Validate history sort values before using them to index rows. */
export function validListSort(keys: readonly string[]) {
  return (value: unknown): boolean => value === null || typeof value === 'object' && value !== null &&
    'key' in value && keys.includes(String(value.key)) && 'direction' in value && (value.direction === 'ascending' || value.direction === 'descending');
}
