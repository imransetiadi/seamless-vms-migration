import { Activity, ShieldQuestion } from 'lucide-react';
import { useRef, useState, type RefObject } from 'react';
import { Link } from 'react-router-dom';
import { usePatchPlan } from '../api/hooks';
import type { AppProbe, Plan, MigrationLike } from '../api/types';
import type { Availability } from '../lib/planActions';
import { describeProbes, probesFor, withVmProbes } from '../lib/probes';
import { Button } from './Button';
import { ConfirmDialog } from './ConfirmDialog';
import { Panel } from './Panel';

export interface ProbeWarningsProps {
  plan: Plan;
  migrations: readonly MigrationLike[];
  /** The plan's Edit availability (SDD §12): using a suggestion edits the plan. */
  edit: Availability;
  onSaved?: (text: string) => void;
  /** Where focus goes once Use suggested probe saved and its row went (the plan's heading, SDD §16). */
  focusAfterRef?: RefObject<HTMLElement | null>;
}

/**
 * "{n} database VMs have no application probe." (SDD §9.3, §16, ADV-18): each one with the rules'
 * suggestion and Use suggested probe, which saves it as the VM's own probe — a plan edit.
 */
export function ProbeWarnings({ plan, migrations, edit, onSaved, focusAfterRef }: ProbeWarningsProps) {
  const patch = usePatchPlan(plan.id);
  const [asking, setAsking] = useState<{ m: MigrationLike; probes: AppProbe[] } | null>(null);
  // read by the dialog as it closes: empty on Cancel, the plan's heading once the probe was saved (SDD §16)
  const afterRef = useRef<HTMLElement | null>(null);
  // a VM the plan now probes is left out: its finding stays until the next validation (SDD §16)
  const missing = migrations.filter(
    (m) => m.phase !== 'cancelled' && m.findings.some((f) => f.code === 'APP_PROBE_MISSING') && probesFor(m.vm.source_id, plan).probes.length === 0,
  );
  if (missing.length === 0) return null;
  const close = () => {
    setAsking(null);
    patch.reset();
  };
  const confirm = () => {
    if (!asking) return;
    const { m, probes } = asking;
    patch.mutate(
      { probe_overrides: withVmProbes(plan, m.vm.source_id, probes) },
      {
        onSuccess: () => {
          // probed now, its row goes with its button
          afterRef.current = focusAfterRef?.current ?? null;
          setAsking(null);
          onSaved?.(`${m.vm.name} is probed on ${describeProbes(probes)}. The plan is a draft again: validate it before starting.`);
        },
      },
    );
  };
  const count = missing.length;
  return (
    <Panel title="Application probes" description="Verification checks only the boot of a VM without one (SDD §7.5)">
      <div className="flex flex-col gap-3">
        <p className="flex items-start gap-2 text-sm text-foreground">
          <ShieldQuestion aria-hidden className="mt-0.5 size-4 shrink-0 text-status-warning" />
          {count === 1 ? '1 database VM has no application probe.' : `${count} database VMs have no application probe.`}
        </p>
        <ul className="flex flex-col divide-y divide-border rounded-md border border-border">
          {missing.map((m) => (
            <li key={m.id} aria-label={m.vm.name} className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2 px-3 py-2">
              <div className="min-w-0 text-sm">
                <Link to={`/migrations/${m.id}`} className="font-mono font-medium text-foreground underline-offset-4 hover:underline">
                  {m.vm.name}
                </Link>
                <p className="text-xs text-muted-foreground">
                  {m.suggested_probes.length ? `Suggested: ${describeProbes(m.suggested_probes)}` : 'No suggestion: add a probe for its service on its migration page.'}
                </p>
              </div>
              {m.suggested_probes.length > 0 && (
                <Button
                  size="sm"
                  icon={Activity}
                  disabledReason={edit.reason}
                  onClick={() => {
                    afterRef.current = null;
                    setAsking({ m, probes: m.suggested_probes });
                  }}
                >
                  Use suggested probe
                </Button>
              )}
            </li>
          ))}
        </ul>
      </div>
      <ConfirmDialog
        open={asking !== null}
        title={asking ? `Probe ${asking.m.vm.name} on ${describeProbes(asking.probes)}?` : ''}
        description="Saves it as this VM's own probe: verification fails if the service refuses it, and the plan rolls the VM back when it says so. The plan returns to draft: validate it again before it starts."
        confirmLabel="Use suggested probe"
        pending={patch.isPending}
        error={asking !== null ? patch.error : null}
        returnFocusRef={afterRef}
        onConfirm={confirm}
        onCancel={close}
      />
    </Panel>
  );
}
