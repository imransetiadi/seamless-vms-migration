import { describe, expect, it } from 'vitest';
// One case table for the control plane and the dashboard (SDD §9.5).
import raw from '../../../seamless/tests/fixtures/guest_os_cases.json?raw';
import { identifyGuestOs } from './guestOs';

const fixture = JSON.parse(raw) as {
  cases: Array<{ in: string | null } & Record<string, unknown>>;
};

describe('guest OS catalog (SDD §9.5)', () => {
  it.each(fixture.cases.map((c) => [String(c.in), c] as const))('identifies %s like the control plane', (_name, c) => {
    const { in: input, ...expected } = c;
    expect(identifyGuestOs(input)).toEqual(expected);
  });
});
