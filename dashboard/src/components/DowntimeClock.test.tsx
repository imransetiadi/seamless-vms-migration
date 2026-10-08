import { act, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { DowntimeClock } from './DowntimeClock';

describe('DowntimeClock', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-10-08T12:00:00Z'));
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it('counts up from downtime_started_at every second', () => {
    render(<DowntimeClock startedAt="2026-10-08T11:58:25Z" endedAt={null} actualS={null} sloS={300} />);
    const timer = screen.getByRole('timer', { name: /downtime/i });
    expect(timer).toHaveTextContent('1:35');

    act(() => {
      vi.advanceTimersByTime(5_000);
    });
    expect(timer).toHaveTextContent('1:40');
    expect(screen.getByText(/source VM stopped/i)).toBeInTheDocument();
    expect(screen.getByText(/3:20 left in the SLO/i)).toBeInTheDocument();
  });

  it('says in words when the SLO is exceeded', () => {
    render(<DowntimeClock startedAt="2026-10-08T11:53:00Z" endedAt={null} actualS={null} sloS={300} />);
    expect(screen.getByRole('timer')).toHaveTextContent('7:00');
    expect(screen.getByText(/over the SLO by 2:00/i)).toBeInTheDocument();
  });

  it('freezes at the actual downtime once it ended', () => {
    render(
      <DowntimeClock startedAt="2026-10-08T09:00:00Z" endedAt="2026-10-08T09:03:32Z" actualS={212} sloS={300} estimateS={352} />,
    );
    expect(screen.getByRole('timer')).toHaveTextContent('3:32');
    act(() => {
      vi.advanceTimersByTime(10_000);
    });
    expect(screen.getByRole('timer')).toHaveTextContent('3:32');
    expect(screen.getByText(/within the SLO/i)).toBeInTheDocument();
    expect(screen.getByText(/estimated 5m 52s/i)).toBeInTheDocument();
  });

  it('explains that no downtime has started yet', () => {
    render(<DowntimeClock startedAt={null} endedAt={null} actualS={null} sloS={900} />);
    expect(screen.getByRole('timer')).toHaveTextContent('0:00');
    expect(screen.getByText(/not started — the source VM is running/i)).toBeInTheDocument();
  });
});
