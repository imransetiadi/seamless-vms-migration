import { describe, expect, it } from 'vitest';

// The dashboard's sources as written (SDD §1 naming, §16, plan T6): a text about the destination cloud says "the
// destination", or the destination provider's name where a plan or a migration is shown - never "RHOSO", which a
// community OpenStack destination is not.
const sources = import.meta.glob(['../**/*.ts', '../**/*.tsx', '!../**/*.test.ts', '!../**/*.test.tsx', '!../test/**'], {
  query: '?raw',
  import: 'default',
  eager: true,
}) as Record<string, string>;

/** A string literal on one line: '...', "..." or `...`. */
const LITERAL = /'(?:[^'\\\n]|\\.)*'|"(?:[^"\\\n]|\\.)*"|`(?:[^`\\\n]|\\.)*`/g;

/** The literals of a source that say RHOSO, comments aside. */
function rhosoLiterals(text: string): string[] {
  const code = text.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');
  return (code.match(LITERAL) ?? []).filter((literal) => literal.includes('RHOSO'));
}

/**
 * Texts about the Red Hat product itself, which keep its name (SDD §1 naming): its preset and kind labels, the
 * platforms the dashboard lists, a name it suggests for an RHOSO provider, and the demo's own RHOSO providers and
 * plans - operator data, not copy.
 */
const PRODUCT = new Set([
  "'RHOSO 18.0'",
  "'e.g. RHOSO 18.0 production'",
  "'RHOSO 18.0 — Production'",
  "'RHOSO 18.0 — Staging'",
  "'DC1 → RHOSO production rollout'",
  "'RHOSP 17.1 DC1 tenants to RHOSO 18.0; warm pre-copy for databases, pilot first.'",
  "'RHOSO uses the existing external Ceph cluster; Spark nodes are handed over by Cinder unmanage/manage.'",
  "'Payroll VM unbootable on RHOSO (VFS mount)'",
  // the destinations where the dashboard lists them: the sign-in page, the Providers page and the brand line
  "'RHOSO 18.0 and community OpenStack'",
  "'RHOSO 18.0 or OpenStack'",
]);

describe('no dashboard text says RHOSO for the destination (SDD §1 naming, plan T6)', () => {
  it('finds the sources', () => {
    expect(Object.keys(sources).length).toBeGreaterThan(50);
  });

  it('keeps RHOSO only where a text is about the product itself', () => {
    const said = Object.entries(sources).flatMap(([file, text]) =>
      rhosoLiterals(text)
        .filter((literal) => !PRODUCT.has(literal))
        .map((literal) => `${file}: ${literal}`),
    );
    expect(said).toEqual([]);
  });

  it('reads JSX text too: a sentence between tags is copy', () => {
    const jsx = Object.entries(sources).flatMap(([file, text]) =>
      text
        .split('\n')
        .filter((line) => />[^<{}'"`]*RHOSO[^<{}'"`]*</.test(line) || /^\s*[A-Z][^<{}'"`=]*RHOSO[^'"`]*$/.test(line))
        .filter((line) => !/^\s*(\/\/|\*)/.test(line))
        .map((line) => `${file}: ${line.trim()}`),
    );
    expect(jsx).toEqual([]);
  });
});
