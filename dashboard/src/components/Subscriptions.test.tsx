import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { expect, it } from 'vitest';
import { renderWithApp } from '../test/utils';
import { PlanNotificationsPanel, SubscriptionForm } from './Subscriptions';

it('distinguishes a loading catalog from an empty catalog', () => {
  renderWithApp(<SubscriptionForm planId={null} channels={undefined} />);
  expect(screen.getByRole('option', { name: 'Loading channels…' })).toBeInTheDocument();
});

it('keeps keyboard focus in the plan notification panel after removing a subscription', async () => {
  const user = userEvent.setup();
  renderWithApp(<PlanNotificationsPanel planId="plan-4f2a9c1e" />);
  await user.click(await screen.findByRole('button', { name: 'Remove ops-slack' }));
  await waitFor(() => expect(screen.queryByRole('button', { name: 'Remove ops-slack' })).not.toBeInTheDocument());
  expect(screen.getByRole('heading', { name: 'Notifications' })).toHaveFocus();
});

it('offers Retry when the plan notification channel catalog fails, then enables adding', async () => {
  const user = userEvent.setup();
  let failing = true;
  renderWithApp(<PlanNotificationsPanel planId="plan-c81d44a0" />, {
    wrapFetch: (fetchImpl) => async (input, init) => {
      if (failing && String(input).includes('/notifications/channels')) {
        return new Response(JSON.stringify({ detail: 'Catalog unavailable' }), { status: 503 });
      }
      return fetchImpl(input, init);
    },
  });
  const error = await screen.findByRole('alert');
  expect(error).toHaveTextContent('Channels are unavailable');
  expect(screen.getByRole('option', { name: 'Channels unavailable' })).toBeInTheDocument();
  failing = false;
  await user.click(within(error).getByRole('button', { name: 'Retry' }));
  await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument());
  expect(screen.getByLabelText('Channel')).toBeEnabled();
  expect(screen.getByRole('button', { name: 'Add subscription' })).not.toHaveAttribute('aria-disabled');
});
