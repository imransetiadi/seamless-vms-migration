import { describe, expect, it } from 'vitest';

// The Demo-mode seed as written: what a customer sees in a demo (SDD §14.6, ADV-17).
const seeds = import.meta.glob(['../api/mockData.ts', '../api/mock.ts'], { query: '?raw', import: 'default', eager: true }) as Record<string, string>;

/** Strings that read as test data, such as "demo46", "test1", "foo" or "lorem". */
const PLACEHOLDER = /\bdemo\d+\b|\btest\d+\b|\b(?:foo|bar|baz|qux|asdf|qwerty|lorem|ipsum|dummy)\b|xxx+/gi;

describe('no placeholder strings in the Demo seed (ADV-17)', () => {
  it('finds the seed', () => {
    expect(Object.keys(seeds)).toHaveLength(2);
  });

  it.each(Object.keys(seeds))('%s', (file) => {
    expect(seeds[file]!.match(PLACEHOLDER) ?? [], file).toEqual([]);
  });
});
