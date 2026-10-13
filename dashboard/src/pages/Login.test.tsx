import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Route, Routes } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import { clearSessionEnded, endSession, storeToken, TOKEN_STORAGE_KEY } from '../api/auth';
import { renderWithApp } from '../test/utils';
import Login from './Login';

function renderLogin() {
  return renderWithApp(
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route path="/" element={<p>Home page</p>} />
    </Routes>,
    { route: '/login', storedToken: true },
  );
}

describe('Login', () => {
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
