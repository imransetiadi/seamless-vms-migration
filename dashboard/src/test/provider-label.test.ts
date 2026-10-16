import ts from 'typescript';
import { describe, expect, it } from 'vitest';

// Every text that names a provider reads its label - its name, else its id - as the API's Provider.label (SDD §1
// naming, §4.2): a provider stored with a blank name before the API refused one (review RV20-11) never reads blank.
// The type checker finds each read of Provider.name, whatever the variable holding the provider is called (review
// RV21-06: a page-local helper, `p.name` in a list, the mock's events), and a Provider handed to a function that reads
// the `name` of a shape of its own (review RV22-01).

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
  const isProviders = (node: ts.Node | undefined) => node !== undefined && ts.isInterfaceDeclaration(node) && node.name.text === 'Provider';
  /** The type is a Provider, a list of them, a union holding one, or one of TypeScript's own over it (Pick<Provider, …>). */
  const holdsProviders = (type: ts.Type): boolean => {
    if (type.isUnion()) return type.types.some(holdsProviders);
    if ((type.aliasTypeArguments ?? []).some(holdsProviders)) return true;
    const element = checker.isArrayType(type) ? checker.getTypeArguments(type as ts.TypeReference)[0] : undefined;
    if (element) return holdsProviders(element);
    return (type.getSymbol()?.declarations ?? []).some(isProviders) || (type.aliasSymbol?.declarations ?? []).some((d) => isProviders(d));
  };
  /** The type, or the type of its items, has a `name` of its own. */
  const readsName = (type: ts.Type): boolean => {
    const element = checker.isArrayType(type) ? checker.getTypeArguments(type as ts.TypeReference)[0] : undefined;
    return (element ?? type).getProperty('name') !== undefined;
  };
  for (const source of program.getSourceFiles()) {
    const file = source.fileName.startsWith(`${root}/`) ? source.fileName.slice(root.length + 1) : source.fileName;
    if (!file.startsWith('src/') || /\.test\.tsx?$/.test(file) || file.startsWith('src/test/')) continue;
    const where = (node: ts.Node) => `${file}:${source.getLineAndCharacterOfPosition(node.getStart()).line + 1}: ${node.getText()}`;
    const visit = (node: ts.Node): void => {
      // a read of Provider.name - any declaration counts: for `x: Provider | Plan` it is Plan's or Provider's (review RV22-01)
      if (ts.isPropertyAccessExpression(node) && node.name.text === 'name') {
        const read = checker.getSymbolAtLocation(node.name)?.declarations?.some((d) => isProviders(d.parent)) ?? false;
        if (read && !RAW.has(`${file}: ${node.getText()}`)) found.push(where(node));
      }
      // a Provider handed to a parameter that reads `name` from a shape of its own, as `{ id: string; name: string }`
      if (ts.isCallExpression(node)) {
        const signature = checker.getResolvedSignature(node);
        node.arguments.forEach((arg, i) => {
          const parameter = signature?.parameters[Math.min(i, (signature?.parameters.length ?? 1) - 1)];
          if (!parameter || !holdsProviders(checker.getTypeAtLocation(arg))) return;
          const expected = checker.getTypeOfSymbolAtLocation(parameter, node);
          if (!holdsProviders(expected) && readsName(expected)) found.push(where(arg));
        });
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
