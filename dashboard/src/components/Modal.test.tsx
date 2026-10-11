import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { Modal } from './Modal';

function Dialog() {
  return (
    <Modal open onClose={() => {}} labelledBy="title">
      <h2 id="title">Connect</h2>
      <fieldset>
        <legend>Platform</legend>
        <label>
          <input type="radio" name="platform" value="a" /> A
        </label>
        <label>
          <input type="radio" name="platform" value="b" defaultChecked /> B
        </label>
        <label>
          <input type="radio" name="platform" value="c" /> C
        </label>
      </fieldset>
      <label>
        Name <input />
      </label>
      <details>
        <summary>More</summary>
        <label>
          Hidden <input />
        </label>
      </details>
      <button type="button">Save</button>
      <button type="button" tabIndex={-1}>
        Not in the Tab order
      </button>
    </Modal>
  );
}

describe('Modal (SDD §16)', () => {
  it('traps focus over what Tab reaches: a radio group counts once, at its checked radio (WCAG 2.4.3)', async () => {
    const user = userEvent.setup({ delay: null });
    render(<Dialog />);
    const checked = screen.getByRole('radio', { name: 'B' });
    const save = screen.getByRole('button', { name: 'Save' });
    // the first stop is the checked radio: the unchecked ones are not in the Tab order
    expect(checked).toHaveFocus();
    await user.keyboard('{Shift>}{Tab}{/Shift}');
    // the last stop is Save: a tabindex=-1 button is not in the Tab order
    expect(save).toHaveFocus();
    await user.keyboard('{Tab}');
    expect(checked).toHaveFocus();
  });

  it('wraps to the summary of a closed <details> at its end, not to what the <details> hides', async () => {
    const user = userEvent.setup({ delay: null });
    render(
      <Modal open onClose={() => {}} labelledBy="title">
        <h2 id="title">Settings</h2>
        <button type="button">First</button>
        <details>
          <summary>More</summary>
          <button type="button">Hidden action</button>
        </details>
      </Modal>,
    );
    expect(screen.getByRole('button', { name: 'First' })).toHaveFocus();
    await user.keyboard('{Shift>}{Tab}{/Shift}');
    expect(screen.getByText('More')).toHaveFocus();
  });
});
