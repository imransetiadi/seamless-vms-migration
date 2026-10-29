import { fireEvent, render, screen } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { ScrollRegion } from './ScrollRegion';

it('scrolls its own arrow keys without swallowing keys from nested controls or modifier shortcuts', () => {
  render(<ScrollRegion label="NICs"><input aria-label="Filter" /></ScrollRegion>);
  const region = screen.getByRole('region', { name: 'NICs' });
  const scroll = vi.fn();
  region.scrollBy = scroll;
  fireEvent.keyDown(region, { key: 'ArrowRight' });
  expect(scroll).toHaveBeenLastCalledWith({ left: 40, top: 0 });
  fireEvent.keyDown(region, { key: 'ArrowUp' });
  expect(scroll).toHaveBeenLastCalledWith({ left: 0, top: -40 });
  fireEvent.keyDown(screen.getByRole('textbox'), { key: 'ArrowRight' });
  fireEvent.keyDown(region, { key: 'ArrowRight', ctrlKey: true });
  expect(scroll).toHaveBeenCalledTimes(2);
});
