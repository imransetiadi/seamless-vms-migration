/**
 * Cinder storage backends as the providers report them (SDD §10 `storage_backends`) and the
 * storage-handover targets they allow (SDD §7.3.1).
 */
import type { VMRef } from '../api/types';

export type StorageFamily = 'rbd' | 'netapp_nfs' | 'netapp_block' | 'other';

export interface StorageBackend {
  /** Cinder `host@backend#pool`. */
  pool: string;
  vendor: string | null;
  protocol: string | null;
  family: StorageFamily;
}

export const FAMILY_LABELS: Record<StorageFamily, string> = {
  rbd: 'Ceph RBD',
  netapp_nfs: 'NetApp ONTAP NFS',
  netapp_block: 'NetApp ONTAP iSCSI/FC',
  other: 'Other backend',
};

const FAMILIES: readonly StorageFamily[] = ['rbd', 'netapp_nfs', 'netapp_block', 'other'];
const SUPPORTED: readonly StorageFamily[] = ['rbd', 'netapp_nfs', 'netapp_block'];

const text = (value: unknown): string | null => (typeof value === 'string' && value ? value : null);

/** The `storage_backends` capability, ignoring malformed entries. */
export function storageBackends(capabilities: Record<string, unknown>): StorageBackend[] {
  const raw = capabilities.storage_backends;
  if (!Array.isArray(raw)) return [];
  return raw.flatMap((entry: unknown) => {
    if (!entry || typeof entry !== 'object') return [];
    const e = entry as Record<string, unknown>;
    const pool = text(e.pool);
    if (!pool) return [];
    const family = FAMILIES.includes(e.family as StorageFamily) ? (e.family as StorageFamily) : 'other';
    return [{ pool, vendor: text(e.vendor), protocol: text(e.protocol), family }];
  });
}

/** `host@backend#pool` → [`host@backend`, `pool` or null]. */
export function splitHost(host: string): [string, string | null] {
  const at = host.indexOf('#');
  return at < 0 ? [host, null] : [host.slice(0, at), host.slice(at + 1) || null];
}

/** "Ceph RBD (2 pools), NetApp ONTAP NFS (1 pool)" in family order. */
export function storageSummary(backends: StorageBackend[]): string {
  return FAMILIES.flatMap((family) => {
    const n = backends.filter((b) => b.family === family).length;
    return n ? [`${FAMILY_LABELS[family]} (${n} pool${n === 1 ? '' : 's'})`] : [];
  }).join(', ');
}

/**
 * `backend_map` values a volume of `family` can be handed to: NetApp backends as `host@backend`
 * (the executor resolves the export or FlexVol per volume), RBD pools as `host@backend#pool`.
 * `null` (family unknown) offers every supported target.
 */
export function handoverTargets(family: StorageFamily | null, destination: StorageBackend[]): string[] {
  const families = family === null ? SUPPORTED : SUPPORTED.includes(family) ? [family] : [];
  const targets = new Set<string>();
  for (const b of destination) {
    if (!families.includes(b.family)) continue;
    targets.add(b.family === 'rbd' ? b.pool : splitHost(b.pool)[0]);
  }
  return [...targets].sort();
}

/** Volume type → driver family of its pools on the source (null when unknown or mixed). */
export function volumeTypeFamilies(vms: VMRef[], source: StorageBackend[]): Map<string, StorageFamily | null> {
  const byPool = new Map(source.map((b) => [b.pool, b.family]));
  const seen = new Map<string, Set<StorageFamily | null>>();
  for (const vm of vms) {
    for (const disk of vm.disks) {
      if (disk.kind !== 'volume' || !disk.volume_type) continue;
      const family = disk.pool ? (byPool.get(disk.pool) ?? null) : null;
      const set = seen.get(disk.volume_type) ?? new Set();
      set.add(family);
      seen.set(disk.volume_type, set);
    }
  }
  const out = new Map<string, StorageFamily | null>();
  for (const [type, set] of [...seen.entries()].sort(([a], [b]) => a.localeCompare(b))) {
    out.set(type, set.size === 1 ? ([...set][0] ?? null) : null);
  }
  return out;
}

const exportPath = (share: string | null): string => {
  const s = share ?? '';
  const i = s.indexOf(':');
  return (i < 0 ? s : s.slice(i + 1)).replace(/\/+$/, '');
};

/**
 * Destination `host@backend#pool` for a volume, or the reason the handover refuses it — the same
 * rules as `seamless_migrate.storage.resolve_destination` (SDD §7.3.1).
 */
export function resolveDestination(
  family: StorageFamily,
  sourcePool: string | null,
  target: string,
  destination: StorageBackend[],
): { host?: string; error?: string } {
  if (!SUPPORTED.includes(family)) return { error: `unsupported storage family '${family}': handover supports Ceph RBD and NetApp ONTAP (NFS, iSCSI, FC)` };
  const [backend, wanted] = splitHost(target);
  const pools = destination.filter((b) => splitHost(b.pool)[0] === backend);
  if (pools.length === 0) {
    return { error: `the destination lists no pools for ${backend}: the ${family === 'rbd' ? 'RBD' : 'NetApp'} pool cannot be checked` };
  }
  const candidates = pools.filter((b) => b.family === family);
  if (candidates.length === 0) {
    const families = [...new Set(pools.map((b) => b.family))].sort().join(', ');
    return { error: `${backend} is ${families}, the source volume is ${family}: map the volume type to a backend of the same driver family` };
  }
  if (family === 'rbd') {
    // a pool the destination does not list is refused in step 0 (SDD §7.3.1)
    if (wanted) {
      return candidates.some((b) => b.pool === target)
        ? { host: target }
        : { error: `the destination lists no pool ${target} (RBD pools of ${backend}: ${candidates.map((b) => b.pool).join(', ')})` };
    }
    if (candidates.length === 1) return { host: candidates[0]!.pool };
    return { error: `${backend} has ${candidates.length} pools: name the pool in backend_map` };
  }
  const what = family === 'netapp_nfs' ? `export ${exportPath(sourcePool)}` : `FlexVol ${sourcePool}`;
  const match = candidates.find((b) =>
    family === 'netapp_nfs' ? exportPath(splitHost(b.pool)[1]) === exportPath(sourcePool) : splitHost(b.pool)[1] === sourcePool,
  );
  if (!match) return { error: `${backend} has no pool for the ${what} (same SVM required)` };
  if (wanted && splitHost(match.pool)[1] !== wanted) return { error: `backend_map names ${target}, but the ${what} is ${match.pool}` };
  return { host: match.pool };
}
