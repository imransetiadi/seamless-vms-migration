import { useEffect, useId, useRef, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { cn } from '../lib/cn';
import { useAnchoredNote } from '../lib/useAnchoredNote';

/**
 * A term with its one-sentence definition, shown on hover and focus (WCAG 1.4.13: the pointer may move
 * onto it, Escape dismisses it). The definition lives outside the sentence, so the sentence reads whole.
 * `className` replaces the dotted underline of a term in a sentence - a status badge whose label is the term
 * (the live indicator, SDD §16) keeps its own look.
 */
export function Term({ children, definition, className }: { children: ReactNode; definition: string; className?: string }) {
  const id = useId();
  const ref = useRef<HTMLButtonElement>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  // under the term, kept inside the viewport (lib/useAnchoredNote)
  const note = useAnchoredNote();

  const cancel = () => {
    if (timer.current) clearTimeout(timer.current);
    timer.current = null;
  };
  const show = () => {
    cancel();
    if (ref.current) note.show(ref.current);
  };
  const hide = () => {
    cancel();
    note.hide();
  };
  // leaving the term for the definition must not close it
  const hideSoon = () => {
    cancel();
    timer.current = setTimeout(() => note.hide(), 150);
  };
  useEffect(() => cancel, []);

  return (
    <>
      {/* a native button: focusable, and a tap on a touch screen shows the definition too */}
      <button
        ref={ref}
        type="button"
        aria-describedby={id}
        onClick={show}
        onMouseEnter={show}
        onMouseLeave={hideSoon}
        onFocus={show}
        onBlur={hide}
        onKeyDown={(event) => {
          // Escape dismisses a shown definition only: the dialog around it stays (WCAG 1.4.13, SDD §16)
          if (event.key === 'Escape' && note.at) {
            event.stopPropagation();
            hide();
          }
        }}
        className={cn('cursor-help rounded-sm border-0 bg-transparent p-0 font-[inherit] text-inherit', className ?? 'inline underline decoration-dotted underline-offset-4')}
      >
        {children}
      </button>
      {createPortal(
        <span ref={note.noteRef} role="tooltip" id={id} hidden={!note.at} onMouseEnter={cancel} onMouseLeave={hide} className="anchored-note" style={note.at ?? undefined}>
          {definition}
        </span>,
        document.body,
      )}
    </>
  );
}
