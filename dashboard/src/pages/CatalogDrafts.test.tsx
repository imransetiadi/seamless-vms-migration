import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { createTestClient, createTestServer, renderWithApp } from '../test/utils';
import Hooks from './Hooks';
import Notifications from './Notifications';

describe('catalog editing protects operator work (SDD §16)', () => {
  it('keeps an implicit keyboard submission focused while its fields are locked', async () => {
    const user = userEvent.setup();
    let release!: () => void;
    const held = new Promise<void>((resolve) => { release = resolve; });
    renderWithApp(<Notifications />, {
      wrapFetch: (fetchImpl) => async (input, init) => {
        if (init?.method === 'POST' && String(input).endsWith('/notifications/channels')) await held;
        return fetchImpl(input, init);
      },
    });
    await screen.findByRole('list', { name: 'Channel catalog' });
    const name = screen.getByLabelText(/^Name/);
    await user.click(name);
    await user.paste('pending-channel');
    await user.selectOptions(screen.getByLabelText('Kind'), 'slack');
    await user.click(name);
    await user.keyboard('{Enter}');
    try {
      const submit = screen.getByRole('button', { name: 'Add channel' });
      expect(submit).toHaveAttribute('aria-busy', 'true');
      expect(name).toBeDisabled();
      expect(submit).toHaveFocus();
    } finally {
      release();
    }
    await screen.findByRole('listitem', { name: 'pending-channel' });
  });

  it('asks before replacing a channel draft and keeps its input on Keep editing', async () => {
    const user = userEvent.setup();
    renderWithApp(<Notifications />);
    await screen.findByRole('list', { name: 'Channel catalog' });
    await user.click(screen.getByLabelText(/^Name/));
    await user.paste('keep-this-channel');
    await user.click(screen.getByRole('button', { name: 'Edit itsm-webhook' }));
    const warning = await screen.findByRole('alertdialog');
    await user.click(within(warning).getByRole('button', { name: 'Keep editing' }));
    expect(screen.getByLabelText(/^Name/)).toHaveValue('keep-this-channel');
    await user.click(screen.getByRole('button', { name: 'Edit itsm-webhook' }));
    await user.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Discard changes' }));
    await waitFor(() => expect(screen.getByLabelText(/^Name/)).toHaveFocus());
    expect(screen.getByLabelText(/^Name/)).toHaveValue('itsm-webhook');
  });

  it('asks before cancelling changed hook input and clears a confirmed discard', async () => {
    const user = userEvent.setup();
    const server = createTestServer();
    await createTestClient(server).post('/hooks', { name: 'audit-hook', kind: 'playbook', playbook: '/opt/hooks/audit.yml' });
    renderWithApp(<Hooks />, { server });
    await user.click(await screen.findByRole('button', { name: 'Edit audit-hook' }));
    await waitFor(() => expect(screen.getByLabelText(/^Name/)).toHaveFocus());
    await user.click(screen.getByLabelText('Description'));
    await user.paste('Keep this description');
    await user.click(screen.getByRole('button', { name: 'Cancel' }));
    await user.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Keep editing' }));
    expect(screen.getByLabelText('Description')).toHaveValue('Keep this description');
    await user.click(screen.getByRole('button', { name: 'Cancel' }));
    await user.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Discard changes' }));
    await waitFor(() => expect(screen.getByLabelText(/^Name/)).toHaveFocus());
    expect(screen.getByLabelText(/^Name/)).toHaveValue('');
  });

  it('keeps an unsaved signing key through Escape, then clears it after confirmed discard', async () => {
    const user = userEvent.setup();
    renderWithApp(<Notifications />);
    await user.click(await screen.findByRole('button', { name: 'Set secret for itsm-webhook' }));
    await user.click(screen.getByRole('button', { name: 'Generate key' }));
    const key = (screen.getByLabelText(/^Signing key/) as HTMLInputElement).value;
    await user.keyboard('{Escape}');
    const warning = await screen.findByRole('alertdialog');
    await user.click(within(warning).getByRole('button', { name: 'Keep editing' }));
    expect(screen.getByLabelText(/^Signing key/)).toHaveValue(key);
    await user.keyboard('{Escape}');
    await user.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Discard changes' }));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(document.body.innerHTML).not.toContain(key.slice(6));
    await user.click(screen.getByRole('button', { name: 'Set secret for itsm-webhook' }));
    expect(screen.getByLabelText(/^Signing key/)).toHaveValue('');
  });

  it('returns focus to the catalog and announces a channel deletion', async () => {
    const user = userEvent.setup();
    const server = createTestServer();
    await createTestClient(server).post('/notifications/channels', { name: 'audit-slack', kind: 'slack' });
    renderWithApp(<Notifications />, { server });
    await user.click(await screen.findByRole('button', { name: 'Delete audit-slack' }));
    await user.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Delete channel' }));
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Delete audit-slack' })).not.toBeInTheDocument());
    await waitFor(() => expect(screen.getByRole('heading', { name: 'Catalog' })).toHaveFocus());
    expect(screen.getAllByRole('status').some((node) => node.textContent === 'Channel audit-slack deleted.')).toBe(true);
  });

  it('prevents a second catalog operation from replacing an in-flight update', async () => {
    const user = userEvent.setup();
    let release!: () => void;
    const held = new Promise<void>((resolve) => { release = resolve; });
    renderWithApp(<Notifications />, {
      wrapFetch: (fetchImpl) => async (input, init) => {
        if (init?.method === 'PATCH') await held;
        return fetchImpl(input, init);
      },
    });
    await user.click(await screen.findByRole('button', { name: 'Disable ops-mailbox' }));
    try {
      expect(screen.getByRole('button', { name: 'Disable ops-slack' })).toHaveAttribute('aria-disabled', 'true');
      expect(screen.getByRole('button', { name: 'Edit itsm-webhook' })).toHaveAttribute('aria-disabled', 'true');
    } finally {
      release();
    }
    await waitFor(() => expect(screen.getByRole('button', { name: 'Enable ops-mailbox' })).toBeInTheDocument());
  });
});
