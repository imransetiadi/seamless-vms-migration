import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { createRef, StrictMode } from 'react';
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
  it('skips CSS-hidden controls at the end of the dialog when wrapping Tab', async () => {
    const user = userEvent.setup({ delay: null });
    render(<Modal open onClose={() => {}} labelledBy="title">
      <h2 id="title">Edit</h2><input aria-label="Name" /><button>Save</button>
      <div style={{ display: 'none' }}><button>Hidden action</button></div>
    </Modal>);
    screen.getByRole('button', { name: 'Save' }).focus();
    await user.keyboard('{Tab}');
    expect(screen.getByRole('textbox', { name: 'Name' })).toHaveFocus();
  });

  it('skips hidden inputs and controls disabled by their fieldset when focusing and wrapping Tab', async () => {
    const user = userEvent.setup({ delay: null });
    render(
      <Modal open onClose={() => {}} labelledBy="title">
        <h2 id="title">Edit plan</h2>
        <input type="hidden" value="source-id" />
        <fieldset disabled>
          <legend>Unavailable source</legend>
          <input aria-label="Unavailable name" />
          <button type="button">Unavailable action</button>
        </fieldset>
        <input aria-label="Name" />
        <button type="button">Save</button>
        <fieldset disabled>
          <legend>Unavailable destination</legend>
          <select aria-label="Unavailable network"><option>Network</option></select>
          <textarea aria-label="Unavailable notes" />
        </fieldset>
        <input type="hidden" value="destination-id" />
      </Modal>,
    );
    expect(screen.getByRole('textbox', { name: 'Name' })).toHaveFocus();
    await user.keyboard('{Shift>}{Tab}{/Shift}');
    expect(screen.getByRole('button', { name: 'Save' })).toHaveFocus();
    await user.keyboard('{Tab}');
    expect(screen.getByRole('textbox', { name: 'Name' })).toHaveFocus();
  });

  it('keeps Tab inside when an error summary before the first control has focus', async () => {
    const user = userEvent.setup({ delay: null });
    const summaryRef = createRef<HTMLHeadingElement>();
    render(
      <>
        <a href="#background">Background action</a>
        <Modal open onClose={() => {}} labelledBy="summary" initialFocusRef={summaryRef}>
          <h2 ref={summaryRef} id="summary" tabIndex={-1}>Fix the form</h2>
          <input aria-label="Name" />
          <h3 tabIndex={-1}>Details</h3>
          <button type="button">Cancel</button>
          <p tabIndex={-1}>End of form</p>
        </Modal>
      </>,
    );
    expect(summaryRef.current).toHaveFocus();
    await user.keyboard('{Shift>}{Tab}{/Shift}');
    expect(screen.getByRole('button', { name: 'Cancel' })).toHaveFocus();
    summaryRef.current?.focus();
    await user.keyboard('{Tab}');
    expect(screen.getByRole('textbox', { name: 'Name' })).toHaveFocus();
    const details = screen.getByRole('heading', { name: 'Details' });
    details.focus();
    await user.keyboard('{Tab}');
    expect(screen.getByRole('button', { name: 'Cancel' })).toHaveFocus();
    details.focus();
    await user.keyboard('{Shift>}{Tab}{/Shift}');
    expect(screen.getByRole('textbox', { name: 'Name' })).toHaveFocus();
    screen.getByText('End of form').focus();
    await user.keyboard('{Tab}');
    expect(screen.getByRole('textbox', { name: 'Name' })).toHaveFocus();
  });

  it('recovers Tab when focus leaves the top dialog, including a dialog without controls', async () => {
    const user = userEvent.setup({ delay: null });
    const dialog = (controls: boolean) => (
      <Modal open onClose={() => {}} labelledBy="title">
        <h2 id="title">Working</h2>
        {controls && <><button type="button">First</button><button type="button">Last</button></>}
      </Modal>
    );
    const view = render(dialog(true));
    (document.activeElement as HTMLElement).blur();
    await user.keyboard('{Tab}');
    expect(screen.getByRole('button', { name: 'First' })).toHaveFocus();
    (document.activeElement as HTMLElement).blur();
    await user.keyboard('{Shift>}{Tab}{/Shift}');
    expect(screen.getByRole('button', { name: 'Last' })).toHaveFocus();
    view.rerender(dialog(false));
    await user.keyboard('{Tab}');
    expect(screen.getByRole('dialog', { name: 'Working' })).toHaveFocus();
  });

  it('preserves a pre-existing inert root and body style through StrictMode cleanup', () => {
    const appRoot = document.createElement('div');
    appRoot.id = 'root';
    appRoot.setAttribute('inert', '');
    document.body.append(appRoot);
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'clip';
    const view = render(<StrictMode><Dialog /></StrictMode>);
    try {
      expect(appRoot).toHaveAttribute('inert');
      expect(document.body.style.overflow).toBe('hidden');
      view.unmount();
      expect(appRoot).toHaveAttribute('inert');
      expect(document.body.style.overflow).toBe('clip');
    } finally {
      view.unmount();
      appRoot.remove();
      document.body.style.overflow = previousOverflow;
    }
  });

  it('keeps lower dialogs and the page inert until the last dialog closes, in either order', () => {
    const appRoot = document.createElement('div');
    appRoot.id = 'root';
    document.body.append(appRoot);
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'clip';
    const dialogs = (outer: boolean, inner: boolean) => (
      <>
        <Modal open={outer} onClose={() => {}} labelledBy="outer">
          <h2 id="outer">Outer</h2>
          <button type="button">Outer action</button>
        </Modal>
        <Modal open={inner} onClose={() => {}} labelledBy="inner">
          <h2 id="inner">Inner</h2>
          <button type="button">Inner action</button>
        </Modal>
      </>
    );
    const view = render(dialogs(true, false));
    try {
      const outer = screen.getByRole('dialog', { name: 'Outer' });
      const action = within(outer).getByRole('button', { name: 'Outer action' });
      view.rerender(dialogs(true, true));
      expect(outer).toHaveAttribute('inert');
      view.rerender(dialogs(true, false));
      expect(outer).not.toHaveAttribute('inert');
      expect(action).toHaveFocus();
      expect(appRoot).toHaveAttribute('inert');
      expect(document.body.style.overflow).toBe('hidden');
      view.rerender(dialogs(true, true));
      const innerAction = screen.getByRole('button', { name: 'Inner action' });
      view.rerender(dialogs(false, true));
      expect(innerAction).toHaveFocus();
      expect(appRoot).toHaveAttribute('inert');
      expect(document.body.style.overflow).toBe('hidden');
      view.rerender(dialogs(false, false));
      expect(appRoot).not.toHaveAttribute('inert');
      expect(document.body.style.overflow).toBe('clip');
    } finally {
      view.unmount();
      appRoot.remove();
      document.body.style.overflow = previousOverflow;
    }
  });

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

  it("closes from the page with the dialog's props as they are now: never while its request runs (SDD §16)", async () => {
    const user = userEvent.setup({ delay: null });
    const first = vi.fn();
    const second = vi.fn();
    const dialog = (onClose: () => void, dismissible: boolean) => (
      <Modal open onClose={onClose} labelledBy="saving" dismissible={dismissible}>
        <h2 id="saving">Save the plan</h2>
        <button type="button">Save</button>
      </Modal>
    );
    const view = render(dialog(first, true));
    // its request starts after it opened: Escape on the page waits for it
    view.rerender(dialog(first, false));
    (document.activeElement as HTMLElement).blur();
    await user.keyboard('{Escape}');
    expect(first).not.toHaveBeenCalled();
    // the request done, drawn again with another handler: that one closes it
    view.rerender(dialog(second, true));
    await user.keyboard('{Escape}');
    expect(second).toHaveBeenCalledTimes(1);
    expect(first).not.toHaveBeenCalled();
  });

  it('dismisses a shown note or definition on Escape and keeps the dialog; the next Escape closes it (WCAG 1.4.13, SDD §16)', async () => {
    const user = userEvent.setup({ delay: null });
    const onClose = vi.fn();
    render(
      <Modal open onClose={onClose} labelledBy="title">
        <h2 id="title">New plan</h2>
        <p>
          <Term definition="VOL_ENCRYPTED: The Barbican key must be re-created on the destination.">Encrypted volume</Term>
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
    expect(screen.getByRole('tooltip')).toHaveTextContent('VOL_ENCRYPTED: The Barbican key must be re-created on the destination.');
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
