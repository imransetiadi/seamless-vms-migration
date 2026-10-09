import { useEffect, useId, useRef, useState, type FormEvent, type ReactNode } from 'react';
import { ErrorBanner } from './ErrorBanner';
import { Button } from './Button';
import { Modal } from './Modal';

export interface ConfirmDialogProps {
  open: boolean;
  title: string;
  description?: ReactNode;
  confirmLabel: string;
  cancelLabel?: string;
  tone?: 'default' | 'danger';
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
  requireText,
  children,
  canConfirm = true,
  confirmReason,
  pending = false,
  error,
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  const titleId = useId();
  const descriptionId = useId();
  const inputId = useId();
  const mismatchId = useId();
  const [typed, setTyped] = useState('');
  const inputRef = useRef<HTMLInputElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);
  const confirmRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (open) setTyped('');
  }, [open]);

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

  return (
    <Modal
      open={open}
      onClose={onCancel}
      labelledBy={titleId}
      describedBy={description ? descriptionId : undefined}
      role="alertdialog"
      dismissible={!pending}
      initialFocusRef={initialFocusRef}
    >
      <form onSubmit={submit} className="flex flex-col gap-4 p-5" noValidate>
        <h2 id={titleId} className="text-lg font-semibold text-foreground">
          {title}
        </h2>
        {description && (
          <div id={descriptionId} className="text-sm text-muted-foreground">
            {description}
          </div>
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
          </div>
        )}
        {error !== undefined && error !== null && <ErrorBanner error={error} title={`${confirmLabel} failed`} />}
        <div className="flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
          <Button ref={cancelRef} size="lg" onClick={onCancel} disabled={pending}>
            {cancelLabel}
          </Button>
          <Button
            ref={confirmRef}
            type="submit"
            size="lg"
            variant={tone === 'danger' ? 'danger' : 'primary'}
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
