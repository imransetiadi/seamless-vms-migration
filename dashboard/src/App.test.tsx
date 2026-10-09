import { QueryClient } from '@tanstack/react-query';
import { render, screen, waitFor, within } from '@testing-library/react';
import { StrictMode } from 'react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeAll, describe, expect, it } from 'vitest';
import App from './App';
import { ApiClient } from './api/client';
import { createMockFetch } from './api/mock';
import { createTestServer } from './test/utils';

function renderApp(token: string | null, strict = false) {
  const server = createTestServer();
  const client = new ApiClient({ getToken: () => token, fetchImpl: createMockFetch(server) });
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  const app = <App client={client} queryClient={queryClient} />;
  render(strict ? <StrictMode>{app}</StrictMode> : app);
  return server;
}

// React.lazy pages are compiled by Vite on first import; warm them so a cold transform under a busy
// parallel run cannot exceed the query timeouts (the assertions measure the app, not the bundler).
beforeAll(async () => {
  await Promise.all([import('./pages/Overview'), import('./pages/Events'), import('./pages/Login')]);
}, 30_000);

afterEach(() => {
  window.history.pushState({}, '', '/');
});

const PAGE_TIMEOUT = { timeout: 5_000 };

describe('App shell', () => {
  it('renders the shell with a skip link, primary navigation and the overview', async () => {
    renderApp('operator');
    expect(await screen.findByRole('heading', { level: 1, name: 'Overview' }, PAGE_TIMEOUT)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /skip to main content/i })).toHaveAttribute('href', '#main');
    const nav = screen.getAllByRole('navigation', { name: 'Primary' })[0] as HTMLElement;
    expect(within(nav).getByRole('link', { name: 'Overview' })).toHaveAttribute('aria-current', 'page');
    expect(screen.getByRole('main')).toHaveAttribute('id', 'main');
  });

  it('navigates between pages and marks the current one', async () => {
    const user = userEvent.setup({ delay: null });
    renderApp('operator');
    await screen.findByRole('heading', { level: 1, name: 'Overview' }, PAGE_TIMEOUT);
    const nav = screen.getAllByRole('navigation', { name: 'Primary' })[0] as HTMLElement;

    await user.click(within(nav).getByRole('link', { name: 'Events' }));
    expect(await screen.findByRole('heading', { level: 1, name: 'Events' }, PAGE_TIMEOUT)).toBeInTheDocument();
    expect(within(nav).getByRole('link', { name: 'Events' })).toHaveAttribute('aria-current', 'page');
    // the page sets its title in an effect, which may run after the heading is in the DOM
    await waitFor(() => expect(document.title).toBe('Events · Seamless Migrate'));
  });

  it('leaves focus alone on first load (even in StrictMode) and moves it to main after navigating', async () => {
    const user = userEvent.setup({ delay: null });
    renderApp('operator', true);
    await screen.findByRole('heading', { level: 1, name: 'Overview' }, PAGE_TIMEOUT);
    // The first Tab must reach the skip link, so nothing may steal focus on load.
    expect(document.activeElement).toBe(document.body);

    const nav = screen.getAllByRole('navigation', { name: 'Primary' })[0] as HTMLElement;
    await user.click(within(nav).getByRole('link', { name: 'Providers' }));
    await screen.findByRole('heading', { level: 1, name: 'Providers' }, PAGE_TIMEOUT);
    // focus moves in the layout's effect after the navigation commits
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('main')));
  });

  it('sends an unauthenticated user to the sign-in page', async () => {
    renderApp('revoked-token');
    expect(await screen.findByRole('heading', { level: 1, name: 'Sign in' }, PAGE_TIMEOUT)).toBeInTheDocument();
    expect(window.location.pathname).toBe('/login');
  });
});
