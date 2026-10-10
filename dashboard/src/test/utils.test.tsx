import { describe, expect, it } from 'vitest';
import { useMe } from '../api/hooks';
import { renderWithApp } from './utils';

describe('renderWithApp', () => {
  it('seeds the session like RequireAuth: a page sees its role at the first render', () => {
    // a role-gated action must not race GET /me: in the app, RequireAuth renders pages only once /me loaded
    const seen: Array<string | undefined> = [];
    function Page() {
      seen.push(useMe().data?.role);
      return null;
    }
    renderWithApp(<Page />, { token: 'operator' });
    expect(seen[0]).toBe('operator');
  });

  it('leaves an unknown token unauthenticated, as GET /me refuses it', () => {
    const seen: Array<string | undefined> = [];
    function Page() {
      seen.push(useMe().data?.role);
      return null;
    }
    renderWithApp(<Page />, { token: 'revoked-token' });
    expect(seen[0]).toBeUndefined();
  });
});
