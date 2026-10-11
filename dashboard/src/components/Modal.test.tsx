import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { Button } from './Button';
import { Modal } from './Modal';
import { Term } from './Term';

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

  it('closes the topmost dialog on Escape wherever the focus is, unless its request runs (SDD §16)', async () => {
    const user = userEvent.setup({ delay: null });
    const outer = vi.fn();
    const inner = vi.fn();
    const view = render(
      <>
        <Modal open onClose={outer} labelledBy="outer">
          <h2 id="outer">Outer</h2>
          <button type="button">Outer action</button>
        </Modal>
        <Modal open onClose={inner} labelledBy="inner">
          <h2 id="inner">Inner</h2>
          <button type="button">Inner action</button>
        </Modal>
      </>,
    );
    // focus inside: closed once, not by the panel and the page both
    expect(screen.getByRole('button', { name: 'Inner action' })).toHaveFocus();
    await user.keyboard('{Escape}');
    expect(inner).toHaveBeenCalledTimes(1);
    // focus dropped to the page: the topmost dialog still closes, the one below does not
    (document.activeElement as HTMLElement).blur();
    expect(document.body).toHaveFocus();
    await user.keyboard('{Escape}');
    expect(inner).toHaveBeenCalledTimes(2);
    expect(outer).not.toHaveBeenCalled();
    // once the inner dialog is gone, the outer one is the topmost again
    view.rerender(
      <>
        <Modal open onClose={outer} labelledBy="outer">
          <h2 id="outer">Outer</h2>
          <button type="button">Outer action</button>
        </Modal>
        <Modal open={false} onClose={inner} labelledBy="inner">
          <h2 id="inner">Inner</h2>
        </Modal>
      </>,
    );
    (document.activeElement as HTMLElement).blur();
    await user.keyboard('{Escape}');
    expect(outer).toHaveBeenCalledTimes(1);
    expect(inner).toHaveBeenCalledTimes(2);
    view.unmount();

    const busy = vi.fn();
    render(
      <Modal open onClose={busy} labelledBy="busy" dismissible={false}>
        <h2 id="busy">Saving</h2>
        <button type="button">Wait</button>
      </Modal>,
    );
    (document.activeElement as HTMLElement).blur();
    await user.keyboard('{Escape}');
    expect(busy).not.toHaveBeenCalled();
  });

  it('dismisses a shown note or definition on Escape and keeps the dialog; the next Escape closes it (WCAG 1.4.13, SDD §16)', async () => {
    const user = userEvent.setup({ delay: null });
    const onClose = vi.fn();
    render(
      <Modal open onClose={onClose} labelledBy="title">
        <h2 id="title">New plan</h2>
        <p>
          <Term definition="VOL_ENCRYPTED: The Barbican key must be re-created at RHOSO.">Encrypted volume</Term>
        </p>
        <Button disabledReason="Select VMs in the table first.">Create plan</Button>
      </Modal>,
    );
    // a soft-disabled button's note
    const create = screen.getByRole('button', { name: 'Create plan' });
    await user.click(create);
    expect(screen.getByRole('note')).toHaveTextContent('Select VMs in the table first.');
    await user.keyboard('{Escape}');
    expect(screen.queryByRole('note')).not.toBeInTheDocument();
    expect(onClose).not.toHaveBeenCalled();
    // the note gone, the next Escape closes the dialog
    expect(create).toHaveFocus();
    await user.keyboard('{Escape}');
    expect(onClose).toHaveBeenCalledTimes(1);
    // a term's definition, shown by a tap
    await user.click(screen.getByRole('button', { name: 'Encrypted volume' }));
    expect(screen.getByRole('tooltip')).toHaveTextContent('VOL_ENCRYPTED: The Barbican key must be re-created at RHOSO.');
    await user.keyboard('{Escape}');
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument();
    expect(onClose).toHaveBeenCalledTimes(1);
    await user.keyboard('{Escape}');
    expect(onClose).toHaveBeenCalledTimes(2);
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
