import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import Plans from '../pages/Plans';
import { renderWithApp } from './utils';

// The browser journeys (tests/e2e/*.mjs, QASuite §14.7) pick the New plan dialog's fields by label, as Playwright's
// getByLabel does it: a case-insensitive substring. A renamed label breaks them although every unit test passes
// (review RV20-02), so each label they pick must stay one the dialog offers.
const journeys = import.meta.glob('../../../tests/e2e/*.mjs', { query: '?raw', import: 'default', eager: true }) as Record<string, string>;

const picked = [...new Set(Object.values(journeys).flatMap((text) => [...text.matchAll(/\bpick\('([^']+)'/g)].map((match) => match[1]!)))];

const asPlaywright = (label: string) => new RegExp(label.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'i');

describe('the browser journeys pick fields the New plan dialog offers (QASuite §14.7, review RV20-02)', () => {
  it('finds the journeys and the labels they pick', () => {
    expect(Object.keys(journeys).length).toBeGreaterThan(3);
    expect(picked).toEqual(expect.arrayContaining(['Source provider', 'Default strategy']));
  });

  it('offers every label a journey picks', async () => {
    const user = userEvent.setup({ delay: null });
    renderWithApp(<Plans />, { route: '/plans', path: '/plans', token: 'operator' });
    await user.click(await screen.findByRole('button', { name: /new plan/i }));
    const dialog = await screen.findByRole('dialog', { name: /new migration plan/i });
    const missing = picked.filter((label) => within(dialog).queryAllByLabelText(asPlaywright(label)).length === 0);
    expect(missing).toEqual([]);
  });
});
