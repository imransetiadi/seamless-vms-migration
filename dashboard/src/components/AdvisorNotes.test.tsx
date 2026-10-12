import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import type { AdvisorNote } from '../api/types';
import { AdvisorNotes } from './AdvisorNotes';

describe('AdvisorNotes', () => {
  it('never says which strategy a strategy note "selected": its decision is in the recommendations (SDD §14.4, §16)', () => {
    const note: AdvisorNote = {
      kind: 'strategy',
      source: 'jev',
      summary: 'Jev suggested warm (confidence 0.57, status ok); below the bar, kept cold',
      confidence: 0.57,
      data: { selected: 'warm', applied: false, deterministic: 'cold', probabilities: { warm: 0.57, cold: 0.43 } },
      created_at: '2026-10-09T08:00:00Z',
    };
    render(
      <MemoryRouter>
        <AdvisorNotes notes={[note]} />
      </MemoryRouter>,
    );
    expect(screen.getByText(note.summary)).toBeInTheDocument();
    expect(document.body).not.toHaveTextContent(/selected/i);
  });
});
