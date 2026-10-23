import { useEffect, useLayoutEffect, useRef, useState, type Ref } from 'react';

/** Where an anchored note shows, in viewport coordinates. */
export interface NoteAt {
  top: number;
  left: number;
}

/** The widest note: max-w-72 of the `anchored-note` utility (index.css). */
const NOTE_WIDTH = 288;
/** Kept clear of the viewport's edges. */
const EDGE = 8;
/** Between the anchor and the note. */
const GAP = 6;

export interface AnchoredNote {
  /** Where the note shows; null while it is hidden. */
  at: NoteAt | null;
  /** The note element: measured once shown, so it stays inside the viewport. */
  noteRef: Ref<HTMLSpanElement>;
  /** Shows the note under `anchor`. */
  show(anchor: Element): void;
  hide(): void;
}

/**
 * A note anchored under a control, in viewport coordinates (`position: fixed`, the `anchored-note` utility): a term's
 * definition (Term), why an action is unavailable (Button). It is left-aligned with its anchor and no further right
 * than the widest note fits; once shown and measured, it is flipped above the anchor when it would overflow the
 * bottom of the viewport and kept inside its sides, so it stays readable (SDD §16, UI-03). The page scrolling or
 * resizing dismisses it - scrolling inside the note does not - and so does `hideAfterMs`, when given.
 */
export function useAnchoredNote(hideAfterMs?: number): AnchoredNote {
  const [at, setAt] = useState<NoteAt | null>(null);
  const noteRef = useRef<HTMLSpanElement>(null);
  const anchorRef = useRef<Element | null>(null);
  useLayoutEffect(() => {
    if (!at || !noteRef.current) return;
    const box = noteRef.current.getBoundingClientRect();
    const anchorTop = anchorRef.current?.getBoundingClientRect().top ?? at.top;
    const top = Math.max(EDGE, box.bottom > window.innerHeight - EDGE ? anchorTop - box.height - GAP : at.top);
    const left = Math.max(EDGE, Math.min(at.left, window.innerWidth - box.width - EDGE));
    if (top !== at.top || left !== at.left) setAt({ top, left });
  }, [at]);
  useEffect(() => {
    if (!at) return;
    const hide = (event?: Event) => {
      if (event?.target !== noteRef.current) setAt(null);
    };
    const timer = hideAfterMs === undefined ? null : window.setTimeout(hide, hideAfterMs);
    window.addEventListener('scroll', hide, true);
    window.addEventListener('resize', hide);
    return () => {
      if (timer !== null) window.clearTimeout(timer);
      window.removeEventListener('scroll', hide, true);
      window.removeEventListener('resize', hide);
    };
  }, [at, hideAfterMs]);
  return {
    at,
    noteRef,
    show(anchor) {
      anchorRef.current = anchor;
      const rect = anchor.getBoundingClientRect();
      setAt({ top: rect.bottom + GAP, left: Math.max(EDGE, Math.min(rect.left, window.innerWidth - NOTE_WIDTH - EDGE)) });
    },
    hide() {
      setAt(null);
    },
  };
}
