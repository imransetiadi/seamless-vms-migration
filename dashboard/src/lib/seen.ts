/**
 * The sequence numbers of the persisted events a page has shown, so a replayed event is shown once (SDD §16). At most
 * `max` are kept: the oldest go first, and anything at or below the highest one that went counts as seen - the stream
 * sends persisted events in sequence order, and resumes after the last one it sent, so only a replay comes that low.
 */
export function createSeenSeqs(max: number) {
  const seqs = new Set<number>();
  let floor = 0;
  return {
    /** True when `seq` is new, and remembers it; false for one already seen. */
    add(seq: number): boolean {
      if (seq <= floor || seqs.has(seq)) return false;
      seqs.add(seq);
      if (seqs.size > max) {
        // a Set iterates in insertion order: the oldest first
        for (const old of seqs) {
          seqs.delete(old);
          floor = Math.max(floor, old);
          if (seqs.size <= max) break;
        }
      }
      return true;
    },
    get size(): number {
      return seqs.size;
    },
  };
}
