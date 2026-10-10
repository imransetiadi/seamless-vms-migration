import { useEffect, useRef, type KeyboardEvent, type ReactNode, type RefObject } from 'react';
import { createPortal } from 'react-dom';
import { cn } from '../lib/cn';

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

function focusables(container: HTMLElement | null): HTMLElement[] {
  if (!container) return [];
  return Array.from(container.querySelectorAll<HTMLElement>(FOCUSABLE)).filter((el) => !el.hasAttribute('inert'));
}

export interface ModalProps {
  open: boolean;
  onClose: () => void;
  labelledBy: string;
  describedBy?: string;
  /** `alertdialog` for confirmations of consequential actions. */
  role?: 'dialog' | 'alertdialog';
  initialFocusRef?: RefObject<HTMLElement>;
  /**
   * Where focus goes on close instead of the control that opened the dialog, when that control may
   * be gone by then (an empty state the action replaces): focus never drops to the page (SDD §16).
   */
  returnFocusRef?: RefObject<HTMLElement>;
  /** When false, Escape does nothing (e.g. while a request is in flight). */
  dismissible?: boolean;
  size?: 'md' | 'lg';
  children: ReactNode;
}

/**
 * Accessible modal: portal, focus moved inside and trapped, Escape to close, background made
 * inert, focus restored to the trigger on close, body scroll locked.
 */
export function Modal({
  open,
  onClose,
  labelledBy,
  describedBy,
  role = 'dialog',
  initialFocusRef,
  returnFocusRef,
  dismissible = true,
  size = 'md',
  children,
}: ModalProps) {
  const panelRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const previouslyFocused = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const appRoot = document.getElementById('root');
    appRoot?.setAttribute('inert', '');
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';

    const target = initialFocusRef?.current ?? focusables(panelRef.current)[0] ?? panelRef.current;
    target?.focus();
    // read at close on purpose: the element the ref points to then takes the focus
    const returnTo = returnFocusRef;

    return () => {
      appRoot?.removeAttribute('inert');
      document.body.style.overflow = previousOverflow;
      (returnTo?.current ?? previouslyFocused)?.focus();
    };
    // Focus management runs once per opening; initialFocusRef and returnFocusRef are stable ref objects.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  if (!open) return null;

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === 'Escape') {
      event.stopPropagation();
      if (dismissible) onClose();
      return;
    }
    if (event.key !== 'Tab') return;
    const items = focusables(panelRef.current);
    if (items.length === 0) {
      event.preventDefault();
      return;
    }
    const first = items[0];
    const last = items[items.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last?.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first?.focus();
    }
  };

  return createPortal(
    <div className="fixed inset-0 z-modal flex items-end justify-center p-3 sm:items-center sm:p-6">
      <div aria-hidden className="absolute inset-0 bg-black/60" />
      <div
        ref={panelRef}
        role={role}
        aria-modal="true"
        aria-labelledby={labelledBy}
        aria-describedby={describedBy}
        tabIndex={-1}
        onKeyDown={onKeyDown}
        className={cn(
          'card animate-dialog-in relative max-h-[calc(100dvh-1.5rem)] w-full overflow-y-auto shadow-xl outline-hidden',
          size === 'md' ? 'max-w-lg' : 'max-w-3xl',
        )}
      >
        {children}
      </div>
    </div>,
    document.body,
  );
}
