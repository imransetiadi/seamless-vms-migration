import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { Tabs } from './Tabs';

describe('Tabs', () => {
  it('switches panels by click and by the arrow, Home and End keys (WAI-ARIA tabs)', async () => {
    const user = userEvent.setup({ delay: null });
    render(
      <Tabs
        label="Records"
        tabs={[
          { id: 'audit', label: 'Audit', content: <p>audit body</p> },
          { id: 'input', label: 'Input', content: <p>input body</p> },
          { id: 'more', label: 'More', content: <p>more body</p> },
        ]}
      />,
    );
    const [audit, input, more] = screen.getAllByRole('tab');
    expect(screen.getByRole('tablist', { name: 'Records' })).toBeInTheDocument();
    expect(audit).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByRole('tabpanel', { name: 'Audit' })).toHaveTextContent('audit body');
    expect(input).toHaveAttribute('tabindex', '-1');

    await user.click(input!);
    expect(screen.getByRole('tabpanel', { name: 'Input' })).toHaveTextContent('input body');
    await user.keyboard('{ArrowRight}');
    expect(more).toHaveFocus();
    expect(more).toHaveAttribute('aria-selected', 'true');
    await user.keyboard('{ArrowRight}');
    expect(audit).toHaveFocus();
    await user.keyboard('{End}');
    expect(more).toHaveFocus();
    await user.keyboard('{Home}');
    expect(audit).toHaveFocus();
    await user.keyboard('{ArrowLeft}');
    expect(more).toHaveFocus();
    expect(screen.getByRole('tabpanel', { name: 'More' })).toHaveTextContent('more body');
  });
});
