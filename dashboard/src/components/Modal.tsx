import { useEffect, useEffectEvent, useRef, type KeyboardEvent, type ReactNode, type RefObject } from 'react';
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
  const radios = Array.from(container.querySelectorAll<HTMLInputElement>('input[type="radio"]:not(:disabled)'));
  return Array.from(container.querySelectorAll<HTMLElement>(FOCUSABLE)).filter((el) => {
    if (el.tabIndex < 0 || el.matches(':disabled, input[type="hidden"]') || el.closest('[inert], [hidden]') || inClosedDetails(el)) return false;
    if (getComputedStyle(el).visibility === 'hidden') return false;
    for (let parent: HTMLElement | null = el; parent; parent = parent.parentElement) {
      if (getComputedStyle(parent).display === 'none') return false;
      if (parent === container) break;
    }
    if (!(el instanceof HTMLInputElement) || el.type !== 'radio' || !el.name) return true;
    const group = radios.filter((radio) => radio.name === el.name && radio.form === el.form);
    return el === (group.find((radio) => radio.checked) ?? group[0]);
  });
}

// the open dialogs, the topmost last: Escape on the page closes that one only (SDD §16)
const openDialogs: Array<{ token: symbol; panel: HTMLElement | null }> = [];
let background: { root: HTMLElement | null; inert: boolean; overflow: string; focus: HTMLElement | null } | null = null;

export interface ModalProps {
  open: boolean;
  onClose: () => void;
  labelledBy: string;
  describedBy?: string;
  /** `alertdialog` for confirmations of consequential actions. */
  role?: 'dialog' | 'alertdialog';
  initialFocusRef?: RefObject<HTMLElement | null>;
  /**
   * Where focus goes on close instead of the control that opened the dialog, when that control may
   * be gone by then (an empty state the action replaces): focus never drops to the page (SDD §16).
   */
  returnFocusRef?: RefObject<HTMLElement | null>;
  /** When false, Escape does nothing (e.g. while a request is in flight). */
  dismissible?: boolean;
  size?: 'md' | 'lg';
  /** `left`: a drawer along the left edge, the full height of the window (the phone navigation, SDD §16). */
  placement?: 'center' | 'left';
  /** A tap on the dimmed page closes the dialog (when it is dismissible). */
  closeOnBackdrop?: boolean;
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
  placement = 'center',
  closeOnBackdrop = false,
  children,
}: ModalProps) {
  const panelRef = useRef<HTMLDivElement>(null);
  // the page's Escape listener, set up once per opening, closes with the props of the last render (an Effect Event)
  const onPageEscape = useEffectEvent(() => {
    if (dismissible) onClose();
  });

  useEffect(() => {
    if (!open) return;
    const previouslyFocused = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const appRoot = document.getElementById('root');
    if (openDialogs.length === 0) {
      background = { root: appRoot, inert: appRoot?.hasAttribute('inert') ?? false, overflow: document.body.style.overflow, focus: previouslyFocused };
    }
    openDialogs.at(-1)?.panel?.setAttribute('inert', '');
    appRoot?.setAttribute('inert', '');
    document.body.style.overflow = 'hidden';

    const target = initialFocusRef?.current ?? focusables(panelRef.current)[0] ?? panelRef.current;
    target?.focus();
    // read at close on purpose: the element the ref points to then takes the focus
    const returnTo = returnFocusRef;

    // Escape closes the topmost dialog wherever the focus is — one that dropped to the page included; inside the
    // panel its own handler does (SDD §16)
    const token = Symbol('dialog');
    openDialogs.push({ token, panel: panelRef.current });
    const onPageKeyDown = (event: globalThis.KeyboardEvent) => {
      if (openDialogs.at(-1)?.token !== token) return;
      if (event.target instanceof Node && panelRef.current?.contains(event.target)) return;
      if (event.key === 'Escape') onPageEscape();
      if (event.key === 'Tab') {
        event.preventDefault();
        const items = focusables(panelRef.current);
        ((event.shiftKey ? items.at(-1) : items[0]) ?? panelRef.current)?.focus();
      }
    };
    document.addEventListener('keydown', onPageKeyDown);

    return () => {
      document.removeEventListener('keydown', onPageKeyDown);
      const index = openDialogs.findIndex((dialog) => dialog.token === token);
      const wasTop = index === openDialogs.length - 1;
      if (index >= 0) openDialogs.splice(index, 1);
      const top = openDialogs.at(-1)?.panel;
      if (top) {
        if (!wasTop) return;
        top.removeAttribute('inert');
        const wanted = returnTo?.current ?? previouslyFocused;
        (wanted && top.contains(wanted) ? wanted : focusables(top)[0] ?? top).focus();
      } else if (background) {
        if (!background.inert) background.root?.removeAttribute('inert');
        document.body.style.overflow = background.overflow;
        const wanted = returnTo?.current ?? previouslyFocused;
        (wanted?.isConnected ? wanted : background.focus)?.focus();
        background = null;
      }
    };
    // Focus management runs once per opening; initialFocusRef and returnFocusRef are stable ref objects.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  if (!open) return null;

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (openDialogs.at(-1)?.panel !== event.currentTarget) return;
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
    // Safari's default Tab order skips buttons; own the complete dialog order in every engine.
    event.preventDefault();
    const active = document.activeElement;
    const index = items.indexOf(active as HTMLElement);
    const next = index >= 0
      ? items[(index + (event.shiftKey ? -1 : 1) + items.length) % items.length]
      : event.shiftKey
        ? [...items].reverse().find((item) => active && (item.compareDocumentPosition(active) & Node.DOCUMENT_POSITION_FOLLOWING)) ?? items.at(-1)
        : items.find((item) => active && (item.compareDocumentPosition(active) & Node.DOCUMENT_POSITION_PRECEDING)) ?? items[0];
    next?.focus();
  };

  return createPortal(
    <div
      className={cn(
        'fixed inset-0 z-modal flex',
        placement === 'left' ? 'items-stretch justify-start' : 'items-end justify-center p-3 sm:items-center sm:p-6',
      )}
    >
      <div
        aria-hidden
        data-testid="modal-backdrop"
        className="absolute inset-0 bg-black/60"
        onClick={closeOnBackdrop && dismissible ? onClose : undefined}
      />
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
          'relative flex flex-col overflow-y-auto shadow-xl outline-hidden',
          placement === 'left'
            ? 'animate-drawer-in h-dvh w-72 max-w-[85vw] border-r border-border bg-card'
            : cn('card animate-dialog-in max-h-[calc(100dvh-1.5rem)] w-full', size === 'md' ? 'max-w-lg' : 'max-w-3xl'),
        )}
      >
        {children}
      </div>
    </div>,
    document.body,
  );
}
