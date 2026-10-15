import { act, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { TONE_CLASSES } from '../lib/status';
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

  it('announces crossing into the warning and past the SLO once each, never every tick (SDD §16)', () => {
    // 230 s of a 300 s SLO: calm, 10 s before the warning
    render(<DowntimeClock startedAt="2026-10-08T11:56:10Z" endedAt={null} actualS={null} sloS={300} />);
    const regions = screen.getAllByRole('status');
    const said = () => regions.map((r) => r.textContent ?? '').filter(Boolean);
    expect(said()).toEqual([]);
    act(() => {
      vi.advanceTimersByTime(12_000);
    });
    expect(said()).toEqual(['Downtime reached 80 % of the SLO: 0:58 left.']);
    act(() => {
      vi.advanceTimersByTime(30_000);
    });
    // the clock moved on; the announcement did not
    expect(said()).toEqual(['Downtime reached 80 % of the SLO: 0:58 left.']);
    act(() => {
      vi.advanceTimersByTime(30_000);
    });
    expect(said()).toEqual(['Downtime is over the SLO.']);
  });

  it('says nothing for a crossing that happened before the page opened or before it knew (SDD §16)', () => {
    const view = render(<DowntimeClock startedAt="2026-10-08T11:53:00Z" endedAt={null} actualS={null} sloS={300} />);
    act(() => {
      vi.advanceTimersByTime(5_000);
    });
    expect(screen.getAllByRole('status').map((r) => r.textContent ?? '').filter(Boolean)).toEqual([]);
    view.unmount();
    // the page opened before the downtime was known; the data then says it began 7 minutes ago
    const late = render(<DowntimeClock startedAt={null} endedAt={null} actualS={null} sloS={300} />);
    late.rerender(<DowntimeClock startedAt="2026-10-08T11:53:00Z" endedAt={null} actualS={null} sloS={300} />);
    expect(screen.getAllByRole('status').map((r) => r.textContent ?? '').filter(Boolean)).toEqual([]);
  });

  it('warns from 80 % of the SLO with an hourglass, not a check mark (SDD §16)', () => {
    // 240 s of a 300 s SLO: exactly 80 %, where the warning starts
    render(<DowntimeClock startedAt="2026-10-08T11:56:00Z" endedAt={null} actualS={null} sloS={300} />);
    const line = screen.getByText(/left in the SLO/i);
    expect(line).toHaveClass(TONE_CLASSES.warning.text);
    expect(line.querySelector('.lucide-hourglass')).not.toBeNull();
    expect(line.querySelector('.lucide-circle-check')).toBeNull();
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

  it('shows an ended downtime rounded to the second, as the text under it and every other view give it (SDD §16)', () => {
    render(<DowntimeClock startedAt="2026-10-08T09:00:00Z" endedAt="2026-10-08T09:11:05Z" actualS={665.798} sloS={900} estimateS={733.33} />);
    expect(screen.getByRole('timer')).toHaveTextContent('11:06');
    expect(screen.getByText(/^Actual 11m 06s:/)).toBeInTheDocument();
    expect(screen.getByRole('progressbar', { name: 'Downtime against the SLO' })).toHaveAttribute('aria-valuetext', '11:06 of 15:00');
  });

  it('counts whole seconds while the downtime runs, never rounding up', () => {
    render(<DowntimeClock startedAt="2026-10-08T11:58:24.300Z" endedAt={null} actualS={null} sloS={300} />);
    // 95.7 s since the stop: the clock reads 1:35 until the 96th second
    expect(screen.getByRole('timer')).toHaveTextContent('1:35');
  });

  it('runs in model time in demo mode, and says so (SDD §16, ADV-16)', () => {
    render(<DowntimeClock startedAt="2026-10-08T11:59:55Z" endedAt={null} actualS={null} sloS={600} demoSpeed={60} />);
    const timer = screen.getByRole('timer');
    expect(timer).toHaveTextContent('5:00');
    act(() => {
      vi.advanceTimersByTime(1_000);
    });
    expect(timer).toHaveTextContent('6:00');
    expect(screen.getByText('Demo time: 60× faster than real time')).toBeInTheDocument();
    expect(screen.getByText(/4:00 left in the SLO/i)).toBeInTheDocument();
  });

  it('keeps wall time outside demo mode, and at a demo speed of 1', () => {
    for (const demoSpeed of [null, 1]) {
      const { unmount } = render(<DowntimeClock startedAt="2026-10-08T11:59:55Z" endedAt={null} actualS={null} sloS={600} demoSpeed={demoSpeed} />);
      expect(screen.getByRole('timer'), String(demoSpeed)).toHaveTextContent('0:05');
      expect(screen.queryByText(/demo time/i)).not.toBeInTheDocument();
      unmount();
    }
  });

  it("lists the estimate's parts and compares the actual downtime with the estimate (SDD §16, ADV-16)", () => {
    const parts = { shutdown_s: 60, copy_s: 81, conversion_s: 0, create_s: 60, boot_s: 120 };
    const { rerender } = render(
      <DowntimeClock startedAt="2026-10-08T09:00:00Z" endedAt="2026-10-08T09:03:32Z" actualS={212} sloS={600} estimateS={321} parts={parts} strategy="warm" />,
    );
    const part = (name: string) => screen.getByRole('definition', { name });
    expect(part('Shutdown')).toHaveTextContent('1m 00s');
    expect(part('Final sync')).toHaveTextContent('1m 21s');
    expect(part('Create')).toHaveTextContent('1m 00s');
    expect(part('Boot')).toHaveTextContent('2m 00s');
    // a part the strategy does not have is left out
    expect(screen.queryByRole('definition', { name: 'Conversion' })).not.toBeInTheDocument();
    expect(screen.getByText('Actual 3m 32s: 1m 49s under the estimate.')).toBeInTheDocument();

    rerender(<DowntimeClock startedAt="2026-10-08T09:00:00Z" endedAt="2026-10-08T09:06:40Z" actualS={400} sloS={600} estimateS={321} parts={parts} strategy="warm" />);
    expect(screen.getByText('Actual 6m 40s: 1m 19s over the estimate.')).toBeInTheDocument();
    rerender(<DowntimeClock startedAt="2026-10-08T09:00:00Z" endedAt="2026-10-08T09:05:21Z" actualS={321.4} sloS={600} estimateS={321} parts={parts} strategy="warm" />);
    expect(screen.getByText('Actual 5m 21s: as estimated.')).toBeInTheDocument();
  });

  it('names the copy part after the strategy: the copy, the final sync or the volume handover', () => {
    const parts = { shutdown_s: 60, copy_s: 300, conversion_s: 120, create_s: 60, boot_s: 120 };
    const { rerender } = render(<DowntimeClock startedAt={null} endedAt={null} actualS={null} estimateS={660} parts={parts} strategy="vmware_cold" />);
    expect(screen.getByRole('definition', { name: 'Copy' })).toHaveTextContent('5m 00s');
    expect(screen.getByRole('definition', { name: 'Conversion' })).toHaveTextContent('2m 00s');
    rerender(<DowntimeClock startedAt={null} endedAt={null} actualS={null} estimateS={660} parts={parts} strategy="storage_handover" />);
    expect(screen.getByRole('definition', { name: 'Volume handover' })).toHaveTextContent('5m 00s');
    // an estimate written before its parts were recorded shows none
    rerender(<DowntimeClock startedAt={null} endedAt={null} actualS={null} estimateS={660} parts={{}} strategy="warm" />);
    expect(screen.queryByRole('definition')).not.toBeInTheDocument();
  });

  it('explains that no downtime has started yet', () => {
    render(<DowntimeClock startedAt={null} endedAt={null} actualS={null} sloS={900} />);
    expect(screen.getByRole('timer')).toHaveTextContent('0:00');
    expect(screen.getByText(/not started — the source VM is running/i)).toBeInTheDocument();
  });
});
