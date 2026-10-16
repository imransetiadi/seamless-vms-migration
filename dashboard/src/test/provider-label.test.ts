import ts from 'typescript';
import { describe, expect, it } from 'vitest';

// Every text that names a provider reads its label - its name, else its id - as the API's Provider.label (SDD §1
// naming, §4.2): a provider stored with a blank name before the API refused one (review RV20-11) never reads blank.
// The type checker finds each read of Provider.name, whatever the variable holding the provider is called (review
// RV21-06: a page-local helper, `p.name` in a list, the mock's events), by element access or destructuring, a Provider
// given where a shape of its own that reads `name` is expected - a call's argument, an object member, a JSX prop, a
// generic constrained to a name - and a function that reads one handed over to be called with Providers (reviews
// RV22-01, RV23-05, RV24-05).

// the dashboard's directory, from this file's URL (src/test/) - as a string: Vite rewrites `new URL(…, import.meta.url)`
// into a dev-server URL. The type checker's file names use "/" too.
const root = decodeURIComponent(import.meta.url.replace(/^file:\/\//, '')).replace(/\/src\/test\/[^/]+$/, '');

/** The reads that are not texts: the label itself, and the edit form's name field, which edits the stored name. */
const RAW = new Set(['src/lib/providerUse.ts: provider.name', 'src/components/ProviderDialog.tsx: provider?.name']);

function config(): ts.ParsedCommandLine {
  const parsed = ts.getParsedCommandLineOfConfigFile(`${root}/tsconfig.json`, {}, { ...ts.sys, onUnRecoverableConfigFileDiagnostic: () => undefined });
  if (!parsed) throw new Error('tsconfig.json cannot be read');
  return parsed;
}

/** The dashboard's program, as tsconfig.json builds it. */
function dashboard(): ts.Program {
  const parsed = config();
  return ts.createProgram(parsed.fileNames, parsed.options);
}

/** A program of one probe component, src/components/Probe.tsx, held in memory, beside the dashboard's own files. */
function probe(text: string): ts.Program {
  const { options } = config();
  const file = `${root}/src/components/Probe.tsx`;
  const host = ts.createCompilerHost(options);
  const read = host.getSourceFile.bind(host);
  host.getSourceFile = (name, version, ...rest) => (name === file ? ts.createSourceFile(name, text, version) : read(name, version, ...rest));
  host.fileExists = (name) => name === file || ts.sys.fileExists(name);
  host.readFile = (name) => (name === file ? text : ts.sys.readFile(name));
  return ts.createProgram([file], options, host);
}

/** Each read of a provider's bare name for a text in `program`'s sources under src/ (tests aside). */
function bareNames(program: ts.Program): string[] {
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
      // the same read by element access, `provider['name']`, or by destructuring a Provider - a callback's parameter, a
      // component's props, a variable - `{ name }` or `{ name: shown }` (review RV24-05)
      if (ts.isElementAccessExpression(node) && ts.isStringLiteralLike(node.argumentExpression) && node.argumentExpression.text === 'name') {
        if (holdsProviders(checker.getTypeAtLocation(node.expression))) found.push(where(node));
      }
      if (ts.isBindingElement(node) && ts.isObjectBindingPattern(node.parent) && (node.propertyName ?? node.name).getText() === 'name') {
        if (holdsProviders(checker.getTypeAtLocation(node.parent))) found.push(where(node));
      }
      // a Provider given where a shape of its own that reads `name` is expected, as `{ id: string; name: string }`: a
      // call's argument, a member of an object literal, a JSX prop, a return value - its contextual type (reviews
      // RV22-01, RV23-05). An identifier that only names a declaration or a member is no value; a shorthand member is;
      // a spread whose object writes its own `name` after it reads none (review RV24-05).
      const naming = ts.isIdentifier(node) && !ts.isShorthandPropertyAssignment(node.parent) && (node.parent as { name?: ts.Node }).name === node;
      const renamed =
        node.parent !== undefined &&
        ts.isSpreadAssignment(node.parent) &&
        ts.isObjectLiteralExpression(node.parent.parent) &&
        node.parent.parent.properties.slice(node.parent.parent.properties.indexOf(node.parent) + 1).some((p) => p.name?.getText() === 'name');
      if (ts.isExpression(node) && !naming && !renamed) {
        const expected = checker.getContextualType(node);
        const given = checker.getTypeAtLocation(node);
        if (expected && !holdsProviders(expected) && readsName(expected) && holdsProviders(given)) found.push(where(node));
        // a function handed over where it is called with Providers, whose own parameter reads a shape's `name`:
        // `providers.map(nameOf)`, `providers.map((item: Named) => item.name)` (review RV24-05)
        const [want, have] = [expected?.getCallSignatures()[0], given.getCallSignatures()[0]];
        if (want && have) {
          const reads = have.parameters.some((param, i) => {
            const called = want.parameters[i];
            const own = checker.getTypeOfSymbolAtLocation(param, node);
            return called !== undefined && holdsProviders(checker.getTypeOfSymbolAtLocation(called, node)) && !holdsProviders(own) && readsName(own);
          });
          if (reads) found.push(where(node));
        }
      }
      // a Provider given to a generic parameter constrained to a shape that reads `name`, `<T extends { name: string }>`:
      // its contextual type is the Provider the call infers, so the constraint is read (review RV24-05)
      if (ts.isCallExpression(node)) {
        const declared = checker.getResolvedSignature(node)?.getDeclaration();
        node.arguments.forEach((arg, i) => {
          const param = declared && 'parameters' in declared ? declared.parameters[i] : undefined;
          const type = param?.type ? checker.getTypeFromTypeNode(param.type) : undefined;
          const constraint = type?.isTypeParameter() ? checker.getBaseConstraintOfType(type) : undefined;
          if (constraint && !holdsProviders(constraint) && readsName(constraint) && holdsProviders(checker.getTypeAtLocation(arg))) found.push(where(arg));
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
    expect(bareNames(dashboard())).toEqual([]);
  }, 30_000);

  it("finds a Provider given wherever a shape of its own that reads `name` is expected (review RV23-05)", () => {
    const component = [
      "import type { Provider } from '../api/types';",
      'type Named = { id: string; name: string };',
      'export const nameOf = (item: Named) => item.name;',
      'export const render = (props: { item: Named }) => props.item.name;',
      'export const Name = ({ item }: { item: Named }) => <span>{item.name}</span>;',
      // a call's argument (the guard's control), a member of an argument object, a JSX prop, a shorthand member, a return
      'export const viaCall = (provider: Provider) => nameOf(provider);',
      'export const viaObject = (provider: Provider) => render({ item: provider });',
      'export const viaJsx = ({ provider }: { provider: Provider }) => <Name item={provider} />;',
      'export const viaShorthand = (item: Provider) => render({ item });',
      'export const viaReturn = (provider: Provider): Named => provider;',
      // a Provider where a Provider is expected reads its label as the dashboard does: none of these
      'export const itself = (providers: Provider[]) => providers.filter((p) => p.id).map((p) => <Name item={{ id: p.id, name: p.id }} />);',
      '',
    ].join('\n');
    expect(bareNames(probe(component))).toEqual([
      'src/components/Probe.tsx:6: provider',
      'src/components/Probe.tsx:7: provider',
      'src/components/Probe.tsx:8: provider',
      'src/components/Probe.tsx:9: item',
      'src/components/Probe.tsx:10: provider',
    ]);
  }, 30_000);

  it("finds the other shapes that read a Provider's name, and lets the label written over it pass (review RV24-05)", () => {
    const component = [
      "import type { Provider } from '../api/types';",
      "import { providerLabel } from '../lib/providerUse';",
      'type Named = { id: string; name: string };',
      'export const nameOf = (item: Named) => item.name;',
      'export const generic = <T extends { name: string }>(item: T) => item.name;',
      // a callback's destructured parameter, element access, a name-reading function handed over as a callback, a
      // Provider given where a generic constrained to a name is expected, destructured props, a renamed binding
      'export const viaCallbackParam = (providers: Provider[]) => providers.map(({ name }) => <li key={name}>{name}</li>);',
      "export const viaElement = (provider: Provider) => <span>{provider['name']}</span>;",
      'export const viaCallback = (providers: Provider[]) => providers.map(nameOf);',
      'export const viaGeneric = (provider: Provider) => generic(provider);',
      'export const ViaProps = ({ name }: Provider) => <span>{name}</span>;',
      'export const viaRenamed = ({ name: shown }: Provider) => shown;',
      'export const viaAnnotated = (providers: Provider[]) => providers.map((item: Named) => item.name);',
      // the label written over the name, and another member: none of these
      'export const viaSpreadLabelled = (provider: Provider) => nameOf({ ...provider, name: providerLabel(provider) });',
      'export const viaId = (providers: Provider[]) => providers.map(({ id }) => id);',
      '',
    ].join('\n');
    expect(bareNames(probe(component))).toEqual([
      'src/components/Probe.tsx:6: name',
      "src/components/Probe.tsx:7: provider['name']",
      'src/components/Probe.tsx:8: nameOf',
      'src/components/Probe.tsx:9: provider',
      'src/components/Probe.tsx:10: name',
      'src/components/Probe.tsx:11: name: shown',
      'src/components/Probe.tsx:12: (item: Named) => item.name',
    ]);
  }, 30_000);
});
