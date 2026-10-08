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
