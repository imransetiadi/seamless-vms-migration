import { describe, expect, it } from 'vitest';
import { canonicalJson, sha256Hex } from './canonical';

describe('canonical JSON and SHA-256 (SDD §14.5)', () => {
  it('hashes the standard test vectors', () => {
    expect(sha256Hex('')).toBe('e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855');
    expect(sha256Hex('abc')).toBe('ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad');
    // two blocks: 56 bytes need the length in a second block
    expect(sha256Hex('abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq')).toBe('248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1');
    // UTF-8: the bytes of the text are hashed, not its UTF-16 units
    expect(sha256Hex('é')).toBe('4a99557e4033c3539de2eb65472017cad5f9557f7a0625a09f1c3f6e2ba69c4c');
  });

  it('writes JSON the way the control plane does: sorted keys, no spaces, non-ASCII as is', () => {
    expect(canonicalJson({ b: 1, a: [true, 'x', null], c: { z: 'é', y: '"q"' } })).toBe('{"a":[true,"x",null],"b":1,"c":{"y":"\\"q\\"","z":"é"}}');
  });

  it('matches the hash Python computes over the same snapshot', () => {
    // hashlib.sha256(json.dumps(p, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    const payload = { tool: 'jev_decide', scope: ['cold', 'warm'], arguments: { escalate_on_contradiction: true, decision: 'Which strategy for VM [vm]?' } };
    expect(sha256Hex(canonicalJson(payload))).toBe('501720e595baf10802e7343a7bc7e9f182645c6cc311d41e92d1f3db402248b8');
  });
});
