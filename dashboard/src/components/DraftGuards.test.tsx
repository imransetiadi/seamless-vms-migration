import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { expect, it } from 'vitest';
import { renderWithApp } from '../test/utils';
import { ProviderDialog } from './ProviderDialog';
import { PolicyDialog } from './PolicyDialog';

it.each(['provider', 'policy'])('protects changed %s on Escape and Cancel', async (kind) => {
  const user = userEvent.setup();
  renderWithApp(kind === 'provider' ? <ProviderDialog open onClose={() => {}} /> : <PolicyDialog open defaultThreshold={0.8} onClose={() => {}} onSaved={() => {}} />);
  const dialog = screen.getByRole('dialog');
  const name = within(dialog).getByLabelText(/^name|^policy name/i);
  await user.type(name, 'Keep this draft');
  await user.keyboard('{Escape}');
  expect(await screen.findByRole('alertdialog')).toHaveTextContent('Discard unsaved changes?');
  await user.click(screen.getByRole('button', { name: 'Keep editing' }));
  expect(name).toHaveValue('Keep this draft');
  await user.click(within(dialog).getByRole('button', { name: 'Cancel' }));
  expect(await screen.findByRole('alertdialog')).toBeInTheDocument();
});
