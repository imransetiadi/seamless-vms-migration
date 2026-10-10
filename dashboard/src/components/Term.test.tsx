import { act, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { Term } from './Term';

const DEFINITION = "Jev's calibrated confidence in its own recommendation, not the probability it gives that option.";

describe('Term', () => {
  it('explains a term on focus and on hover, and Escape dismisses it (WCAG 1.4.13)', async () => {
    const user = userEvent.setup({ delay: null });
    render(
      <p>
        57% <Term definition={DEFINITION}>confidence</Term> (threshold 60%)
      </p>,
    );
    const term = screen.getByText('confidence');
    // the sentence stays whole: the definition is not part of it
    expect(term.closest('p')).toHaveTextContent(/^57% confidence \(threshold 60%\)$/);
    expect(term).toHaveAccessibleDescription(DEFINITION);
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument();

    await user.tab();
    expect(term).toHaveFocus();
    expect(screen.getByRole('tooltip')).toHaveTextContent(DEFINITION);
    await user.keyboard('{Escape}');
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument();

    await user.tab({ shift: true });
    await user.hover(term);
    expect(screen.getByRole('tooltip')).toHaveTextContent(DEFINITION);
    // the pointer can move onto the definition without it going away, however long it stays
    await user.hover(screen.getByRole('tooltip'));
    await act(() => new Promise((resolve) => setTimeout(resolve, 250)));
    expect(screen.getByRole('tooltip')).toBeInTheDocument();
    await user.unhover(screen.getByRole('tooltip'));
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument();
  });
});
