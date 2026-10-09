import type { Migration } from '../api/types';

/**
 * What a migration has transferred in passes that ended, plus the bytes of passes dropped from its
 * history (SDD §4.2, §5.4). The API lists only passes that ended; the mock also lists the running one,
 * whose bytes count through the progress event's `bytes_done` instead.
 */
export function endedPassBytes(m: Pick<Migration, 'sync_passes' | 'sync_bytes_dropped'>): number {
  return m.sync_bytes_dropped + m.sync_passes.reduce((sum, p) => sum + (p.ended_at === null ? 0 : p.bytes_transferred), 0);
}
