import { LoaderCircle, type LucideIcon } from 'lucide-react';
import { forwardRef, useEffect, useId, useState, type ButtonHTMLAttributes, type KeyboardEvent, type MouseEvent } from 'react';
import { buttonClassName, type ButtonSize, type ButtonVariant } from '../lib/buttonStyles';

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
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
  // where the note about an unavailable action shows, under the button (viewport coordinates)
  const [note, setNote] = useState<{ top: number; left: number } | null>(null);
  useEffect(() => {
    if (!note) return;
    const hide = () => setNote(null);
    const timer = window.setTimeout(hide, 4000);
    window.addEventListener('scroll', hide, true);
    window.addEventListener('resize', hide);
    return () => {
      window.clearTimeout(timer);
      window.removeEventListener('scroll', hide, true);
      window.removeEventListener('resize', hide);
    };
  }, [note]);
  const handleClick = (event: MouseEvent<HTMLButtonElement>) => {
    if (softDisabled || loading) {
      event.preventDefault();
      if (softDisabled) {
        const rect = event.currentTarget.getBoundingClientRect();
        setNote({ top: rect.bottom + 6, left: Math.max(8, Math.min(rect.left, window.innerWidth - 296)) });
      }
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
        disabled={disabled || (loading && !softDisabled)}
        aria-disabled={softDisabled || undefined}
        aria-busy={loading || undefined}
        title={softDisabled ? (disabledReason ?? undefined) : title}
        onClick={handleClick}
        onBlur={(event) => {
          setNote(null);
          onBlur?.(event);
        }}
        onKeyDown={(event: KeyboardEvent<HTMLButtonElement>) => {
          if (event.key === 'Escape') setNote(null);
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
        <span
          id={reasonId}
          role={note ? 'note' : undefined}
          className={note ? 'disabled-reason' : 'sr-only'}
          style={note ? { top: note.top, left: note.left } : undefined}
        >
          {disabledReason}
        </span>
      )}
    </>
  );
});
