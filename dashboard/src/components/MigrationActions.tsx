import {
  ArrowRight,
  BadgeCheck,
  CircleSlash,
  FastForward,
  RefreshCw,
  RotateCcw,
  ThumbsUp,
  Undo2,
  Zap,
  type LucideIcon,
} from 'lucide-react';
import { Component, useId, useRef, useState, type ReactNode, type RefObject } from 'react';
import { Link } from 'react-router-dom';
import { useMe, useMigrationAction, useProviders, type MigrationActionRequest } from '../api/hooks';
import { ACTION_TEXT_MAX, ACTION_TEXT_UNITS, FORCE_REASON_MIN, type Migration, type Plan, type Role } from '../api/types';
import { waitingDecision } from '../lib/advisor';
import { buttonClassName } from '../lib/buttonStyles';
import { cn } from '../lib/cn';
import { forcedDowntime } from '../lib/forcedEstimate';
import { formatBytes, formatDateTime, formatDuration, pyLen, pyStrip, pyTooLong } from '../lib/format';
import { hasDataPath } from '../lib/fsm';
import { isWarmStrategy } from '../lib/phase';
import {
  approveDialog,
  MIGRATION_ACTION_ORDER,
  migrationActions,
  nextStep,
  requestWaitsForApprover,
  rollbackEffect,
  waitsOnClosedWindow,
  type MigrationActionKey,
} from '../lib/migrationActions';
import { destinationName } from '../lib/providerUse';
import { useNow } from '../lib/useNow';
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

function windowOpen(plan: Plan | null | undefined, now: number): boolean {
  const w = plan?.cutover_window;
  if (!w) return true;
  return now >= Date.parse(w.start) && now <= Date.parse(w.end);
}

interface KeepsActionFocusProps {
  /** The available actions in their order: a change mounts the buttons that changed rows anew. */
  layout: string;
  /** Each action's button, as last mounted. */
  buttons: RefObject<Map<MigrationActionKey, HTMLButtonElement>>;
  children: ReactNode;
}

/**
 * A button that changes rows as the migration moves keeps the focus (SDD §16): the rows are two lists, so React mounts
 * the button anew in the other one and the focus drops to the page. The action whose button has the focus is read just
 * before React changes the page - getSnapshotBeforeUpdate, which function components lack - and only its new button
 * gets it back: focus the user had moved elsewhere (the page, another control) is never taken back.
 */
class KeepsActionFocus extends Component<KeepsActionFocusProps> {
  override getSnapshotBeforeUpdate(previous: Readonly<KeepsActionFocusProps>): MigrationActionKey | null {
    // the first layout moves nothing, and as before neither does one from rows with no action available
    if (previous.layout === '' || previous.layout === this.props.layout) return null;
    for (const [key, node] of this.props.buttons.current) if (node === document.activeElement) return key;
    return null;
  }

  override componentDidUpdate(_previous: Readonly<KeepsActionFocusProps>, _state: unknown, focused: MigrationActionKey | null): void {
    const node = focused ? this.props.buttons.current.get(focused) : undefined;
    if (node && document.activeElement !== node && (document.activeElement === document.body || document.activeElement === null)) {
      node.focus({ preventScroll: true });
    }
  }

  override render(): ReactNode {
    return this.props.children;
  }
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
  // the time, read once per render for every rule that asks it (useNow): a cutover window that opens or closes shows
  // within ten seconds, so the Cutover action's dialog never speaks of a window that has changed
  const now = useNow(10_000);
  const availability = migrationActions(m, role, plan, now, me);
  const fourEyes = Boolean(plan?.require_approval && plan.require_distinct_approver);
  // a request or a force records the user's approval only without a distinct approver (SDD §5.4, §16)
  const recordsApproval = plan?.require_distinct_approver === false;
  if (planUnavailable && availability.cutover.enabled) {
    availability.cutover = { enabled: false, reason: 'The plan could not be loaded: its cutover window and approval policy are unknown.' };
  } else if (planLoading && availability.cutover.enabled) {
    availability.cutover = { enabled: false, reason: 'The plan is loading: its cutover window and approval policy are not known yet.' };
  }
  // a requested cutover waiting for a closed window: the Cutover action lets it start outside the window (SDD §5.4)
  const forcing = waitsOnClosedWindow(m, plan, now);
  // a warm cutover requested before pre-copy converged starts once it does (SDD §5.4)
  const early = isWarmStrategy(m.strategy) && m.phase !== 'awaiting_cutover';
  // maker-checker with no other approval: the request waits for another approver, it starts nothing (SDD §16)
  const waitsForApprover = requestWaitsForApprover(m, plan, me);
  const providers = useProviders();
  const step = nextStep(m, plan, now, destinationName(providers.data, plan));
  // the advisor's decision that holds the migration, when one does (SDD §14.6)
  const waiting = waitingDecision(m);
  const action = useMigrationAction(m.id);
  const [open, setOpen] = useState<MigrationActionKey | null>(null);
  const [comment, setComment] = useState('');
  const [reason, setReason] = useState('');
  const [forceWindow, setForceWindow] = useState(false);
  const [deleteSource, setDeleteSource] = useState(false);
  // its finalize began deleting the source, which runs again: only a finalize that deletes it is taken (SDD §8.1,
  // audit OC-09)
  const mustDeleteSource = m.checkpoint === 'finalizing' && Boolean(m.source_running_at);
  const [announcement, setAnnouncement] = useState('');
  const forceId = useId();
  const deleteId = useId();
  // each action's button, for the one that changes rows to keep the focus (KeepsActionFocus, SDD §16)
  const buttons = useRef(new Map<MigrationActionKey, HTMLButtonElement>());
  const remember = (key: MigrationActionKey, node: HTMLButtonElement | null) => {
    if (node) buttons.current.set(key, node);
    else if (buttons.current.get(key)?.isConnected === false) buttons.current.delete(key);
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

  // a comment or reason is sent as counted: stripped as the API's Python strips it, at most ACTION_TEXT_MAX code points,
  // which the fields' maxLength - UTF-16 units - cannot check (SDD §12, review RV50-06)
  const commentField = (
    <TextAreaField label="Comment (optional)" rows={2} maxLength={ACTION_TEXT_UNITS} value={comment} onChange={(e) => setComment(e.target.value)} hint="Recorded in the audit trail." />
  );
  const commentTooLong = pyTooLong(comment, ACTION_TEXT_MAX);
  const commentBody = pyStrip(comment) ? { comment: pyStrip(comment) } : {};
  const reasonTooLong = pyTooLong(reason, ACTION_TEXT_MAX);
  const estimate = m.estimate ? formatDuration(m.estimate.downtime_s) : 'unknown';
  // what a Force cutover records (SDD §5.4, §16): the downtime after the running pass, not the converged estimate
  const forced = plan && isWarmStrategy(m.strategy) ? forcedDowntime(m, plan) : null;
  // the API lists only passes that ended (SDD §4.2): the last one is what a Force cutover records
  const lastPass = m.sync_passes.filter((p) => p.ended_at !== null).sort((a, b) => a.number - b.number).at(-1);
  const slo = plan ? formatDuration(plan.downtime_slo_s) : null;
  const closedWindow = !windowOpen(plan, now);
  const common = { pending: action.isPending, error: action.error, onCancel: close };
  const approval = approveDialog(m, plan, now);

  // the actions available now first, the next step's first; the others in a quieter row (SDD §16)
  const offered = MIGRATION_ACTION_ORDER.filter((key) => key !== 'force-cutover' || isWarmStrategy(m.strategy));
  const available = offered
    .filter((key) => availability[key].enabled)
    .sort((a, b) => Number(b === step.action) - Number(a === step.action));
  const unavailable = offered.filter((key) => !availability[key].enabled);
  const layout = available.join(',');

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
      <KeepsActionFocus layout={layout} buttons={buttons}>
        <div role="group" aria-label="Migration actions" className="flex flex-wrap items-center gap-2">
          {waiting && (
            // SDD §16: the decision the next step asks for comes first, ahead of the migration's own actions
            <Link to={`/advisor/recommendations/${encodeURIComponent(waiting)}`} className={buttonClassName('primary', 'lg')}>
              See the recommendation
            </Link>
          )}
          {available.map((key) => {
            const primary = key === step.action;
            const destructive = key === 'rollback' || key === 'cancel' || key === 'finalize';
            return (
              <Button
                key={key}
                ref={(node) => remember(key, node)}
                size="lg"
                icon={ICON[key]}
                variant={primary ? (destructive && key !== 'finalize' ? 'danger' : 'primary') : 'secondary'}
                onClick={() => show(key)}
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
              ref={(node) => remember(key, node)}
              size="sm"
              icon={ICON[key]}
              variant="secondary"
              disabledReason={availability[key].reason}
              onClick={() => show(key)}
            >
              {key === 'cutover' && forcing ? 'Cut over outside the window' : LABEL[key]}
            </Button>
          ))}
        </div>
      </KeepsActionFocus>
      <p role="status" className="sr-only">
        {announcement}
      </p>

      <ConfirmDialog
        {...common}
        open={open === 'approve'}
        icon={ICON['approve']}
        subject={`Migration ${m.id}`}
        title={`Approve the cutover of ${m.vm.name}?`}
        // what the approval leads to (SDD §5.4, §16): the one the gate waits for starts the cutover and stops the source
        description={approval.description}
        confirmLabel={approval.confirmLabel}
        canConfirm={!commentTooLong}
        confirmReason={commentTooLong ?? undefined}
        onConfirm={() => submit({ action: 'approve', body: commentBody })}
      >
        {commentField}
      </ConfirmDialog>

      <ConfirmDialog
        {...common}
        open={open === 'cutover'}
        icon={ICON['cutover']}
        subject={`Migration ${m.id}`}
        title={
          forcing
            ? `Let ${m.vm.name} cut over outside the window?`
            : waitsForApprover
              ? `Request the cutover of ${m.vm.name}?`
              : early
                ? `Cut over ${m.vm.name} once pre-copy converges?`
                : `Cut over ${m.vm.name} now?`
        }
        description={
          <>
            {forcing && plan?.cutover_window
              ? `The cutover was requested and waits for the cutover window (${formatDateTime(plan.cutover_window.start)} → ${formatDateTime(plan.cutover_window.end)}). Confirming lets it start as soon as the plan, its wave and a free cutover slot allow. `
              : ''}
            {waitsForApprover && !forcing
              ? `The cutover is requested now and starts once another approver approves it${early ? ' and pre-copy has converged' : ''}. `
              : early && !forcing
                ? 'The cutover is requested now and starts once pre-copy has converged and the plan, its wave and a free cutover slot allow. '
                : ''}
            {(early || waitsForApprover) && !forcing ? 'Then the' : 'The'} source VM will be stopped and the downtime clock starts. Estimated downtime {estimate}
            {slo ? ` against an SLO of ${slo}` : ''}.{' '}
            {fourEyes
              ? 'Your request is not an approval: another approver approves the cutover. The migration can be rolled back until it is finalized.'
              : recordsApproval
                ? 'This also records your approval; the migration can be rolled back until it is finalized.'
                : 'The migration can be rolled back until it is finalized.'}
          </>
        }
        confirmLabel={forcing ? 'Cut over outside the window' : early || waitsForApprover ? 'Request cutover' : 'Start cutover'}
        canConfirm={!commentTooLong}
        confirmReason={commentTooLong ?? undefined}
        onConfirm={() =>
          submit({
            action: 'cutover',
            body: { force_window: forcing || (closedWindow && forceWindow), ...commentBody },
          })
        }
      >
        {m.cutover_refusals.length > 0 && (
          // the passes found what the cutover will refuse (SDD §7.2): requesting stays possible, the cutover's own
          // check stops it before the source VM stops (SDD §16)
          <div role="note" aria-label="Cutover will refuse" className="rounded-md border border-status-warning/40 bg-status-warning/10 p-2 text-sm">
            <p className="font-medium">The last pass found what the cutover will refuse:</p>
            <ul className="mt-1 list-disc pl-5">
              {m.cutover_refusals.map((problem) => (
                <li key={problem} className="wrap-break-word">
                  {problem}
                </li>
              ))}
            </ul>
            <p className="mt-1 text-xs text-muted-foreground">Fix it first: otherwise the cutover stops at its check, while the source VM still runs.</p>
          </div>
        )}
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
        icon={ICON['force-cutover']}
        subject={`Migration ${m.id}`}
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
              : recordsApproval
                ? 'This also records your approval; the window and a free cutover slot still apply.'
                : 'The window and a free cutover slot still apply.'}
          </>
        }
        confirmLabel="Force cutover"
        canConfirm={pyLen(pyStrip(reason)) >= FORCE_REASON_MIN && !reasonTooLong}
        confirmReason={reasonTooLong ?? `Enter a reason of at least ${FORCE_REASON_MIN} characters.`}
        onConfirm={() => submit({ action: 'force-cutover', body: { reason: pyStrip(reason) } })}
      >
        <TextAreaField
          label="Reason"
          required
          rows={2}
          maxLength={ACTION_TEXT_UNITS}
          value={reason}
          onChange={(e) => setReason(e.target.value)}
          hint={`At least ${FORCE_REASON_MIN} characters; recorded in the audit trail.`}
        />
      </ConfirmDialog>

      <ConfirmDialog
        {...common}
        open={open === 'sync'}
        icon={ICON['sync']}
        subject={`Migration ${m.id}`}
        title={`Run a delta sync for ${m.vm.name}?`}
        description="Starts a keep-warm pass now so the final delta at cutover stays small. The source VM keeps running."
        confirmLabel="Sync now"
        onConfirm={() => submit({ action: 'sync' })}
      />

      <ConfirmDialog
        {...common}
        open={open === 'retry'}
        icon={ICON['retry']}
        subject={`Migration ${m.id}`}
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
        icon={ICON['rollback']}
        subject={`Migration ${m.id}`}
        tone="danger"
        title={`Roll back ${m.vm.name}?`}
        description={`${rollbackEffect(m)} Downtime ends when the source is running.`}
        confirmLabel="Roll back"
        canConfirm={pyStrip(reason).length > 0 && !reasonTooLong}
        confirmReason={reasonTooLong ?? 'Enter a reason first.'}
        onConfirm={() => submit({ action: 'rollback', body: { reason: pyStrip(reason) } })}
      >
        <TextAreaField label="Reason" required rows={2} maxLength={ACTION_TEXT_UNITS} value={reason} onChange={(e) => setReason(e.target.value)} hint="Required; recorded in the audit trail." />
      </ConfirmDialog>

      <ConfirmDialog
        {...common}
        open={open === 'cancel'}
        icon={ICON['cancel']}
        subject={`Migration ${m.id}`}
        tone="danger"
        title={`Cancel ${m.vm.name}?`}
        description={
          !hasDataPath(m)
            ? 'The migration stops for good and is removed from its wave. The source VM is not touched.'
            : m.strategy === 'vmware_warm' || m.strategy === 'vmware_cold'
              ? // a VMware cancel deletes no kit volume: the kit names them after the VM (SDD §5.1, §7.2, §16)
                "The migration stops for good and is removed from its wave. The source VM keeps running; the destination server it recorded is removed, and the migration kit's volumes stay, named in the timeline — delete them once no plan migrates the VM."
              : 'The migration stops for good and is removed from its wave. The source VM keeps running; the data copied so far — destination volumes and source snapshots — is then removed, and the timeline reports the outcome.'
        }
        confirmLabel="Cancel migration"
        cancelLabel="Keep migration"
        canConfirm={!reasonTooLong}
        confirmReason={reasonTooLong ?? undefined}
        onConfirm={() => submit({ action: 'cancel', body: pyStrip(reason) ? { reason: pyStrip(reason) } : {} })}
      >
        <TextAreaField label="Reason (optional)" rows={2} maxLength={ACTION_TEXT_UNITS} value={reason} onChange={(e) => setReason(e.target.value)} />
      </ConfirmDialog>

      <ConfirmDialog
        {...common}
        open={open === 'finalize'}
        icon={ICON['finalize']}
        subject={`Migration ${m.id}`}
        tone="danger"
        title={`Finalize ${m.vm.name}?`}
        description="Finalizing is irreversible: rollback is no longer possible and the source VM's temporary resources are cleaned up."
        confirmLabel="Finalize"
        requireText={m.vm.name}
        onConfirm={() => submit({ action: 'finalize', body: { confirm: m.vm.name, delete_source: deleteSource || mustDeleteSource } })}
      >
        <label htmlFor={deleteId} className="flex min-h-11 cursor-pointer items-start gap-2.5 text-sm">
          <input
            id={deleteId}
            type="checkbox"
            className="mt-1 size-4 accent-accent"
            checked={deleteSource || mustDeleteSource}
            disabled={mustDeleteSource}
            onChange={(e) => setDeleteSource(e.target.checked)}
          />
          <span>
            {/* a space of its own before the hint: only CSS puts the hint on its own line, and the name must not rely on it */}
            Also delete the source VM{' '}
            <span className="block text-xs text-muted-foreground">
              {mustDeleteSource
                ? 'The finalize began deleting the source VM, which runs again: only deleting it finalizes it.'
                : 'Leave unchecked to keep the stopped source VM for a while.'}
            </span>
          </span>
        </label>
      </ConfirmDialog>
    </div>
  );
}
