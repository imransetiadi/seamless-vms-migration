import { afterEach, describe, expect, it } from 'vitest';
import { clearSessionEnded, endSession, getStoredToken, sessionEnded, storeToken } from './auth';

describe('a session that ended (SDD §16)', () => {
  afterEach(() => {
    clearSessionEnded();
    window.sessionStorage.clear();
  });

  it('drops a token the server refused once, and marks that the session ended', () => {
    storeToken('smg_test-token-not-real');
    expect(endSession()).toBe(true);
    expect(getStoredToken()).toBeNull();
    expect(sessionEnded()).toBe(true);
    // reading it changes nothing; clearing it does
    expect(sessionEnded()).toBe(true);
    clearSessionEnded();
    expect(sessionEnded()).toBe(false);
  });

  it('marks nothing without a stored token: a refused request of an anonymous page ends no session', () => {
    expect(endSession()).toBe(false);
    expect(sessionEnded()).toBe(false);
  });
});
