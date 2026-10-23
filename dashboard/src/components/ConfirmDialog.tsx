import { ArrowRight, CircleCheck, CircleHelp, CircleX, TriangleAlert, X, type LucideIcon } from 'lucide-react';
import { useId, useRef, useState, type FormEvent, type ReactNode, type RefObject } from 'react';
import { cn } from '../lib/cn';
import { ErrorBanner } from './ErrorBanner';
import { Button } from './Button';
import { Modal } from './Modal';

/** What a consequential action does, row by row (SDD §16): each kind with its icon and word, never colour alone. */
const CONSEQUENCES = {
  removed: { label: 'Removed', icon: CircleX, className: 'text-status-danger' },
  kept: { label: 'Kept', icon: CircleCheck, className: 'text-status-success' },
  then: { label: 'Then', icon: ArrowRight, className: 'text-muted-foreground' },
} as const;

export interface Consequence {
  kind: keyof typeof CONSEQUENCES;
  text: ReactNode;
}

export interface ConfirmDialogProps {
  open: boolean;
  title: string;
  description?: ReactNode;
  confirmLabel: string;
  cancelLabel?: string;
  tone?: 'default' | 'danger';
  /** The action's icon, in the header and on its button (a bin for a delete); by default by the tone. */
  icon?: LucideIcon;
  /** The object the action is about, under the title (its kind and id, e.g. "Provider rhosp17-dc1"). */
  subject?: ReactNode;
  /** What happens, as rows (Removed, Kept, Then); shown instead of the description. */
  consequences?: Consequence[];
  /** The user must type this text exactly (e.g. the VM name to finalize) before confirming. */
  requireText?: string;
  /** Extra fields (reason, options) rendered between the description and the buttons. */
  children?: ReactNode;
  /** Additional validation owned by `children`. */
  canConfirm?: boolean;
  /** Why Confirm is unavailable while `canConfirm` is false: the button stays focusable and says so (SDD §16). */
  confirmReason?: string;
  pending?: boolean;
  error?: unknown;
  /** Where focus goes on close when the control that opened the dialog may be gone (SDD §16). */
  returnFocusRef?: RefObject<HTMLElement | null>;
  onConfirm: () => void;
  onCancel: () => void;
}

/** Confirmation for consequential actions; destructive ones use the danger tone. */
export function ConfirmDialog({
  open,
  title,
  description,
  confirmLabel,
  cancelLabel = 'Cancel',
  tone = 'default',
  icon,
  subject,
  consequences,
  requireText,
  children,
  canConfirm = true,
  confirmReason,
  pending = false,
  error,
  returnFocusRef,
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  const titleId = useId();
  const descriptionId = useId();
  const inputId = useId();
  const mismatchId = useId();
  const consequencesId = useId();
  const [typed, setTyped] = useState('');
  const inputRef = useRef<HTMLInputElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);
  const confirmRef = useRef<HTMLButtonElement>(null);

  // each opening asks for the text anew: reset as the dialog renders, not by an effect a render later (react.dev,
  // "Adjusting some state when a prop changes")
  const [shownOpen, setShownOpen] = useState(open);
  if (open !== shownOpen) {
    setShownOpen(open);
    if (open) setTyped('');
  }

  const typedOk = requireText === undefined || typed === requireText;
  const ready = typedOk && canConfirm && !pending;
  const mismatch = typed.length > 0 && !typedOk;
  // an unavailable Confirm stays in the Tab order and says why (SDD §16); while pending it is busy instead
  const blockedReason = pending
    ? null
    : !typedOk
      ? `Type ${requireText} to confirm.`
      : !canConfirm
        ? (confirmReason ?? 'Complete the fields above first.')
        : null;

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (ready) onConfirm();
  };

  // Destructive dialogs start on the least destructive control; typed confirmation starts on its input.
  const initialFocusRef = requireText !== undefined ? inputRef : tone === 'danger' ? cancelRef : confirmRef;
  const ToneIcon = icon ?? (tone === 'danger' ? TriangleAlert : CircleHelp);
  const listed = consequences !== undefined && consequences.length > 0;
  const unavailableWhilePending = pending ? 'Wait for the request to finish.' : null;

  return (
    <Modal
      open={open}
      onClose={onCancel}
      labelledBy={titleId}
      describedBy={listed ? consequencesId : description ? descriptionId : undefined}
      role="alertdialog"
      dismissible={!pending}
      initialFocusRef={initialFocusRef}
      returnFocusRef={returnFocusRef}
    >
      <form onSubmit={submit} className="flex min-h-0 flex-col" noValidate>
        {/* plain blocks, not <header>/<footer>: those would be page landmarks next to the shell's (axe) */}
        <div className="flex items-start gap-3 px-5 pb-3 pt-5">
          <span
            aria-hidden
            data-testid="confirm-tone-icon"
            data-tone={tone}
            className={cn(
              'flex size-10 shrink-0 items-center justify-center rounded-full',
              tone === 'danger' ? 'bg-status-danger/15 text-status-danger' : 'bg-status-info/15 text-status-info',
            )}
          >
            <ToneIcon className="size-5" />
          </span>
          <div className="min-w-0 flex-1 pt-1.5">
            <h2 id={titleId} className="wrap-break-word text-lg font-semibold leading-snug text-foreground">
              {title}
            </h2>
            {subject && <p className="mt-1 wrap-break-word font-mono text-xs text-muted-foreground">{subject}</p>}
          </div>
          {/* does what Cancel does; unavailable while the request runs, saying why (SDD §16) */}
          <Button variant="ghost" size="lg" aria-label="Close" onClick={onCancel} disabledReason={unavailableWhilePending} className="-mr-2 -mt-1">
            <X aria-hidden className="size-4" />
          </Button>
        </div>
        <div className="flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto px-5 pb-5">
          {listed ? (
            <ul id={consequencesId} aria-label="What happens" className="flex flex-col gap-2 rounded-md border border-border bg-muted/40 p-3 text-sm">
              {consequences.map((consequence, index) => {
                const meta = CONSEQUENCES[consequence.kind];
                return (
                  <li key={index} className="flex items-start gap-2">
                    <meta.icon aria-hidden className={cn('mt-0.5 size-4 shrink-0', meta.className)} />
                    <span className={cn('w-16 shrink-0 font-medium', meta.className)}>{meta.label}</span>{' '}
                    <span className="min-w-0 flex-1 wrap-break-word text-foreground">{consequence.text}</span>
                  </li>
                );
              })}
            </ul>
          ) : (
            description && (
              <div id={descriptionId} className="text-sm text-muted-foreground">
                {description}
              </div>
            )
          )}
          {children}
          {requireText !== undefined && (
            <div>
              <label htmlFor={inputId} className="field-label">
                Type <code className="rounded-sm bg-muted px-1 py-0.5 text-foreground">{requireText}</code> to confirm
              </label>
              <input
                ref={inputRef}
                id={inputId}
                className="input font-mono"
                value={typed}
                onChange={(e) => setTyped(e.target.value)}
                autoComplete="off"
                autoCapitalize="off"
                spellCheck={false}
                aria-invalid={mismatch ? true : undefined}
                aria-describedby={mismatch ? mismatchId : undefined}
              />
              {mismatch && (
                <p id={mismatchId} className="field-error">
                  The text does not match yet.
                </p>
              )}
              {/* always on the page, so the match is announced when it happens (SDD §16) */}
              <p role="status" data-testid="confirm-match" className="mt-1.5 flex items-center gap-1 text-xs font-medium text-status-success">
                {typedOk && typed.length > 0 && (
                  <>
                    <CircleCheck aria-hidden className="size-3.5 shrink-0" />
                    Matches
                  </>
                )}
              </p>
            </div>
          )}
          {error !== undefined && error !== null && <ErrorBanner error={error} title={`${confirmLabel} failed`} reveal />}
        </div>
        <div data-testid="confirm-actions" className="flex flex-col-reverse gap-2 border-t border-border bg-muted/30 px-5 py-4 sm:flex-row sm:justify-end">
          {/* unavailable while the request runs, yet focusable and saying why (SDD §16) */}
          <Button ref={cancelRef} size="lg" onClick={onCancel} disabledReason={unavailableWhilePending}>
            {cancelLabel}
          </Button>
          <Button
            ref={confirmRef}
            type="submit"
            size="lg"
            variant={tone === 'danger' ? 'danger' : 'primary'}
            icon={icon}
            loading={pending}
            disabledReason={blockedReason}
          >
            {confirmLabel}
          </Button>
        </div>
      </form>
    </Modal>
  );
}
