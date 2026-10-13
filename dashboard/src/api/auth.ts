/**
 * Bearer-token storage (SDD §16: the login page stores the token in sessionStorage, so it is
 * dropped when the tab closes). Storage can be unavailable (privacy modes) — every access is guarded.
 */
export const TOKEN_STORAGE_KEY = 'seamless.token';

export function getStoredToken(): string | null {
  try {
    return window.sessionStorage.getItem(TOKEN_STORAGE_KEY);
  } catch {
    return null;
  }
}

export function storeToken(token: string): void {
  try {
    window.sessionStorage.setItem(TOKEN_STORAGE_KEY, token);
  } catch {
    // Storage unavailable: the token lives only for this page load.
  }
}

export function clearStoredToken(): void {
  try {
    window.sessionStorage.removeItem(TOKEN_STORAGE_KEY);
  } catch {
    // ignore
  }
}

/**
 * Set when the server refused the stored token of a signed-in session (401, SDD §16), so Sign in says the session
 * ended. Kept in memory: a new page load is a new visit. Signing out clears the token without setting it.
 */
let ended = false;

/** A 401 for the stored token: drop it once and mark that the session ended. False when no token was stored. */
export function endSession(): boolean {
  if (!getStoredToken()) return false;
  clearStoredToken();
  ended = true;
  return true;
}

/** Whether the last session ended because its token was refused; reading it changes nothing. */
export function sessionEnded(): boolean {
  return ended;
}

export function clearSessionEnded(): void {
  ended = false;
}
