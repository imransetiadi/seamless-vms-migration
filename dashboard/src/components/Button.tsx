import { LoaderCircle, type LucideIcon } from 'lucide-react';
import { forwardRef, useId, type ButtonHTMLAttributes, type KeyboardEvent, type MouseEvent } from 'react';
import { buttonClassName, type ButtonSize, type ButtonVariant } from '../lib/buttonStyles';
import { useAnchoredNote } from '../lib/useAnchoredNote';

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  /**
   * While its request runs the button shows a spinner, is busy and `aria-disabled`, and ignores presses;
   * it is never natively disabled, which would drop keyboard focus to the page (SDD §16).
   */
  loading?: boolean;
  icon?: LucideIcon;
  /**
   * When set, the button is disabled but stays focusable (`aria-disabled`) and exposes the reason
   * to assistive technology, as a tooltip, and in a short note under it when it is clicked or tapped
   * (touch screens have no hover, SDD §16), so operators can discover why an action is unavailable.
   */
  disabledReason?: string | null;
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  {
    variant = 'secondary',
    size = 'md',
    loading = false,
    icon: Icon,
    disabledReason,
    className,
    children,
    disabled,
    type = 'button',
    onClick,
    onBlur,
    onKeyDown,
    title,
    ...rest
  },
  ref,
) {
  const reasonId = useId();
  const softDisabled = Boolean(disabledReason);
  // the note about an unavailable action, under the button for a few seconds (lib/useAnchoredNote)
  const note = useAnchoredNote(4000);
  const handleClick = (event: MouseEvent<HTMLButtonElement>) => {
    // Safari does not focus pointer-activated buttons; dialogs need their trigger for focus restoration.
    // Only a pointer click (detail = its click count, at least 1): a keyboard activation already has the
    // focus, and the click a form's implicit submission dispatches (Enter in a field, detail 0) must leave
    // it in the field.
    if (event.detail > 0) event.currentTarget.focus({ preventScroll: true });
    if (softDisabled || loading) {
      event.preventDefault();
      if (softDisabled) note.show(event.currentTarget);
      return;
    }
    onClick?.(event);
  };
  const describedBy = [rest['aria-describedby'], softDisabled ? reasonId : null].filter(Boolean).join(' ') || undefined;

  return (
    <>
      <button
        ref={ref}
        type={type}
        disabled={disabled}
        aria-disabled={softDisabled || loading || undefined}
        aria-busy={loading || undefined}
        title={softDisabled ? (disabledReason ?? undefined) : title}
        onClick={handleClick}
        onBlur={(event) => {
          note.hide();
          onBlur?.(event);
        }}
        onKeyDown={(event: KeyboardEvent<HTMLButtonElement>) => {
          // Escape dismisses a shown note only: the dialog around it stays (WCAG 1.4.13, SDD §16)
          if (event.key === 'Escape' && note.at) {
            event.stopPropagation();
            note.hide();
          }
          onKeyDown?.(event);
        }}
        {...rest}
        aria-describedby={describedBy}
        className={buttonClassName(variant, size, className)}
      >
        {loading ? (
          <LoaderCircle aria-hidden className="size-4 shrink-0 animate-spin" />
        ) : Icon ? (
          <Icon aria-hidden className="size-4 shrink-0" />
        ) : null}
        {children}
      </button>
      {softDisabled && (
        <span ref={note.noteRef} id={reasonId} role={note.at ? 'note' : undefined} className={note.at ? 'anchored-note' : 'sr-only'} style={note.at ?? undefined}>
          {disabledReason}
        </span>
      )}
    </>
  );
});
