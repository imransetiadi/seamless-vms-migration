/** A source and its destination are separated by "=", "->" or "→" (SDD §16 plan form). */
const SEPARATOR = /^\s*(?:=|->|→)/;
/** A name written in double quotes: it holds a separator, a quote, a backslash or edge spaces. */
const NEEDS_QUOTES = /=|->|→|["\\]|^\s|\s$|^$/;

function quote(name: string): string {
  return NEEDS_QUOTES.test(name) ? `"${name.replace(/["\\]/g, (c) => `\\${c}`)}"` : name;
}

/** The name at `start` - quoted, or plain up to a separator or the end - and where it ends. */
function readName(line: string, start: number): [string, number] | null {
  let i = start;
  while (i < line.length && /\s/.test(line[i]!)) i += 1;
  if (line[i] === '"') {
    let name = '';
    for (i += 1; i < line.length; i += 1) {
      const c = line[i]!;
      if (c === '"') return [name, i + 1];
      if (c === '\\') {
        i += 1;
        if (i >= line.length) return null;
      }
      name += line[i];
    }
    return null; // the quote never closes
  }
  let end = i;
  while (end < line.length && !SEPARATOR.test(line.slice(end))) end += 1;
  const name = line.slice(i, end).trim();
  return name && !name.includes('"') ? [name, end] : null;
}

/**
 * Parses "source = destination" lines (also "->" and "→"); a name holding one of those, a double
 * quote or edge spaces is written in double quotes (`\"` and `\\` inside). Returns null on a line it
 * cannot read unambiguously, so every mapping the API accepts round-trips through the form.
 */
export function parseMappings(text: string): Record<string, string> | null {
  // own keys only: a source named "__proto__" is a mapping like any other, never Object.prototype's setter (review RV23-01)
  const out = Object.create(null) as Record<string, string>;
  for (const raw of text.split('\n')) {
    const line = raw.trim();
    if (!line) continue;
    const source = readName(line, 0);
    if (!source) return null;
    const separator = SEPARATOR.exec(line.slice(source[1]));
    if (!separator) return null;
    const destination = readName(line, source[1] + separator[0].length);
    if (!destination || line.slice(destination[1]).trim()) return null;
    out[source[0]] = destination[0];
  }
  return out;
}

/** The inverse of parseMappings: one "source = destination" line per entry, quoting where needed. */
export function formatMappings(mappings: Record<string, string>): string {
  return Object.entries(mappings)
    .map(([source, destination]) => `${quote(source)} = ${quote(destination)}`)
    .join('\n');
}
