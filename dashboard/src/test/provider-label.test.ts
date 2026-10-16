import ts from 'typescript';
import { describe, expect, it } from 'vitest';

// Every text that names a provider reads its label - its name, else its id - as the API's Provider.label (SDD §1
// naming, §4.2): a provider stored with a blank name before the API refused one (review RV20-11) never reads blank.
// The type checker finds each read of Provider.name, whatever the variable holding the provider is called (review
// RV21-06: a page-local helper, `p.name` in a list, the mock's events).

// the dashboard's directory, from this file's URL (src/test/) - as a string: Vite rewrites `new URL(…, import.meta.url)`
// into a dev-server URL. The type checker's file names use "/" too.
const root = decodeURIComponent(import.meta.url.replace(/^file:\/\//, '')).replace(/\/src\/test\/[^/]+$/, '');

/** The reads that are not texts: the label itself, and the edit form's name field, which edits the stored name. */
const RAW = new Set(['src/lib/providerUse.ts: provider.name', 'src/components/ProviderDialog.tsx: provider?.name']);

function bareNames(): string[] {
  const parsed = ts.getParsedCommandLineOfConfigFile(`${root}/tsconfig.json`, {}, { ...ts.sys, onUnRecoverableConfigFileDiagnostic: () => undefined });
  if (!parsed) throw new Error('tsconfig.json cannot be read');
  const program = ts.createProgram(parsed.fileNames, parsed.options);
  const checker = program.getTypeChecker();
  const found: string[] = [];
  for (const source of program.getSourceFiles()) {
    const file = source.fileName.startsWith(`${root}/`) ? source.fileName.slice(root.length + 1) : source.fileName;
    if (!file.startsWith('src/') || /\.test\.tsx?$/.test(file) || file.startsWith('src/test/')) continue;
    const visit = (node: ts.Node): void => {
      if (ts.isPropertyAccessExpression(node) && node.name.text === 'name') {
        const owner = checker.getSymbolAtLocation(node.name)?.declarations?.[0]?.parent;
        if (owner && ts.isInterfaceDeclaration(owner) && owner.name.text === 'Provider' && !RAW.has(`${file}: ${node.getText()}`)) {
          found.push(`${file}:${source.getLineAndCharacterOfPosition(node.getStart()).line + 1}: ${node.getText()}`);
        }
      }
      ts.forEachChild(node, visit);
    };
    visit(source);
  }
  return found;
}

describe('every text names a provider by its label (SDD §1 naming, §4.2, review RV21-06)', () => {
  it("never reads a provider's bare name for a text", () => {
    expect(bareNames()).toEqual([]);
  }, 30_000);
});
