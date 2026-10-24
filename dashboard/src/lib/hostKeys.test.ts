import { describe, expect, it } from 'vitest';
import { declaredKeyProblems, declaredKeys, fingerprint, publicKeyProblem } from './hostKeys';

// what the API's public_key_problem says of each (seamless/src/seamless_migrate/security/host_keys.py, SDD §4.2)
const ECDSA =
  'ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTYAAABBBAEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQE= host';
const RSA =
  'ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQCrq6urq6urq6urq6urq6urq6urq6urq6urq6urq6urq6urq6urq6urq6urq6urq6urq6urq6urq6urq6urq6urq6urq6urq6urq6urq6urq6urq6urq6urq6urq6urq6urq6urq6urq6urq6urq6urq6urq6urq6urq6urq6urq6urq6urq6urq6urq6urq6urq6urq6urq6urq6urq6urq6urq6urq6urq6urq6urq6urq6urq6urq6urq6urq6urq6urq6urq6urq6urq6urq6urq6urq6urq6urq6urq6urq6urq6urq6urq6urq6urq6urq6urq6urq6urq6ur';
const ED25519 = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPG3KNdzNY5BMzcxmBiodxJzFyuk3o+NUwZvY9B8BPyn os-migrate-conv-dst';

describe("a conversion host's declared keys (SDD §4.2, §26.3; plan task ST7)", () => {
  it('takes an ed25519, ECDSA or RSA public key and refuses anything else in the API words, never quoting the line', () => {
    for (const key of [ED25519, ECDSA, RSA]) expect(publicKeyProblem(key), key.slice(0, 20)).toBeNull();
    const refused: Array<[string, string]> = [
      ['-----BEGIN OPENSSH PRIVATE KEY-----', 'a private key: declare its public key'],
      ['b3BlbnNzaC1rZXktdjEAAAAA PRIVATE KEY', 'a private key: declare its public key'],
      ['PuTTY-User-Key-File-3: ssh-ed25519', 'a private key: declare its public key'],
      ['conv.example.com ssh-rsa AAAA', 'options or a host name before the key type: declare the key alone'],
      ['sk-ssh-ed25519@openssh.com AAAA', 'sk-ssh-ed25519@openssh.com is not an ed25519, ECDSA or RSA key'],
      ['hello world', 'no OpenSSH public key'],
      ['ssh-ed25519', 'its base64 field holds no ssh-ed25519 key'],
      ['ssh-ed25519 AAAA$$$$', 'its base64 field holds no ssh-ed25519 key'],
      // padding Python's strict decoder refuses
      ['ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIONXdEMQsyylyTGj3eyOnSgAxp5zE4Y8rF+Fpabez7MG=', 'its base64 field holds no ssh-ed25519 key'],
      // an RSA key under the ed25519 type, a point a byte short, bytes left over, an empty modulus, another curve
      ['ssh-ed25519 AAAAB3NzaC1yc2E=', 'its base64 field holds no ssh-ed25519 key'],
      ['ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAHwICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgI=', 'its base64 field holds no ssh-ed25519 key'],
      ['ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAAA=', 'its base64 field holds no ssh-ed25519 key'],
      ['ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAAAAA==', 'its base64 field holds no ssh-rsa key'],
      [
        'ecdsa-sha2-nistp384 AAAAE2VjZHNhLXNoYTItbmlzdHAzODQAAAAIbmlzdHAyNTYAAABhBAEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQ==',
        'its base64 field holds no ecdsa-sha2-nistp384 key',
      ],
    ];
    for (const [line, why] of refused) expect(publicKeyProblem(line), line.slice(0, 20)).toBe(why);
  });

  it("names each refused line by its number in the field, and sends the others trimmed, blank lines dropped", () => {
    const text = `\n  ${ED25519}  \n\nhello world\n${RSA}\n`;
    expect(declaredKeyProblems(text)).toEqual(['line 4: no OpenSSH public key']);
    expect(declaredKeys(text)).toEqual([ED25519, 'hello world', RSA]);
    expect(declaredKeys(' \n')).toEqual([]);
  });

  it('prints a fingerprint as ssh-keygen -l does', async () => {
    await expect(fingerprint(ED25519)).resolves.toBe('SHA256:c7YZ2kyybPWsZp/yLpIl7YrkvASjTK9SIZdcPY+5aP0');
    await expect(fingerprint('ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIONXdEMQsyylyTGj3eyOnSgAxp5zE4Y8rF+Fpabez7MG')).resolves.toBe(
      'SHA256:sEjGO8jPw9A7YTcBql1IsdjRiT9LliDPmN1IP8DtJsY',
    );
    await expect(fingerprint(RSA)).resolves.toBe('SHA256:kgefOwF5TtIj3F2qVqbyhkKDG+4mS6L3+M7tCAkUZyo');
  });
});
