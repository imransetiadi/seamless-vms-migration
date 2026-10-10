/** Parses "source = destination" lines (also accepts "->" and "→"). Returns null on a malformed line. */
export function parseMappings(text: string): Record<string, string> | null {
  const out: Record<string, string> = {};
  for (const raw of text.split('\n')) {
    const line = raw.trim();
    if (!line) continue;
    const parts = line.split(/\s*(?:=|->|→)\s*/);
    if (parts.length !== 2 || !parts[0] || !parts[1]) return null;
    out[parts[0]] = parts[1];
  }
  return out;
}

/** The inverse of parseMappings: one "source = destination" line per entry. */
export function formatMappings(mappings: Record<string, string>): string {
  return Object.entries(mappings)
    .map(([source, destination]) => `${source} = ${destination}`)
    .join('\n');
}
