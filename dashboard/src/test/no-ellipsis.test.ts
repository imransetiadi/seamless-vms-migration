import { describe, expect, it } from 'vitest';

// Every UI source file as written, without the tests (they may name the classes they look for).
const sources = Object.fromEntries(
  Object.entries(import.meta.glob('../**/*.{ts,tsx}', { query: '?raw', import: 'default', eager: true }) as Record<string, string>).filter(
    ([file]) => !/\.test\.tsx?$/.test(file),
  ),
);

// Tailwind classes that cut text off with an ellipsis: one line (`truncate`, `text-ellipsis`) or several (`line-clamp-N`).
const CUTS_TEXT = /(?<![\w-])(truncate|text-ellipsis|line-clamp-\d+)(?![\w-])/;

describe('no text is cut off with an ellipsis (SDD §16: a name, label or value that does not fit wraps; WCAG 1.4.10)', () => {
  it('finds the sources', () => {
    expect(Object.keys(sources).length).toBeGreaterThan(50);
  });

  it.each(Object.keys(sources))('%s', (file) => {
    const offending = (sources[file] ?? '')
      .split('\n')
      .map((line, i) => ({ line: i + 1, text: line.trim() }))
      .filter(({ text }) => CUTS_TEXT.test(text));
    expect(offending, `text cut off in ${file}`).toEqual([]);
  });
});
