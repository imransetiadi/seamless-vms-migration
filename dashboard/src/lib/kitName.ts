/** Pairs of a transliteration table row: each character of `from` written as the character of `to` at its place. */
const pairs = (from: string, to: string): [string, string][] => {
  const target = [...to];
  return [...from].map((ch, i) => [ch, target[i]!]);
};

/** SafeVmName's transliteration table (the pinned migration kit 2.2.7, as the API's planning/kit.py holds it). */
const KIT_TRANSLITERATIONS = new Map<string, string>([
  ...pairs('áéíóúÁÉÍÓÚñÑüÜ', 'aeiouAEIOUnNuU'),
  ...pairs('ãõàâêôçÃÕÀÂÊÔÇ', 'aoaaeocAOAAEOC'),
  ...pairs('èëîïùûÈËÎÏÙÛ', 'eeiiuuEEIIUU'),
  ['æ', 'ae'],
  ['Æ', 'AE'],
  ['œ', 'oe'],
  ['Œ', 'OE'],
  ['ß', 'ss'],
  ...pairs('äöÄÖìòÌÒ', 'aoAOioIO'),
  ...[...'·’–— '].map((ch): [string, string] => [ch, '_']),
]);

/**
 * The VM's name as the VMware migration kit writes it into every name it gives - its destination server, the prefix of its
 * ports, its volumes (SDD §7.2): transliterated, every other character outside [A-Za-z0-9_-] written `_`, runs of `_` and of
 * `-` collapsed, cut to 64 characters, its trailing `_` and `-` removed - the API's kit_vm_name, SafeVmName of the kit.
 */
export function kitVmName(name: string): string {
  let safe = '';
  for (const ch of name) safe += KIT_TRANSLITERATIONS.get(ch) ?? ch;
  safe = safe
    .replace(/[^a-zA-Z0-9_-]/gu, '_')
    .replace(/_+/g, '_')
    .replace(/-+/g, '-');
  return safe.slice(0, 64).replace(/[_-]+$/, '');
}
