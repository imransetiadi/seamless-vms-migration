import { describe, expect, it } from 'vitest';
import { formatMappings, parseMappings } from './mappings';

describe('mappings text (SDD §16: the form accepts what the API accepts)', () => {
  it('reads one pair per line with any of the three separators', () => {
    expect(parseMappings('a = b\nc -> d\n\ne → f')).toEqual({ a: 'b', c: 'd', e: 'f' });
  });

  it('writes plain names as before', () => {
    expect(formatMappings({ 'tenant-net': 'tenant-net-ovn', 'm1.xlarge': 'm2.xlarge' })).toBe('tenant-net = tenant-net-ovn\nm1.xlarge = m2.xlarge');
  });

  it('round-trips names that contain a separator, a double quote, a backslash or edge spaces', () => {
    const mappings = { 'm1=gpu': 'm2->gpu', 'net→a': 'plain', 'say "hi"': 'back\\slash', ' padded ': 'ok', 'tenant-net': 'tenant-net-ovn' };
    const text = formatMappings(mappings);
    expect(text).toContain('"m1=gpu" = "m2->gpu"');
    expect(parseMappings(text)).toEqual(mappings);
  });

  it('reads quoted names typed by hand', () => {
    expect(parseMappings('"m1=gpu" = m2.gpu\nnet -> "net->b"')).toEqual({ 'm1=gpu': 'm2.gpu', net: 'net->b' });
  });

  it('refuses a line it cannot read unambiguously', () => {
    for (const line of ['a = b = c', '"open = b', 'a = "open', 'a"b = c', '= b', 'a =', 'a b', '"a" "b"', '"a" = "b" extra']) {
      expect(parseMappings(line), line).toBeNull();
    }
  });
});

describe('a source named like an Object.prototype key (review RV23-01)', () => {
  it('keeps a "__proto__ = net-x" mapping the API accepts through the form, as any other', () => {
    const parsed = parseMappings('__proto__ = net-x\nweb = net-y')!;
    expect(Object.entries(parsed)).toEqual([
      ['__proto__', 'net-x'],
      ['web', 'net-y'],
    ]);
    expect(formatMappings(parsed)).toBe('__proto__ = net-x\nweb = net-y');
    expect(JSON.parse(JSON.stringify(parsed))).toEqual(JSON.parse('{"__proto__": "net-x", "web": "net-y"}'));
  });
});
