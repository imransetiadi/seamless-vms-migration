import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import type { NotificationChannelView } from '../api/types';
import { createTestClient, createTestServer, renderWithApp, TEST_NOW } from '../test/utils';
import Notifications from './Notifications';

// a made-up incoming webhook path, too short for a secret scanner's pattern (plan: synthetic fixtures)
const SLACK_URL = 'https://hooks.slack.com/services/T000/B000/notasecret';
const AT = new Date(TEST_NOW).toISOString();

describe('the notification catalog (SDD §16, §24.9, plan task NT9)', () => {
  it('lets only an admin change a channel', async () => {
    const user = userEvent.setup();
    const server = createTestServer();
    const view = renderWithApp(<Notifications />, { route: '/notifications', server, token: 'admin' });
    const catalog = await screen.findByRole('list', { name: 'Channel catalog' });
    // the seeded channel of each kind, in name order
    expect(within(catalog).getAllByRole('listitem').map((item) => item.getAttribute('aria-label'))).toEqual(['itsm-webhook', 'ops-mailbox', 'ops-slack']);

    // an email channel by default: its recipients, one a line
    expect(screen.getByLabelText('Kind')).toHaveValue('email');
    await user.type(screen.getByLabelText(/^Name/), 'app-owners');
    await user.type(screen.getByLabelText(/^Recipients/), 'owner@example.com\nbackup@example.com');
    await user.click(screen.getByRole('button', { name: 'Add channel' }));
    expect(await within(catalog).findByRole('listitem', { name: 'app-owners' })).toBeInTheDocument();
    expect(await createTestClient(server, 'admin').get('/notifications/channels/app-owners')).toMatchObject({
      kind: 'email',
      recipients: ['owner@example.com', 'backup@example.com'],
      recipient_count: 2,
      enabled: true,
      cursor: null,
    });
    await waitFor(() => expect(screen.getByLabelText(/^Name/)).toHaveValue(''));

    // a refused registration keeps what was typed and shows the API's reason
    await user.selectOptions(screen.getByLabelText('Kind'), 'webhook');
    await user.type(screen.getByLabelText(/^Name/), 'itsm-2');
    await user.type(screen.getByLabelText(/^URL/), 'http://itsm.example.com/hook');
    await user.click(screen.getByRole('button', { name: 'Add channel' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Value error, url: an https:// URL with a host');
    expect(screen.getByLabelText(/^URL/)).toHaveValue('http://itsm.example.com/hook');

    // Disable sends the PATCH's enabled; a disabled channel's test is unavailable, saying why
    const owners = within(catalog).getByRole('listitem', { name: 'app-owners' });
    expect(owners).toHaveTextContent('Enabled');
    await user.click(within(owners).getByRole('button', { name: 'Disable app-owners' }));
    await waitFor(() => expect(within(catalog).getByRole('listitem', { name: 'app-owners' })).toHaveTextContent('Disabled'));
    expect(within(within(catalog).getByRole('listitem', { name: 'app-owners' })).getByRole('button', { name: 'Send test to app-owners' })).toHaveAccessibleDescription(
      'Enable the channel first.',
    );
    expect(await createTestClient(server, 'viewer').get('/notifications/channels/app-owners')).toMatchObject({ enabled: false });

    // Edit opens the form with the channel's values, its name and kind read only
    await user.click(within(within(catalog).getByRole('listitem', { name: 'app-owners' })).getByRole('button', { name: 'Edit app-owners' }));
    // the form takes the focus a frame later, as the Hooks page's does: type once it has
    await waitFor(() => expect(screen.getByLabelText(/^Name/)).toHaveFocus());
    expect(screen.getByLabelText(/^Name/)).toHaveValue('app-owners');
    expect(screen.getByLabelText(/^Name/)).toHaveAttribute('readonly');
    expect(screen.getByLabelText('Kind')).toBeDisabled();
    await user.type(screen.getByLabelText(/^Description/), 'The owners of the billing apps');
    await user.click(screen.getByRole('button', { name: 'Save channel' }));
    await waitFor(async () => expect(await createTestClient(server, 'admin').get('/notifications/channels/app-owners')).toMatchObject({ description: 'The owners of the billing apps', enabled: false }));

    // a delete is refused while a subscription names the channel, and the refusal names it
    await user.click(within(within(catalog).getByRole('listitem', { name: 'ops-mailbox' })).getByRole('button', { name: 'Delete ops-mailbox' }));
    const dialog = await screen.findByRole('alertdialog');
    expect(dialog).toHaveTextContent('Channel ops-mailbox');
    await user.click(within(dialog).getByRole('button', { name: 'Delete channel' }));
    expect(await within(dialog).findByRole('alert')).toHaveTextContent(/channel ops-mailbox is named by subscription\(s\) sub-[0-9a-f]{8} \(every plan\): remove them first/);
    await user.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    // a channel nothing names goes, with its cursor
    await user.click(within(within(catalog).getByRole('listitem', { name: 'app-owners' })).getByRole('button', { name: 'Delete app-owners' }));
    await user.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Delete channel' }));
    await waitFor(() => expect(within(catalog).queryByRole('listitem', { name: 'app-owners' })).not.toBeInTheDocument());
    view.unmount();

    // any other role reads the catalog and is told why it cannot change a channel
    for (const token of ['viewer', 'operator', 'approver']) {
      const other = renderWithApp(<Notifications />, { route: '/notifications', server, token });
      const list = await screen.findByRole('list', { name: 'Channel catalog' });
      expect(within(list).getByRole('listitem', { name: 'ops-mailbox' })).toBeInTheDocument();
      expect(screen.getByText('Changing channels requires the admin role.')).toHaveAttribute('role', 'status');
      expect(screen.queryByRole('button', { name: 'Add channel' })).not.toBeInTheDocument();
      expect(screen.queryByRole('button', { name: /^(Delete|Edit|Disable|Enable|Set secret for|Send test to) / })).not.toBeInTheDocument();
      await expect(createTestClient(server, token).post('/notifications/channels', { name: 'other', kind: 'slack' })).rejects.toMatchObject({ status: 403 });
      other.unmount();
    }
  });

  it('never shows a Slack URL or a signing key', async () => {
    const user = userEvent.setup();
    const server = createTestServer();
    renderWithApp(<Notifications />, { route: '/notifications', server, token: 'admin' });
    const catalog = await screen.findByRole('list', { name: 'Channel catalog' });
    // an email channel has no secret
    expect(within(within(catalog).getByRole('listitem', { name: 'ops-mailbox' })).queryByRole('button', { name: /Set secret/ })).not.toBeInTheDocument();

    // the Slack webhook's URL is typed into the dialog and never shown again
    await user.click(within(within(catalog).getByRole('listitem', { name: 'ops-slack' })).getByRole('button', { name: 'Set secret for ops-slack' }));
    let dialog = await screen.findByRole('dialog', { name: 'Set secret for ops-slack' });
    await user.type(within(dialog).getByLabelText(/^Slack webhook URL/), SLACK_URL);
    await user.click(within(dialog).getByRole('button', { name: 'Save secret' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(document.body.innerHTML).not.toContain('notasecret');
    const admin = createTestClient(server, 'admin');
    expect(JSON.stringify(await admin.get<NotificationChannelView>('/notifications/channels/ops-slack'))).not.toContain('notasecret');
    expect(JSON.stringify(await admin.get<NotificationChannelView[]>('/notifications/channels'))).not.toContain('notasecret');

    // a webhook's signing key, generated in the browser, is shown once in the dialog and nowhere after it
    await user.click(within(within(catalog).getByRole('listitem', { name: 'itsm-webhook' })).getByRole('button', { name: 'Set secret for itsm-webhook' }));
    dialog = await screen.findByRole('dialog', { name: 'Set secret for itsm-webhook' });
    await user.click(within(dialog).getByRole('button', { name: 'Generate key' }));
    const key = (within(dialog).getByLabelText(/^Signing key/) as HTMLInputElement).value;
    // whsec_ and the base64 of 32 random bytes (SDD §16, §24.3)
    expect(key).toMatch(/^whsec_[A-Za-z0-9+/]{43}=$/);
    expect(dialog).toHaveTextContent('shown once');
    await user.click(within(dialog).getByRole('button', { name: 'Save secret' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(document.body.innerHTML).not.toContain(key.slice(6));
    // the catalog's events carry the fields set, never a value (SDD §24.7)
    const secretEvents = server.events.filter((event) => event.kind === 'channel.updated');
    expect(secretEvents.map((event) => event.data)).toEqual([
      { name: 'ops-slack', kind: 'slack', fields: ['secret'] },
      { name: 'itsm-webhook', kind: 'webhook', fields: ['secret'] },
    ]);
    expect(JSON.stringify(server.events)).not.toContain('notasecret');
    expect(JSON.stringify(server.events)).not.toContain(key.slice(6));
    // a channel the admin reads again still answers no secret
    expect(JSON.stringify(await admin.get<NotificationChannelView>('/notifications/channels/itsm-webhook'))).not.toContain(key.slice(6));
  });

  it('shows recipients to an admin and their number to everyone else', async () => {
    const server = createTestServer();
    const admin = renderWithApp(<Notifications />, { route: '/notifications', server, token: 'admin' });
    let catalog = await screen.findByRole('list', { name: 'Channel catalog' });
    const mailbox = within(catalog).getByRole('listitem', { name: 'ops-mailbox' });
    expect(mailbox).toHaveTextContent('ops@example.com');
    expect(mailbox).toHaveTextContent('migration-team@example.com');
    expect(within(catalog).getByRole('listitem', { name: 'itsm-webhook' })).toHaveTextContent('https://itsm.example.com/hooks/seamless');
    admin.unmount();

    for (const token of ['viewer', 'operator', 'approver']) {
      const other = renderWithApp(<Notifications />, { route: '/notifications', server, token });
      catalog = await screen.findByRole('list', { name: 'Channel catalog' });
      const item = within(catalog).getByRole('listitem', { name: 'ops-mailbox' });
      expect(item).toHaveTextContent('2 recipients');
      expect(item).not.toHaveTextContent('example.com');
      const webhook = within(catalog).getByRole('listitem', { name: 'itsm-webhook' });
      expect(webhook).toHaveTextContent('itsm.example.com');
      expect(webhook).not.toHaveTextContent('/hooks/seamless');
      other.unmount();
    }
  });

  it("shows a channel's test outcome", async () => {
    const user = userEvent.setup();
    const server = createTestServer();
    // a Slack channel whose secret was never set
    server.channels.set('new-slack', { name: 'new-slack', kind: 'slack', description: null, recipients: [], url: null, redact_names: false, enabled: true, created_at: AT, updated_at: AT });
    renderWithApp(<Notifications />, { route: '/notifications', server, token: 'admin' });
    const catalog = await screen.findByRole('list', { name: 'Channel catalog' });
    // the seeded state: the last outcome with its reason and the failures in a row, or that no message went yet
    const webhook = within(catalog).getByRole('listitem', { name: 'itsm-webhook' });
    expect(webhook).toHaveTextContent('Failed');
    expect(webhook).toHaveTextContent('HTTP 503');
    expect(webhook).toHaveTextContent('2 failures in a row');
    const mailbox = within(catalog).getByRole('listitem', { name: 'ops-mailbox' });
    expect(mailbox).toHaveTextContent('Sent');
    expect(within(catalog).getByRole('listitem', { name: 'new-slack' })).toHaveTextContent('No message yet');

    await user.click(within(mailbox).getByRole('button', { name: 'Send test to ops-mailbox' }));
    expect(await within(mailbox).findByRole('status')).toHaveTextContent('Test sent');
    const slack = within(catalog).getByRole('listitem', { name: 'new-slack' });
    await user.click(within(slack).getByRole('button', { name: 'Send test to new-slack' }));
    expect(await within(slack).findByRole('status')).toHaveTextContent('Test failed: no_secret');
    // each test is recorded as a delivery with the trigger test (SDD §12, §24.3)
    expect(server.events.filter((event) => event.kind.startsWith('notification.')).map((event) => [event.kind, event.data.channel, event.data.trigger])).toEqual([
      ['notification.sent', 'ops-mailbox', 'test'],
      ['notification.failed', 'new-slack', 'test'],
    ]);
  });
});
