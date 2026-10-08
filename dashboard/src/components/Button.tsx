import { LoaderCircle, type LucideIcon } from 'lucide-react';
import { forwardRef, useId, type ButtonHTMLAttributes, type MouseEvent } from 'react';
import { buttonClassName, type ButtonSize, type ButtonVariant } from '../lib/buttonStyles';

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  loading?: boolean;
  icon?: LucideIcon;
  /**
   * When set, the button is disabled but stays focusable (`aria-disabled`) and exposes the reason
   * to assistive technology and as a tooltip, so operators can discover why an action is unavailable.
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
    title,
    ...rest
  },
  ref,
) {
  const reasonId = useId();
  const softDisabled = Boolean(disabledReason);
  const handleClick = (event: MouseEvent<HTMLButtonElement>) => {
    if (softDisabled || loading) {
      event.preventDefault();
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
        <span id={reasonId} className="sr-only">
          {disabledReason}
        </span>
      )}
    </>
  );
});
