import ts from 'typescript';
import { describe, expect, it } from 'vitest';
import indexHtml from '../../index.html?raw';
import { DESTINATIONS } from '../lib/platforms';

// The dashboard's sources as written (SDD §1 naming, §16, plan T6): a text about the destination cloud says "the
// destination", or the destination provider's name where a plan or a migration is shown - never "RHOSO", which a
// community OpenStack destination is not.
const sources = import.meta.glob(['../**/*.ts', '../**/*.tsx', '!../**/*.test.ts', '!../**/*.test.tsx', '!../test/**'], {
  query: '?raw',
  import: 'default',
  eager: true,
}) as Record<string, string>;

/** What names the Red Hat product: its acronym or its long-form name (reviews RV20-04, RV20-06). */
const NAMES_RHOSO = /RHOSO|Red Hat OpenStack Services on OpenShift/;

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
  // the RHOSO 18.0 preset, by the product's long-form name
  "'Red Hat OpenStack Services on OpenShift; Keystone published as a route.'",
]);

/**
 * The texts of one source that name the Red Hat product, each in its source form - a string literal or a template
 * part with its quotes, JSX text with its spaces collapsed - read from the source's syntax tree, so no line break,
 * apostrophe or {expression} hides one and no comment counts (review RV20-06).
 */
function namingTexts(file: string, source: string): string[] {
  const kind = file.endsWith('.tsx') ? ts.ScriptKind.TSX : ts.ScriptKind.TS;
  const tree = ts.createSourceFile(file, source, ts.ScriptTarget.Latest, true, kind);
  const found: string[] = [];
  const visit = (node: ts.Node): void => {
    if (ts.isJsxText(node)) {
      const text = node.text.replace(/\s+/g, ' ').trim();
      if (NAMES_RHOSO.test(text)) found.push(text);
    } else if (
      ts.isStringLiteral(node) ||
      ts.isNoSubstitutionTemplateLiteral(node) ||
      ts.isTemplateHead(node) ||
      ts.isTemplateMiddle(node) ||
      ts.isTemplateTail(node)
    ) {
      if (NAMES_RHOSO.test(node.text)) found.push(node.getText(tree));
    }
    ts.forEachChild(node, visit);
  };
  visit(tree);
  return found;
}

/** The texts of a page that name the Red Hat product without naming every destination (SDD §1 naming, §21). */
function pageTexts(html: string): string[] {
  const texts = [...html.matchAll(/<title>([^<]*)<\/title>|content="([^"]*)"/g)].map((match) => match[1] ?? match[2] ?? '');
  return texts.filter((text) => NAMES_RHOSO.test(text) && !text.includes(DESTINATIONS));
}

describe('the wording scan reads every text however it is written (review RV20-06)', () => {
  it('JSX text after an {expression}, JSX text with an apostrophe, a template literal over two lines', () => {
    const afterExpression = '<p><strong>The source VM runs again</strong> since {formatDateTime(at)}, so its data diverges from RHOSO: stop it,</p>';
    const apostrophe = "<p className=\"text-xs\">The VM's data no longer reaches RHOSO after the cutover.</p>";
    const twoLines = 'const note = `Verified on\nRHOSO ${since}`;';
    expect({
      afterExpression: namingTexts('a.tsx', `export const A = () => (${afterExpression});`),
      apostrophe: namingTexts('b.tsx', `export const B = () => (${apostrophe});`),
      twoLines: namingTexts('c.ts', twoLines),
    }).toEqual({
      afterExpression: [', so its data diverges from RHOSO: stop it,'],
      apostrophe: ["The VM's data no longer reaches RHOSO after the cutover."],
      twoLines: ['`Verified on\nRHOSO ${'],
    });
  });

  it('the long-form name too, and never a comment', () => {
    expect(namingTexts('d.ts', "const d = 'Live state of VM migrations into Red Hat OpenStack Services on OpenShift 18.0.';")).toEqual([
      "'Live state of VM migrations into Red Hat OpenStack Services on OpenShift 18.0.'",
    ]);
    expect(namingTexts('e.ts', '// migrations into RHOSO\n/* RHOSO */\nconst e = 1;')).toEqual([]);
  });
});

describe('no dashboard text says RHOSO for the destination (SDD §1 naming, plan T6)', () => {
  it('finds the sources', () => {
    expect(Object.keys(sources).length).toBeGreaterThan(50);
  });

  it('keeps RHOSO only where a text is about the product itself', () => {
    const said = Object.entries(sources).flatMap(([file, text]) =>
      namingTexts(file, text)
        .filter((literal) => !PRODUCT.has(literal))
        .map((literal) => `${file}: ${literal}`),
    );
    expect(said).toEqual([]);
  });

  it("reads index.html's title and description too (review RV20-06)", () => {
    // the description before RV20-04 named RHOSO 18.0 alone
    expect(pageTexts('<meta name="description" content="VM migrations into Red Hat OpenStack Services on OpenShift 18.0" />')).toEqual([
      'VM migrations into Red Hat OpenStack Services on OpenShift 18.0',
    ]);
    expect(pageTexts(indexHtml)).toEqual([]);
  });
});
