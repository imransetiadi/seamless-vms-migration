import { useEffect, useRef, type KeyboardEvent, type ReactNode, type RefObject } from 'react';
import { createPortal } from 'react-dom';
import { cn } from '../lib/cn';

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), summary, [tabindex]:not([tabindex="-1"])';

/** Inside a closed <details>, only its own summary is shown. */
function inClosedDetails(el: HTMLElement): boolean {
  for (let details = el.closest('details'); details; details = details.parentElement?.closest('details') ?? null) {
    const ownSummary = el.tagName === 'SUMMARY' && el.parentElement === details;
    if (!details.open && !ownSummary) return true;
  }
  return false;
}

/**
 * What Tab reaches, in order: the trap wraps from the last to the first of these (SDD §16). A radio group is one
 * stop — its checked radio, or its first while none is — and an element with tabindex -1, an inert or hidden one,
 * or one in a closed <details> is none.
 */
function focusables(container: HTMLElement | null): HTMLElement[] {
  if (!container) return [];
  const radios = Array.from(container.querySelectorAll<HTMLInputElement>('input[type="radio"]:not([disabled])'));
  return Array.from(container.querySelectorAll<HTMLElement>(FOCUSABLE)).filter((el) => {
    if (el.tabIndex < 0 || el.closest('[inert], [hidden]') || inClosedDetails(el)) return false;
    if (!(el instanceof HTMLInputElement) || el.type !== 'radio' || !el.name) return true;
    const group = radios.filter((radio) => radio.name === el.name && radio.form === el.form);
    return el === (group.find((radio) => radio.checked) ?? group[0]);
  });
}

// the open dialogs, the topmost last: Escape on the page closes that one only (SDD §16)
const openDialogs: symbol[] = [];

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
  // read by the page's Escape listener, which is set up once per opening
  const closeRef = useRef({ onClose, dismissible });
  closeRef.current = { onClose, dismissible };

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

    // Escape closes the topmost dialog wherever the focus is — one that dropped to the page included; inside the
    // panel its own handler does (SDD §16)
    const token = Symbol('dialog');
    openDialogs.push(token);
    const onPageKeyDown = (event: globalThis.KeyboardEvent) => {
      if (event.key !== 'Escape' || openDialogs[openDialogs.length - 1] !== token) return;
      if (event.target instanceof Node && panelRef.current?.contains(event.target)) return;
      if (closeRef.current.dismissible) closeRef.current.onClose();
    };
    document.addEventListener('keydown', onPageKeyDown);

    return () => {
      document.removeEventListener('keydown', onPageKeyDown);
      const index = openDialogs.indexOf(token);
      if (index >= 0) openDialogs.splice(index, 1);
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
          // a column: a form with min-h-0 shrinks to the panel and scrolls its own body between a header and its
          // actions, which then never cover the focused control (WCAG 2.4.11, SDD §16); other content scrolls the panel
          'card animate-dialog-in relative flex max-h-[calc(100dvh-1.5rem)] w-full flex-col overflow-y-auto shadow-xl outline-hidden',
          size === 'md' ? 'max-w-lg' : 'max-w-3xl',
        )}
      >
        {children}
      </div>
    </div>,
    document.body,
  );
}
