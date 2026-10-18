import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { buildFixtures } from '../api/mockData';
import type { Migration, Plan } from '../api/types';
import { CalibrationPanel, ResolvedMappings } from './CalibrationPanel';

const MiB = 2 ** 20;

function fixtures(): { warm: Migration; plan: Plan } {
  const data = buildFixtures(Date.parse('2026-10-08T12:00:00Z'));
  const warm = data.migrations.find((m) => m.strategy === 'warm' && m.sync_passes.length >= 2);
  const plan = data.plans.find((p) => p.id === warm?.plan_id);
  if (!warm || !plan) throw new Error('mock data has no warm migration with two passes');
  return { warm, plan };
}

/** A row of the panel: its value and its source. */
function row(label: string): [string, string] {
  const dd = screen.getByText(label, { selector: 'dt' }).nextElementSibling as HTMLElement;
  const [value, source] = Array.from(dd.children).map((c) => c.textContent ?? '');
  return [value ?? '', source ?? ''];
}

describe('CalibrationPanel', () => {
  it('shows measured rates once a delta pass calibrated the migration', () => {
    const { warm, plan } = fixtures();
    const m: Migration = { ...warm, observed_scan_bps: 545 * MiB, vm: { ...warm.vm, change_rate_bps: 3 * MiB } };
    render(<CalibrationPanel migration={m} plan={plan} />);
    expect(screen.getByText(/calibrated: the estimate uses rates measured/i)).toBeInTheDocument();
    expect(screen.getByText('545 MiB/s')).toBeInTheDocument();
    expect(screen.getByText(/measured by the last delta pass/)).toBeInTheDocument();
    expect(screen.getByText('3.0 MiB/s')).toBeInTheDocument();
    expect(screen.getByText(/measured between snapshots/)).toBeInTheDocument();
    expect(screen.getByText('Link speed')).toBeInTheDocument();
  });

  it('falls back to plan overrides, then planning defaults, before calibration', () => {
    const { warm, plan } = fixtures();
    const m: Migration = { ...warm, sync_passes: [], observed_scan_bps: null, vm: { ...warm.vm, change_rate_bps: null } };
    const overridden: Plan = { ...plan, estimator_overrides: { scan_bps: 1_000 * MiB, parallel_disks: 2 } };
    render(<CalibrationPanel migration={m} plan={overridden} />);
    expect(screen.getByText(/not calibrated yet/i)).toBeInTheDocument();
    expect(screen.getByText('1000 MiB/s')).toBeInTheDocument();
    expect(screen.getAllByText('plan override')).toHaveLength(2);
    expect(screen.getByText('2.0 MiB/s')).toBeInTheDocument();
    expect(screen.getByText('planning default')).toBeInTheDocument();
    expect(screen.getByText('Disks scanned in parallel')).toBeInTheDocument();
  });

  it('shows no scan term for VMware CBT and calls it calibrated once a delta pass carried bytes', () => {
    const { warm, plan } = fixtures();
    const m: Migration = { ...warm, strategy: 'vmware_warm', observed_scan_bps: null, vm: { ...warm.vm, change_rate_bps: 3 * MiB } };
    render(<CalibrationPanel migration={m} plan={plan} />);
    expect(screen.getByText(/calibrated: the estimate uses rates measured/i)).toBeInTheDocument();
    expect(screen.getByText('not needed')).toBeInTheDocument();
    expect(screen.getByText(/changed-block tracking knows the delta/)).toBeInTheDocument();
    expect(screen.queryByText(/measured by the last delta pass/)).not.toBeInTheDocument();
    // CBT passes scan no disk: no disks in parallel unless the plan sets them
    expect(screen.queryByText('Disks scanned in parallel', { selector: 'dt' })).not.toBeInTheDocument();
  });

  it('does not call a delta pass without byte counts a measurement', () => {
    const { warm, plan } = fixtures();
    const passes = warm.sync_passes.map((p) => ({ ...p, bytes_changed: 0, bytes_scanned: 0, bytes_transferred: 0 }));
    const m: Migration = { ...warm, sync_passes: passes, observed_scan_bps: null, vm: { ...warm.vm, change_rate_bps: 3 * MiB } };
    render(<CalibrationPanel migration={m} plan={plan} />);
    expect(screen.getByText(/not calibrated yet/i)).toBeInTheDocument();
    expect(screen.getByText('from the inventory')).toBeInTheDocument();
  });

  it('shows the disks scanned in parallel on the OpenStack warm path, the planning default when the plan sets none (SDD §16)', () => {
    const { warm, plan } = fixtures();
    render(<CalibrationPanel migration={warm} plan={{ ...plan, estimator_overrides: {} }} />);
    expect(row('Disks scanned in parallel')).toEqual(['4', 'planning default']);
  });

  it('shows the disks in parallel the estimate uses: a fractional override counts its whole part, as the API does', () => {
    const { warm, plan } = fixtures();
    render(<CalibrationPanel migration={warm} plan={{ ...plan, estimator_overrides: { parallel_disks: 2.5 } }} />);
    expect(row('Disks scanned in parallel')).toEqual(['2', 'plan override']);
  });

  it('lists every other estimator override the plan sets, with its value (SDD §16)', () => {
    const { warm, plan } = fixtures();
    const overrides = {
      max_aggregate_scan_bps: 600 * MiB,
      shutdown_s: 45,
      snapshot_s: 20,
      create_s: 90,
      boot_s: 150,
      handover_per_volume_s: 25,
      v2v_s: 420,
      v2v_inplace_s: 90,
    };
    render(<CalibrationPanel migration={warm} plan={{ ...plan, estimator_overrides: overrides }} />);
    expect(row('Aggregate scan cap')).toEqual(['600 MiB/s', 'plan override']);
    expect(row('Source shutdown')).toEqual(['45 s', 'plan override']);
    expect(row('Snapshot')).toEqual(['20 s', 'plan override']);
    expect(row('Volume create')).toEqual(['1m 30s', 'plan override']);
    expect(row('Boot')).toEqual(['2m 30s', 'plan override']);
    expect(row('Handover per volume')).toEqual(['25 s', 'plan override']);
    expect(row('Conversion')).toEqual(['7m 00s', 'plan override']);
    expect(row('In-place conversion')).toEqual(['1m 30s', 'plan override']);
  });

  it("keeps a single-shot strategy's panel short: no disks in parallel or step time the plan does not set", () => {
    const { warm, plan } = fixtures();
    const m: Migration = { ...warm, strategy: 'cold', sync_passes: [], observed_scan_bps: null };
    render(<CalibrationPanel migration={m} plan={{ ...plan, estimator_overrides: {} }} />);
    for (const label of ['Disks scanned in parallel', 'Aggregate scan cap', 'Source shutdown', 'Snapshot', 'Boot']) {
      expect(screen.queryByText(label, { selector: 'dt' })).not.toBeInTheDocument();
    }
  });

  it('explains that single-shot strategies are never calibrated', () => {
    const { warm, plan } = fixtures();
    const m: Migration = { ...warm, strategy: 'cold', sync_passes: [], observed_scan_bps: null };
    render(<CalibrationPanel migration={m} plan={plan} />);
    expect(screen.getByText(/single-shot strategy/i)).toBeInTheDocument();
  });
});

describe('ResolvedMappings', () => {
  it('renders nothing when pre-flight resolved no mapping', () => {
    const { warm } = fixtures();
    const { container } = render(<ResolvedMappings migration={warm} />);
    expect(container).toBeEmptyDOMElement();
  });

  it('lists the automatically matched flavor', () => {
    const { warm } = fixtures();
    const m: Migration = {
      ...warm,
      resolved_mappings: { networks: {}, flavors: { 'custom.4x8': 'm2.medium' }, volume_types: {}, projects: {} },
    };
    render(<ResolvedMappings migration={m} />);
    expect(screen.getByRole('table', { name: /resolved automatically/i })).toBeInTheDocument();
    expect(screen.getByText('custom.4x8')).toBeInTheDocument();
    expect(screen.getByText('m2.medium')).toBeInTheDocument();
  });
});
