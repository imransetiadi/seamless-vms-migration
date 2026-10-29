import { act, fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { createMemoryRouter, Link, RouterProvider } from 'react-router-dom';
import { expect, it } from 'vitest';
import { UnsavedChangesGuard } from './UnsavedChangesGuard';
it('keeps edits on cancelled navigation and confirms internal navigation and Back', async () => {
  const user = userEvent.setup();
  const router = createMemoryRouter([{ path: '/edit', element: <><UnsavedChangesGuard dirty /><Link to="/other">Other</Link></> }, { path: '/other', element: <p>Other page</p> }], { initialEntries: ['/other', '/edit'] });
  render(<RouterProvider router={router} />);
  await user.click(screen.getByText('Other'));
  expect(screen.getByRole('alertdialog')).toHaveTextContent('Discard unsaved changes?');
  await user.click(screen.getByRole('button', { name: 'Keep editing' }));
  expect(router.state.location.pathname).toBe('/edit');
  const unload = new Event('beforeunload', { cancelable: true });
  fireEvent(window, unload);
  expect(unload.defaultPrevented).toBe(true);
  await act(() => router.navigate(-1));
  await screen.findByRole('alertdialog');
  await user.click(screen.getByRole('button', { name: 'Discard changes' }));
  expect(await screen.findByText('Other page')).toBeInTheDocument();
});

it('keeps pending requests on the page instead of discarding their result', async () => {
  const user = userEvent.setup();
  const router = createMemoryRouter([{ path: '/edit', element: <><UnsavedChangesGuard dirty pending /><Link to="/other">Other</Link></> }, { path: '/other', element: <p>Other page</p> }], { initialEntries: ['/edit'] });
  render(<RouterProvider router={router} />);
  await user.click(screen.getByText('Other'));
  expect(screen.getByRole('button', { name: 'Discard changes' })).toHaveAttribute('aria-disabled', 'true');
  expect(router.state.location.pathname).toBe('/edit');
});
