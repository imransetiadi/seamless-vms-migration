import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Route, Routes } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import { clearSessionEnded, endSession, storeToken, TOKEN_STORAGE_KEY } from '../api/auth';
import { createTestServer, renderWithApp, type RenderOptions } from '../test/utils';
import Login from './Login';

function renderLogin(options: RenderOptions = {}) {
  return renderWithApp(
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route path="/" element={<p>Home page</p>} />
    </Routes>,
    { route: '/login', storedToken: true, ...options },
  );
}

describe('Login', () => {
  it('keeps the submitted token stable and ignores Enter while sign-in is pending', async () => {
    const user = userEvent.setup({ delay: null });
    let attempts = 0;
    let release!: () => void;
    const pending = new Promise<void>((resolve) => { release = resolve; });
    renderLogin({ wrapFetch: (mock) => async (input, init) => {
      if (new URL(String(input), 'http://localhost').pathname.endsWith('/me')) {
        attempts += 1;
        await pending;
      }
      return mock(input, init);
    } });
    const input = await screen.findByLabelText('API token');
    await user.type(input, 'not-a-valid-token');
    await user.keyboard('{Enter}');
    await waitFor(() => expect(attempts).toBe(1));
    await user.type(input, 'edited{Enter}{Enter}');
    try {
      expect(input).toHaveValue('not-a-valid-token');
      expect(input).toHaveAttribute('readonly');
      expect(input).toHaveFocus();
      expect(attempts).toBe(1);
    } finally {
      release();
    }
    await screen.findByRole('alert');
    expect(input).not.toHaveAttribute('readonly');
    await user.clear(input);
    await user.type(input, 'viewer{Enter}');
    await screen.findByText('Home page');
    expect(attempts).toBe(2);
  });

  it('says the session ended when a signed-in request was refused, once, and not after signing out (SDD §16)', async () => {
    storeToken('smg_test-token-not-real');
    endSession(); // what the client does on a 401 for a stored token
    const view = renderLogin();
    const note = await screen.findByRole('status', { name: /session ended/i });
    expect(note).toHaveTextContent('Your session ended: the server no longer accepts your token. It was revoked, or the server\u2019s tokens changed. Sign in with a valid token.');
    // a note shown on load is not reliably announced: the field it is about says it too
    expect(screen.getByLabelText('API token')).toHaveAccessibleDescription(/^Your session ended: the server no longer accepts your token\./);
    view.unmount();
    // shown once: the next visit is an ordinary sign-in
    renderLogin();
    await screen.findByLabelText('API token');
    expect(screen.queryByRole('status', { name: /session ended/i })).not.toBeInTheDocument();
    clearSessionEnded();
  });

  it('offers to continue without a token only when GET /health says authentication is off, never asking GET /me (SDD §16)', async () => {
    const requests: string[] = [];
    const record =
      (fetchImpl: typeof fetch): typeof fetch =>
      (input, init) => {
        requests.push(String(input));
        return fetchImpl(input, init);
      };
    // the mock runs without authentication: no token is the anonymous admin
    const view = renderLogin({ wrapFetch: record });
    const link = await screen.findByRole('link', { name: 'continue without a token' });
    expect(link).toHaveAttribute('href', '/');
    expect(link.closest('p')).toHaveTextContent(/^Authentication is off on this server: continue without a token\.$/);
    view.unmount();
    // a demo with authentication on: the link would lead straight back to Sign in
    const server = createTestServer();
    server.health = { ...server.health, demo: true, auth_disabled: false };
    renderLogin({ server, wrapFetch: record });
    await screen.findByText(/^Control plane .* reachable · demo mode$/);
    expect(screen.queryByRole('link', { name: 'continue without a token' })).not.toBeInTheDocument();
    // with authentication on, GET /me without a token is refused: an auth.denied event and a lockout tick each visit
    expect(requests.filter((url) => new URL(url, 'http://dashboard.test').pathname.endsWith('/me'))).toEqual([]);
  });

  it('shows the brand mark on the sign-in card (SDD §16)', async () => {
    renderLogin();
    await screen.findByLabelText('API token');
    expect(document.querySelector('main svg[data-brand-mark]')).toHaveAttribute('aria-hidden', 'true');
  });

  it('labels the token field and lets password managers fill it', async () => {
    renderLogin();
    const input = await screen.findByLabelText('API token');
    expect(input).toHaveAttribute('type', 'password');
    expect(input).toHaveAttribute('autocomplete', 'current-password');
    expect(screen.getByRole('button', { name: /show token/i })).toHaveAttribute('aria-pressed', 'false');
  });

  it('never says the session ended after a rejected sign-in: it was no session, and signing out never says so (SDD §16, review RR-06)', async () => {
    const user = userEvent.setup({ delay: null });
    // the app's 401 handler (main.tsx): a refused request drops the stored token and marks the session ended
    const view = renderLogin({ onUnauthorized: () => void endSession() });
    await user.type(await screen.findByLabelText('API token'), 'not-a-valid-token');
    await user.click(screen.getByRole('button', { name: /^sign in/i }));
    expect(await screen.findByRole('alert')).toHaveTextContent(/token was rejected/i);
    view.unmount();
    // the next time Sign in shows - after a good token and a sign-out, say - it is an ordinary sign-in
    renderLogin();
    await screen.findByLabelText('API token');
    expect(screen.queryByRole('status', { name: /session ended/i })).not.toBeInTheDocument();
    clearSessionEnded();
  });

  it('keeps the user on the page with a clear error when the token is rejected', async () => {
    const user = userEvent.setup({ delay: null });
    renderLogin();

    await user.type(await screen.findByLabelText('API token'), 'not-a-valid-token');
    await user.click(screen.getByRole('button', { name: /^sign in/i }));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/token was rejected/i);
    expect(screen.getByLabelText('API token')).toHaveAttribute('aria-invalid', 'true');
    expect(window.sessionStorage.getItem(TOKEN_STORAGE_KEY)).toBeNull();
  });

  it('explains the lockout when the address made too many failed attempts', async () => {
    const user = userEvent.setup({ delay: null });
    renderLogin();

    await user.type(await screen.findByLabelText('API token'), 'locked');
    await user.click(screen.getByRole('button', { name: /^sign in/i }));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/too many failed sign-in attempts .* wait a minute/i);
    expect(window.sessionStorage.getItem(TOKEN_STORAGE_KEY)).toBeNull();
  });

  it('stores a valid token for this tab only and returns to the app', async () => {
    const user = userEvent.setup({ delay: null });
    renderLogin();

    await user.type(await screen.findByLabelText('API token'), 'approver');
    await user.click(screen.getByRole('button', { name: /^sign in/i }));

    expect(await screen.findByText('Home page')).toBeInTheDocument();
    expect(window.sessionStorage.getItem(TOKEN_STORAGE_KEY)).toBe('approver');
  });

  it('asks for a token before calling the server', async () => {
    const user = userEvent.setup({ delay: null });
    renderLogin();
    await user.click(await screen.findByRole('button', { name: /^sign in/i }));
    expect(within(await screen.findByRole('alert')).getByText(/paste your api token/i)).toBeInTheDocument();
  });
});
