import { screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import indexHtml from '../../index.html?raw';
import { DISTRIBUTION_PRESETS } from '../lib/distributions';
import { DESTINATIONS } from '../lib/platforms';
import Overview from '../pages/Overview';
import { renderWithApp } from './utils';

// The product migrates into RHOSO 18.0 and community OpenStack (SDD §1 naming, §21): what it says about itself names
// both, never RHOSO 18.0 alone, and the RHOSO 18.0 preset is one destination, not "the destination" (review RV20-04).
describe('the product names every destination it migrates into (SDD §1 naming, §21, review RV20-04)', () => {
  it('on the Overview page', async () => {
    renderWithApp(<Overview />, { route: '/', token: 'viewer' });
    expect(await screen.findByText(`Live VM migrations to ${DESTINATIONS}.`)).toBeInTheDocument();
  });

  it("in the page's description", () => {
    expect(/<meta name="description" content="([^"]*)"/.exec(indexHtml)?.[1]).toBe(`Seamless Migrate — VM migrations into ${DESTINATIONS}`);
  });

  it('in the RHOSO 18.0 preset, one destination of several', () => {
    const rhoso = DISTRIBUTION_PRESETS.find((preset) => preset.id === 'rhoso');
    expect(rhoso?.summary).toBeDefined();
    expect(rhoso?.summary).not.toMatch(/the destination/i);
  });
});
