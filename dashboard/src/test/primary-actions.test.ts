import { describe, expect, it } from 'vitest';

// Every component and page, as written.
const sources = import.meta.glob(['../**/*.tsx', '!../**/*.test.tsx'], { query: '?raw', import: 'default', eager: true }) as Record<
  string,
  string
>;

/** Each `<Button …>` opening tag (braces balanced) with the line it starts on. */
function buttonTags(text: string): { line: number; tag: string }[] {
  const tags: { line: number; tag: string }[] = [];
  for (const match of text.matchAll(/<Button\b/g)) {
    let depth = 0;
    let end = match.index;
    for (; end < text.length; end += 1) {
      const c = text[end];
      if (c === '{') depth += 1;
      else if (c === '}') depth -= 1;
      else if (c === '>' && depth === 0) break;
    }
    tags.push({ line: text.slice(0, match.index).split('\n').length, tag: text.slice(match.index, end + 1) });
  }
  return tags;
}

describe('primary actions are at least 44×44 px (SDD §16)', () => {
  it('finds the primary buttons', () => {
    const primary = Object.values(sources)
      .flatMap(buttonTags)
      .filter(({ tag }) => tag.includes('variant="primary"'));
    expect(primary.length).toBeGreaterThan(8);
  });

  it.each(Object.keys(sources))('%s', (file) => {
    const small = buttonTags(sources[file] ?? '')
      .filter(({ tag }) => tag.includes('variant="primary"') && !tag.includes('size="lg"'))
      .map(({ line }) => line);
    expect(small, `primary buttons below 44 px (size="lg") in ${file}`).toEqual([]);
  });
});
