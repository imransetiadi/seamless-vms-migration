import { replaceEqualDeep } from '@tanstack/react-query';

/** Whether `value` holds an own "__proto__" key at any depth: data keyed by a tenant's names can, a network named
 * "__proto__" in a plan's mappings among them (SDD §9.2). */
function holdsProtoKey(value: unknown, depth = 0): boolean {
  if (value === null || typeof value !== 'object' || depth > 500) return false;
  if (Array.isArray(value)) return value.some((item) => holdsProtoKey(item, depth + 1));
  return Object.hasOwn(value, '__proto__') || Object.values(value).some((item) => holdsProtoKey(item, depth + 1));
}

/**
 * The query cache's structural sharing (SDD §16): React Query's replaceEqualDeep keeps what a refetch did not change by
 * reference, but it copies by assignment, which hands an own "__proto__" key to Object.prototype's setter and drops it -
 * data holding one is kept as the API sent it (review RV24-02).
 */
export function shareStructure(oldData: unknown, newData: unknown): unknown {
  return holdsProtoKey(newData) ? newData : replaceEqualDeep(oldData, newData);
}
