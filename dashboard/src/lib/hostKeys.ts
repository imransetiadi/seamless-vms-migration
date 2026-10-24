import { useEffect, useState } from 'react';

/**
 * A conversion host's SSH host keys (SDD §4.2, §16, §26.3, §26.4): the API's rule for a key its operator declares
 * (`public_key_problem` of seamless_migrate/security/host_keys.py), and its fingerprint as `ssh-keygen -l` prints it.
 * A refusal never quotes the line, which may be a private key: it names a type alone.
 */

/** The types a conversion host's key may have: ed25519, ECDSA or RSA. */
const KEY_TYPES = ['ssh-ed25519', 'ecdsa-sha2-nistp256', 'ecdsa-sha2-nistp384', 'ecdsa-sha2-nistp521', 'ssh-rsa'];
/** The size of each curve's point, uncompressed: 0x04, then its two coordinates (RFC 5656 §3.1). */
const POINT_BYTES: Record<string, number> = { nistp256: 65, nistp384: 97, nistp521: 133 };
/** An OpenSSH key type's name, which a refusal may name: never any other part of a line. */
const TYPE_NAME = /^(?:ssh|sk|ecdsa|x509v3|rsa)-[a-z0-9@.-]{1,60}$/;
/** Base64 as Python's strict decoder takes it: its alphabet, padded to a multiple of four. */
const BASE64 = /^[A-Za-z0-9+/]*={0,2}$/;

/** The bytes of a base64 field, or null when Python's strict decoder would refuse it. */
function decode(field: string): Uint8Array<ArrayBuffer> | null {
  if (!BASE64.test(field) || field.length % 4 !== 0) return null;
  try {
    return Uint8Array.from(atob(field), (c) => c.charCodeAt(0));
  } catch {
    return null;
  }
}

/** The SSH wire strings `blob` is made of (RFC 4251 §5), null when it is not made of them. */
function strings(blob: Uint8Array): Uint8Array[] | null {
  const parts: Uint8Array[] = [];
  let at = 0;
  while (at < blob.length) {
    if (at + 4 > blob.length) return null;
    const size = ((blob[at]! << 24) >>> 0) + (blob[at + 1]! << 16) + (blob[at + 2]! << 8) + blob[at + 3]!;
    at += 4;
    if (at + size > blob.length) return null;
    parts.push(blob.subarray(at, at + size));
    at += size;
  }
  return parts;
}

const text = (bytes: Uint8Array | undefined) => (bytes ? String.fromCharCode(...bytes) : null);

/**
 * Whether a base64 field holds a public key of `kind`: the type it names first, then its parts - an ed25519 key's 32
 * bytes, an RSA key's exponent and modulus, an ECDSA key's curve and point (RFC 8709, RFC 4253 §6.6, RFC 5656 §3.1).
 */
function holdsKey(kind: string, field: string): boolean {
  const blob = decode(field);
  const parts = blob && strings(blob);
  if (!parts || !parts.length || text(parts[0]) !== kind) return false;
  if (kind === 'ssh-ed25519') return parts.length === 2 && parts[1]!.length === 32;
  if (kind === 'ssh-rsa') return parts.length === 3 && parts[1]!.length > 0 && parts[2]!.length > 0;
  const curve = kind.replace(/^ecdsa-sha2-/, '');
  return parts.length === 3 && text(parts[1]) === curve && parts[2]!.length === POINT_BYTES[curve] && parts[2]![0] === 0x04;
}

/** Why `line` is no key a conversion host's key may be, in the API's words; null for one: `<type> <base64> [comment]`. */
export function publicKeyProblem(line: string): string | null {
  const trimmed = line.trim();
  if (!trimmed) return 'empty: no public key';
  if (trimmed.includes('PRIVATE KEY') || trimmed.startsWith('-----BEGIN') || trimmed.startsWith('PuTTY-User-Key-File')) {
    return 'a private key: declare its public key';
  }
  const fields = trimmed.split(/\s+/);
  const kind = fields[0]!;
  if (KEY_TYPES.includes(kind)) return fields.length < 2 || !holdsKey(kind, fields[1]!) ? `its base64 field holds no ${kind} key` : null;
  if (fields.slice(1).some((field) => KEY_TYPES.includes(field))) return 'options or a host name before the key type: declare the key alone';
  if (TYPE_NAME.test(kind)) return `${kind} is not an ed25519, ECDSA or RSA key`;
  return 'no OpenSSH public key';
}

/** `line {n}: {why}` for each line of a "Host keys" field that is no such key, n its line in the field; blank lines are none. */
export function declaredKeyProblems(field: string): string[] {
  return field.split('\n').flatMap((line, i) => {
    const why = line.trim() ? publicKeyProblem(line) : null;
    return why ? [`line ${i + 1}: ${why}`] : [];
  });
}

/** The keys a "Host keys" field declares, as the API takes them: each line trimmed, blank lines dropped. */
export function declaredKeys(field: string): string[] {
  return field
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean);
}

/** A public key's SHA256 fingerprint as `ssh-keygen -l` prints it: the unpadded base64 of its blob's SHA-256. */
export async function fingerprint(key: string): Promise<string> {
  const blob = decode(key.trim().split(/\s+/)[1] ?? '');
  if (!blob) throw new Error('no public key');
  const digest = new Uint8Array(await crypto.subtle.digest('SHA-256', blob));
  return `SHA256:${btoa(String.fromCharCode(...digest)).replace(/=+$/, '')}`;
}

/** The fingerprints of `keys`, each null for a key it cannot read; null until they are computed (the browser's digest is async). */
export function useFingerprints(keys: readonly string[]): Array<string | null> | null {
  const id = keys.join('\n');
  const [done, setDone] = useState<{ id: string; prints: Array<string | null> } | null>(null);
  useEffect(() => {
    let live = true;
    void Promise.all(id.split('\n').map((key) => fingerprint(key).catch(() => null))).then((prints) => {
      if (live) setDone({ id, prints });
    });
    return () => {
      live = false;
    };
  }, [id]);
  return done?.id === id ? done.prints : null;
}
