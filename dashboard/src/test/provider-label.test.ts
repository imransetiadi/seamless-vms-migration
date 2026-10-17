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

/** The reads that are not texts, by file and the function they stand in: the label itself, and the edit form's name
 * field, which edits the stored name (review RV25-06: a further read in either file is found). */
const RAW = new Set(['src/lib/providerUse.ts#providerLabel: provider.name', 'src/components/ProviderDialog.tsx#initialState: provider?.name']);

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

/** A program of the dashboard's file `relative` with `extra` appended, in memory, and what it imports: rooted at that
 * file alone, it type-checks a fraction of what the whole dashboard's program does. */
function appended(relative: string, extra: string): ts.Program {
  const { options } = config();
  const file = `${root}/${relative}`;
  const text = `${ts.sys.readFile(file)}\n${extra}\n`;
  const host = ts.createCompilerHost(options);
  const read = host.getSourceFile.bind(host);
  host.getSourceFile = (name, version, ...rest) => (name === file ? ts.createSourceFile(name, text, version) : read(name, version, ...rest));
  host.readFile = (name) => (name === file ? text : ts.sys.readFile(name));
  return ts.createProgram([file], options, host);
}

/** Each read of a provider's bare name for a text in `program`'s sources under src/ (tests aside). */
function bareNames(program: ts.Program): string[] {
  const checker = program.getTypeChecker();
  const found: string[] = [];
  const isProviders = (node: ts.Node | undefined) => node !== undefined && ts.isInterfaceDeclaration(node) && node.name.text === 'Provider';
  /** The types a type carries: a list's, a Map's or a Set's type arguments, a record's values (review RV25-06). */
  const carried = (type: ts.Type): ts.Type[] => [
    ...(((type as ts.ObjectType).objectFlags ?? 0) & ts.ObjectFlags.Reference ? checker.getTypeArguments(type as ts.TypeReference) : []),
    ...checker.getIndexInfosOfType(type).map((info) => info.type),
  ];
  /** The type is a Provider, a union holding one, one of TypeScript's own over it (Pick<Provider, …>), or carries one: a
   * list, a Map or a record of them. */
  const holdsProviders = (type: ts.Type, depth = 0): boolean => {
    if (depth > 8) return false;
    if (type.isUnion()) return type.types.some((t) => holdsProviders(t, depth + 1));
    if ((type.aliasTypeArguments ?? []).some((t) => holdsProviders(t, depth + 1))) return true;
    if ((type.getSymbol()?.declarations ?? []).some(isProviders) || (type.aliasSymbol?.declarations ?? []).some((d) => isProviders(d))) return true;
    return carried(type).some((t) => holdsProviders(t, depth + 1));
  };
  /** The type has a `name` of its own, or the types it carries do: a list's items, a Map's or a record's values. */
  const readsName = (type: ts.Type): boolean => {
    const items = carried(type);
    return items.length ? items.some((t) => t.getProperty('name') !== undefined) : type.getProperty('name') !== undefined;
  };
  /** The function a node stands in, by name: a declaration's, a method's, or the variable an arrow function is given to. */
  const owner = (node: ts.Node): string => {
    for (let n: ts.Node | undefined = node.parent; n; n = n.parent) {
      if ((ts.isFunctionDeclaration(n) || ts.isMethodDeclaration(n)) && n.name) return n.name.getText();
      if ((ts.isArrowFunction(n) || ts.isFunctionExpression(n)) && ts.isVariableDeclaration(n.parent)) return n.parent.name.getText();
    }
    return '';
  };
  /** An object literal's member that writes `name`: `name:`, `'name':`, `['name']:`, a shorthand, or a spread object holding one. */
  const writesName = (member: ts.ObjectLiteralElementLike): boolean => {
    if (ts.isSpreadAssignment(member)) return ts.isObjectLiteralExpression(member.expression) && member.expression.properties.some(writesName);
    const key = member.name;
    if (key === undefined) return false;
    if (ts.isIdentifier(key) || ts.isStringLiteralLike(key)) return key.text === 'name';
    return ts.isComputedPropertyName(key) && ts.isStringLiteralLike(key.expression) && key.expression.text === 'name';
  };
  for (const source of program.getSourceFiles()) {
    const file = source.fileName.startsWith(`${root}/`) ? source.fileName.slice(root.length + 1) : source.fileName;
    if (!file.startsWith('src/') || /\.test\.tsx?$/.test(file) || file.startsWith('src/test/')) continue;
    const where = (node: ts.Node) => `${file}:${source.getLineAndCharacterOfPosition(node.getStart()).line + 1}: ${node.getText()}`;
    const visit = (node: ts.Node): void => {
      // a read of Provider.name - any declaration counts: for `x: Provider | Plan` it is Plan's or Provider's (review RV22-01)
      if (ts.isPropertyAccessExpression(node) && node.name.text === 'name') {
        const read = checker.getSymbolAtLocation(node.name)?.declarations?.some((d) => isProviders(d.parent)) ?? false;
        // the label's own read and the edit form's field are exempt by the function they stand in, not by their text
        if (read && !RAW.has(`${file}#${owner(node)}: ${node.getText()}`)) found.push(where(node));
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
        node.parent.parent.properties.slice(node.parent.parent.properties.indexOf(node.parent) + 1).some(writesName);
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
          // the parameter itself, or the items of a list of it - `items: readonly T[]` (review RV25-06); a type parameter
          // deeper in an object, `{ item: T }`, or a keyed accessor `pluck(provider, 'name')` stay out of reach
          const subject = type && checker.isArrayType(type) ? checker.getTypeArguments(type as ts.TypeReference)[0] : type;
          const constraint = subject?.isTypeParameter() ? checker.getBaseConstraintOfType(subject) : undefined;
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
  // it builds and type-checks the whole dashboard, as the test that appends to it does: under a
  // full parallel run that took over 30 s once (2026-10-17), so both get the same ceiling
  it("never reads a provider's bare name for a text", () => {
    expect(bareNames(dashboard())).toEqual([]);
  }, 120_000);

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

  it('finds a further bare-name read in the two files whose one raw read it exempts (review RV25-06)', () => {
    const found = [
      ...bareNames(appended('src/components/ProviderDialog.tsx', 'export const dialogTitle = (provider?: Provider) => `Edit ${provider?.name}`;')),
      ...bareNames(appended('src/lib/providerUse.ts', 'export const heading = (provider: Provider) => `Provider ${provider.name}`;')),
    ];
    expect(found).toEqual([expect.stringMatching(/^src\/components\/ProviderDialog\.tsx:\d+: provider\?\.name$/), expect.stringMatching(/^src\/lib\/providerUse\.ts:\d+: provider\.name$/)]);
  }, 120_000);

  it('finds Providers handed over in a Record, a Map or to a generic over a list, and lets the label written over a spread pass however it is keyed (review RV25-06)', () => {
    const component = [
      "import type { Provider } from '../api/types';",
      "import { providerLabel } from '../lib/providerUse';",
      'type Named = { id: string; name: string };',
      'export const nameOf = (item: Named) => item.name;',
      'export const labels = (byId: Record<string, Named>) => Object.values(byId).map((x) => x.name);',
      'export const fromMap = (byId: Map<string, Named>) => [...byId.values()].map((x) => x.name);',
      "export const joinNames = <T extends { name: string }>(items: readonly T[]) => items.map((i) => i.name).join(', ');",
      'export const viaRecord = (byId: Record<string, Provider>) => labels(byId);',
      'export const viaMap = (byId: Map<string, Provider>) => fromMap(byId);',
      'export const viaGenericList = (providers: Provider[]) => joinNames(providers);',
      // the label written over the name - by a quoted key, a computed key, a spread object: none of these
      "export const quoted = (provider: Provider) => nameOf({ ...provider, 'name': providerLabel(provider) });",
      "export const computed = (provider: Provider) => nameOf({ ...provider, ['name']: providerLabel(provider) });",
      'export const spreadObject = (provider: Provider) => nameOf({ ...provider, ...{ name: providerLabel(provider) } });',
      '',
    ].join('\n');
    expect(bareNames(probe(component))).toEqual(['src/components/Probe.tsx:8: byId', 'src/components/Probe.tsx:9: byId', 'src/components/Probe.tsx:10: providers']);
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
