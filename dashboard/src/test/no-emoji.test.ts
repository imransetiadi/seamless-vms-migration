import { describe, expect, it } from 'vitest';

// Every UI source file plus the HTML shell, as written.
const sources = {
  ...import.meta.glob('../**/*.{ts,tsx}', { query: '?raw', import: 'default', eager: true }),
  ...import.meta.glob('../../index.html', { query: '?raw', import: 'default', eager: true }),
} as Record<string, string>;

// Emoji presentation characters (pictographs, dingbats with emoji style, flags, variation selector 16).
const EMOJI = /\p{Extended_Pictographic}|[\u{1F1E6}-\u{1F1FF}]|\u{FE0F}/u;

describe('no emoji used as icons (ui-ux-pro-max pre-delivery checklist)', () => {
  it('finds the sources', () => {
    expect(Object.keys(sources).length).toBeGreaterThan(50);
  });

  it.each(Object.keys(sources))('%s', (file) => {
    const offending = (sources[file] ?? '')
      .split('\n')
      .map((line, i) => ({ line: i + 1, text: line }))
      .filter(({ text }) => EMOJI.test(text));
    expect(offending, `emoji in ${file}`).toEqual([]);
  });
});
