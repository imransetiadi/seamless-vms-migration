import {
  ArrowRight,
  BadgeCheck,
  CircleSlash,
  FastForward,
  RefreshCw,
  RotateCcw,
  ThumbsUp,
  TriangleAlert,
  Undo2,
  Zap,
  type LucideIcon,
} from 'lucide-react';
import { useId, useLayoutEffect, useRef, useState, type FocusEvent } from 'react';
import { useMe, useMigrationAction, type MigrationActionRequest } from '../api/hooks';
import { ACTION_TEXT_MAX, FORCE_REASON_MIN, type Migration, type Plan, type Role } from '../api/types';
import { cn } from '../lib/cn';
import { forcedDowntime } from '../lib/forcedEstimate';
import { formatBytes, formatDateTime, formatDuration } from '../lib/format';
import { hasDataPath } from '../lib/fsm';
import { isWarmStrategy } from '../lib/phase';
import { MIGRATION_ACTION_ORDER, migrationActions, nextStep, waitsOnClosedWindow, type MigrationActionKey } from '../lib/migrationActions';
import { Button } from './Button';
import { ConfirmDialog } from './ConfirmDialog';
import { TextAreaField } from './Field';

const LABEL: Record<MigrationActionKey, string> = {
  approve: 'Approve',
  cutover: 'Cut over',
  'force-cutover': 'Force cutover',
  sync: 'Sync now',
  rollback: 'Roll back',
  retry: 'Retry',
  cancel: 'Cancel',
  finalize: 'Finalize',
};

const ICON: Record<MigrationActionKey, LucideIcon> = {
  approve: ThumbsUp,
  cutover: Zap,
  'force-cutover': FastForward,
  sync: RefreshCw,
  rollback: Undo2,
  retry: RotateCcw,
  cancel: CircleSlash,
  finalize: BadgeCheck,
};

const DONE: Record<MigrationActionKey, string> = {
  approve: 'approved',
  cutover: 'cutover requested',
  'force-cutover': 'cutover forced',
  sync: 'delta sync requested',
  rollback: 'rollback started',
  retry: 'retry requested',
  cancel: 'cancelled',
  finalize: 'finalized',
};

function windowOpen(plan: Plan | null | undefined): boolean {
  const w = plan?.cutover_window;
  if (!w) return true;
  const now = Date.now();
  return now >= Date.parse(w.start) && now <= Date.parse(w.end);
}

export interface MigrationActionsProps {
  migration: Migration;
  plan?: Plan | null;
  role: Role | undefined;
  /** The plan failed to load: its cutover window and approval policy are unknown (SDD §16). */
  planUnavailable?: boolean;
  /** The plan is still loading: its cutover window and approval policy are not known yet (SDD §16). */
  planLoading?: boolean;
}

/**
 * Approve / Cut over / Force cutover / Sync / Retry / Roll back / Cancel / Finalize, enabled only for a
 * valid phase and sufficient role (unavailable ones stay focusable and say why). Every action confirms
 * first; destructive ones use the danger tone and Finalize requires typing the VM name. Force cutover
 * belongs to warm strategies alone: a single-shot one has no passes to skip (SDD §5.4).
 */
export function MigrationActions({ migration: m, plan, role, planUnavailable = false, planLoading = false }: MigrationActionsProps) {
  // the cutover's requester does not approve it (maker-checker, SDD §5.4)
  const me = useMe().data?.name;
  const availability = migrationActions(m, role, plan, undefined, me);
  const fourEyes = Boolean(plan?.require_approval && plan.require_distinct_approver);
  if (planUnavailable && availability.cutover.enabled) {
    availability.cutover = { enabled: false, reason: 'The plan could not be loaded: its cutover window and approval policy are unknown.' };
  } else if (planLoading && availability.cutover.enabled) {
    availability.cutover = { enabled: false, reason: 'The plan is loading: its cutover window and approval policy are not known yet.' };
  }
  // a requested cutover waiting for a closed window: the Cutover action lets it start outside the window (SDD §5.4)
  const forcing = waitsOnClosedWindow(m, plan, Date.now());
  // a warm cutover requested before pre-copy converged starts once it does (SDD §5.4)
  const early = isWarmStrategy(m.strategy) && m.phase !== 'awaiting_cutover';
  const step = nextStep(m, plan);
  const action = useMigrationAction(m.id);
  const [open, setOpen] = useState<MigrationActionKey | null>(null);
  const [comment, setComment] = useState('');
  const [reason, setReason] = useState('');
  const [forceWindow, setForceWindow] = useState(false);
  const [deleteSource, setDeleteSource] = useState(false);
  const [announcement, setAnnouncement] = useState('');
  const forceId = useId();
  const deleteId = useId();
  // a button that changes rows as the migration moves keeps the focus (SDD §16): React moves the node, which
  // drops the focus in the browser, so the focused action is remembered and focused again after the move
  const buttons = useRef(new Map<MigrationActionKey, HTMLButtonElement>());
  const focused = useRef<MigrationActionKey | null>(null);
  const rows = useRef('');
  const remember = (key: MigrationActionKey) => (node: HTMLButtonElement | null) => {
    if (node) buttons.current.set(key, node);
    else buttons.current.delete(key);
  };
  // focus that went to another element is not ours to take back; focus dropped to the page by a move is
  const forget = (event: FocusEvent<HTMLButtonElement>) => {
    if (event.relatedTarget) focused.current = null;
  };

  const show = (key: MigrationActionKey) => {
    action.reset();
    setComment('');
    setReason('');
    setForceWindow(false);
    setDeleteSource(false);
    setOpen(key);
  };
  const close = () => {
    setOpen(null);
    action.reset();
  };
  const submit = (request: MigrationActionRequest) =>
    action.mutate(request, {
      onSuccess: () => {
        setAnnouncement(`${m.vm.name}: ${DONE[request.action]}.`);
        setOpen(null);
      },
    });

  const commentField = (
    <TextAreaField label="Comment (optional)" rows={2} maxLength={ACTION_TEXT_MAX} value={comment} onChange={(e) => setComment(e.target.value)} hint="Recorded in the audit trail." />
  );
  const estimate = m.estimate ? formatDuration(m.estimate.downtime_s) : 'unknown';
  // what a Force cutover records (SDD §5.4, §16): the downtime after the running pass, not the converged estimate
  const forced = plan && isWarmStrategy(m.strategy) ? forcedDowntime(m, plan) : null;
  // the API lists only passes that ended (SDD §4.2): the last one is what a Force cutover records
  const lastPass = m.sync_passes.filter((p) => p.ended_at !== null).sort((a, b) => a.number - b.number).at(-1);
  const slo = plan ? formatDuration(plan.downtime_slo_s) : null;
  const closedWindow = !windowOpen(plan);
  const common = { pending: action.isPending, error: action.error, onCancel: close };

  // the actions available now first, the next step's first; the others in a quieter row (SDD §16)
  const offered = MIGRATION_ACTION_ORDER.filter((key) => key !== 'force-cutover' || isWarmStrategy(m.strategy));
  const available = offered
    .filter((key) => availability[key].enabled)
    .sort((a, b) => Number(b === step.action) - Number(a === step.action));
  const unavailable = offered.filter((key) => !availability[key].enabled);
  const layout = available.join(',');
  useLayoutEffect(() => {
    const moved = rows.current !== '' && rows.current !== layout;
    rows.current = layout;
    const key = focused.current;
    const node = key ? buttons.current.get(key) : undefined;
    if (moved && node && document.activeElement !== node && (document.activeElement === document.body || document.activeElement === null)) {
      node.focus({ preventScroll: true });
    }
  }, [layout]);

  return (
    <div className="flex flex-col gap-3">
      <p
        className={cn(
          'flex items-start gap-2 rounded-md border px-3 py-2 text-sm text-foreground',
          step.action ? 'border-accent/50 bg-accent/10' : 'border-border bg-muted/40',
        )}
      >
        <ArrowRight aria-hidden className={cn('mt-0.5 size-4 shrink-0', step.action ? 'text-status-success' : 'text-muted-foreground')} />
        {step.text}
      </p>
      <div role="group" aria-label="Migration actions" className="flex flex-wrap items-center gap-2">
        {available.map((key) => {
          const primary = key === step.action;
          const destructive = key === 'rollback' || key === 'cancel' || key === 'finalize';
          return (
            <Button
              key={key}
              ref={remember(key)}
              size="lg"
              icon={ICON[key]}
              variant={primary ? (destructive && key !== 'finalize' ? 'danger' : 'primary') : 'secondary'}
              onClick={() => show(key)}
              onFocus={() => (focused.current = key)}
              onBlur={forget}
              className={cn(key === 'cancel' && available.length > 1 && 'sm:ml-auto')}
            >
              {key === 'cutover' && forcing ? 'Cut over outside the window' : LABEL[key]}
            </Button>
          );
        })}
        {unavailable.length > 0 && (
          <p key="unavailable" className="mt-1 basis-full text-xs text-muted-foreground">
            Unavailable now
          </p>
        )}
        {unavailable.map((key) => (
          // quieter and smaller, still focusable and saying why (SDD §16)
          <Button
            key={key}
            ref={remember(key)}
            size="sm"
            icon={ICON[key]}
            variant="secondary"
            disabledReason={availability[key].reason}
            onClick={() => show(key)}
            onFocus={() => (focused.current = key)}
            onBlur={forget}
          >
            {key === 'cutover' && forcing ? 'Cut over outside the window' : LABEL[key]}
          </Button>
        ))}
      </div>
      <p role="status" className="sr-only">
        {announcement}
      </p>

      <ConfirmDialog
        {...common}
        open={open === 'approve'}
        title={`Approve the cutover of ${m.vm.name}?`}
        description={`Records your approval. The cutover still waits for a cutover request${plan?.auto_cutover ? ' (automatic in this plan)' : ''} and the window.`}
        confirmLabel="Approve"
        onConfirm={() => submit({ action: 'approve', body: comment.trim() ? { comment: comment.trim() } : {} })}
      >
        {commentField}
      </ConfirmDialog>

      <ConfirmDialog
        {...common}
        open={open === 'cutover'}
        title={
          forcing ? `Let ${m.vm.name} cut over outside the window?` : early ? `Cut over ${m.vm.name} once pre-copy converges?` : `Cut over ${m.vm.name} now?`
        }
        description={
          <>
            {forcing && plan?.cutover_window
              ? `The cutover was requested and waits for the cutover window (${formatDateTime(plan.cutover_window.start)} → ${formatDateTime(plan.cutover_window.end)}). Confirming lets it start as soon as the plan, its wave and a free cutover slot allow. `
              : ''}
            {early && !forcing
              ? 'The cutover is requested now and starts once pre-copy has converged and the plan, its wave and a free cutover slot allow. '
              : ''}
            {early && !forcing ? 'Then the' : 'The'} source VM will be stopped and the downtime clock starts. Estimated downtime {estimate}
            {slo ? ` against an SLO of ${slo}` : ''}.{' '}
            {fourEyes
              ? 'Your request is not an approval: another approver approves the cutover. The migration can be rolled back until it is finalized.'
              : 'This also records your approval; the migration can be rolled back until it is finalized.'}
          </>
        }
        confirmLabel={forcing ? 'Cut over outside the window' : early ? 'Request cutover' : 'Start cutover'}
        onConfirm={() =>
          submit({
            action: 'cutover',
            body: { force_window: forcing || (closedWindow && forceWindow), ...(comment.trim() ? { comment: comment.trim() } : {}) },
          })
        }
      >
        {closedWindow && !forcing && plan?.cutover_window && (
          <label htmlFor={forceId} className="flex min-h-11 cursor-pointer items-start gap-2.5 rounded-md border border-status-warning/40 bg-status-warning/10 p-2 text-sm">
            <input id={forceId} type="checkbox" className="mt-1 size-4 accent-accent" checked={forceWindow} onChange={(e) => setForceWindow(e.target.checked)} />
            <span>
              Ignore the cutover window
              <span className="block text-xs text-muted-foreground">
                The window is {formatDateTime(plan.cutover_window.start)} → {formatDateTime(plan.cutover_window.end)}. Without this the cutover waits for it.
              </span>
            </span>
          </label>
        )}
        {commentField}
      </ConfirmDialog>

      <ConfirmDialog
        {...common}
        open={open === 'force-cutover'}
        tone="danger"
        title={`Force the cutover of ${m.vm.name}?`}
        description={
          <>
            {lastPass
              ? `The cutover skips the convergence rule: the running pass finishes, then the source VM stops and the final sync copies what changed. The last pass (pass ${lastPass.number}) changed ${formatBytes(lastPass.bytes_changed)}. `
              : 'The cutover skips the convergence rule. The full copy is still running: the cutover follows it, and the final sync copies what changed meanwhile. '}
            The estimated downtime is {forced !== null ? formatDuration(forced) : 'unknown'}
            {/* forcing skips the passes that would shorten it: say when it misses the SLO already */}
            {slo ? (forced !== null && plan && forced > plan.downtime_slo_s ? `, over the SLO of ${slo}` : ` against an SLO of ${slo}`) : ''}.{' '}
            {fourEyes
              ? 'Another approver still approves the cutover; the window and a free cutover slot still apply.'
              : 'This also records your approval; the window and a free cutover slot still apply.'}
          </>
        }
        confirmLabel="Force cutover"
        canConfirm={reason.trim().length >= FORCE_REASON_MIN}
        confirmReason={`Enter a reason of at least ${FORCE_REASON_MIN} characters.`}
        onConfirm={() => submit({ action: 'force-cutover', body: { reason: reason.trim() } })}
      >
        <TextAreaField
          label="Reason"
          required
          rows={2}
          maxLength={ACTION_TEXT_MAX}
          value={reason}
          onChange={(e) => setReason(e.target.value)}
          hint={`At least ${FORCE_REASON_MIN} characters; recorded in the audit trail.`}
        />
      </ConfirmDialog>

      <ConfirmDialog
        {...common}
        open={open === 'sync'}
        title={`Run a delta sync for ${m.vm.name}?`}
        description="Starts a keep-warm pass now so the final delta at cutover stays small. The source VM keeps running."
        confirmLabel="Sync now"
        onConfirm={() => submit({ action: 'sync' })}
      />

      <ConfirmDialog
        {...common}
        open={open === 'retry'}
        title={`Retry ${m.vm.name}?`}
        description={`The migration returns to ready and starts over from its first step.${
          m.downtime_started_at && !m.downtime_ended_at
            ? ' The source VM is still stopped: its downtime clock keeps running until the next cutover is verified.'
            : ''
        }${
          // SDD §5.1: a completed plan is driven no more; the retry returns it to running
          plan?.status === 'completed' ? ' The plan has completed: the retry reopens it, so the new attempt runs.' : ''
        } Make sure the cause of the failure is fixed.`}
        confirmLabel="Retry"
        onConfirm={() => submit({ action: 'retry' })}
      />

      <ConfirmDialog
        {...common}
        open={open === 'rollback'}
        tone="danger"
        title={`Roll back ${m.vm.name}?`}
        description="Deletes the RHOSO instance (volumes are kept) and starts the source VM again. Downtime ends when the source is running."
        confirmLabel="Roll back"
        canConfirm={reason.trim().length > 0}
        confirmReason="Enter a reason first."
        onConfirm={() => submit({ action: 'rollback', body: { reason: reason.trim() } })}
      >
        <TextAreaField label="Reason" required rows={2} maxLength={ACTION_TEXT_MAX} value={reason} onChange={(e) => setReason(e.target.value)} hint="Required; recorded in the audit trail." />
      </ConfirmDialog>

      <ConfirmDialog
        {...common}
        open={open === 'cancel'}
        tone="danger"
        title={`Cancel ${m.vm.name}?`}
        description={
          hasDataPath(m)
            ? 'The migration stops for good and is removed from its wave. The source VM keeps running; the data copied so far — destination volumes and source snapshots — is then removed, and the timeline reports the outcome.'
            : 'The migration stops for good and is removed from its wave. The source VM is not touched.'
        }
        confirmLabel="Cancel migration"
        cancelLabel="Keep migration"
        onConfirm={() => submit({ action: 'cancel', body: reason.trim() ? { reason: reason.trim() } : {} })}
      >
        <TextAreaField label="Reason (optional)" rows={2} maxLength={ACTION_TEXT_MAX} value={reason} onChange={(e) => setReason(e.target.value)} />
      </ConfirmDialog>

      <ConfirmDialog
        {...common}
        open={open === 'finalize'}
        tone="danger"
        title={`Finalize ${m.vm.name}?`}
        description="Finalizing is irreversible: rollback is no longer possible and the source VM's temporary resources are cleaned up."
        confirmLabel="Finalize"
        requireText={m.vm.name}
        onConfirm={() => submit({ action: 'finalize', body: { confirm: m.vm.name, delete_source: deleteSource } })}
      >
        {m.source_running_at && (
          // the source watch saw it running (SDD §8.1, ADV-19)
          <p className="flex items-start gap-2 rounded-md border border-status-danger/40 bg-status-danger/10 p-2.5 text-sm text-foreground">
            <TriangleAlert aria-hidden className="mt-0.5 size-4 shrink-0 text-status-danger" />
            The source VM runs again: what it wrote since is not on RHOSO. Stop it, or roll it back instead.
          </p>
        )}
        <label htmlFor={deleteId} className="flex min-h-11 cursor-pointer items-start gap-2.5 text-sm">
          <input id={deleteId} type="checkbox" className="mt-1 size-4 accent-accent" checked={deleteSource} onChange={(e) => setDeleteSource(e.target.checked)} />
          <span>
            Also delete the source VM
            <span className="block text-xs text-muted-foreground">Leave unchecked to keep the stopped source VM for a while.</span>
          </span>
        </label>
      </ConfirmDialog>
    </div>
  );
}
