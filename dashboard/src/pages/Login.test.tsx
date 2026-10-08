import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Route, Routes } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import { TOKEN_STORAGE_KEY } from '../api/auth';
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
  it('labels the token field and lets password managers fill it', async () => {
    renderLogin();
    const input = await screen.findByLabelText('API token');
    expect(input).toHaveAttribute('type', 'password');
    expect(input).toHaveAttribute('autocomplete', 'current-password');
    expect(screen.getByRole('button', { name: /show token/i })).toHaveAttribute('aria-pressed', 'false');
  });

  it('keeps the user on the page with a clear error when the token is rejected', async () => {
    const user = userEvent.setup();
    renderLogin();

    await user.type(await screen.findByLabelText('API token'), 'not-a-valid-token');
    await user.click(screen.getByRole('button', { name: /^sign in/i }));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/token was rejected/i);
    expect(screen.getByLabelText('API token')).toHaveAttribute('aria-invalid', 'true');
    expect(window.sessionStorage.getItem(TOKEN_STORAGE_KEY)).toBeNull();
  });

  it('explains the lockout when the address made too many failed attempts', async () => {
    const user = userEvent.setup();
    renderLogin();

    await user.type(await screen.findByLabelText('API token'), 'locked');
    await user.click(screen.getByRole('button', { name: /^sign in/i }));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/too many failed sign-in attempts .* wait a minute/i);
    expect(window.sessionStorage.getItem(TOKEN_STORAGE_KEY)).toBeNull();
  });

  it('stores a valid token for this tab only and returns to the app', async () => {
    const user = userEvent.setup();
    renderLogin();

    await user.type(await screen.findByLabelText('API token'), 'approver');
    await user.click(screen.getByRole('button', { name: /^sign in/i }));

    expect(await screen.findByText('Home page')).toBeInTheDocument();
    expect(window.sessionStorage.getItem(TOKEN_STORAGE_KEY)).toBe('approver');
  });

  it('asks for a token before calling the server', async () => {
    const user = userEvent.setup();
    renderLogin();
    await user.click(await screen.findByRole('button', { name: /^sign in/i }));
    expect(within(await screen.findByRole('alert')).getByText(/paste your api token/i)).toBeInTheDocument();
  });
});
