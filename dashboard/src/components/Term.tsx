import { useEffect, useId, useLayoutEffect, useRef, useState, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { cn } from '../lib/cn';

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
  const tooltipRef = useRef<HTMLSpanElement>(null);
  const [at, setAt] = useState<{ top: number; left: number } | null>(null);

  const cancel = () => {
    if (timer.current) clearTimeout(timer.current);
    timer.current = null;
  };
  const show = () => {
    cancel();
    const rect = ref.current?.getBoundingClientRect();
    if (rect) setAt({ top: rect.bottom + 6, left: Math.max(8, Math.min(rect.left, window.innerWidth - 296)) });
  };
  useLayoutEffect(() => {
    if (!at || !tooltipRef.current) return;
    const box = tooltipRef.current.getBoundingClientRect();
    const top = Math.max(8, box.bottom > window.innerHeight - 8 ? (ref.current?.getBoundingClientRect().top ?? at.top) - box.height - 6 : at.top);
    const left = Math.max(8, Math.min(at.left, window.innerWidth - box.width - 8));
    if (top !== at.top || left !== at.left) setAt({ top, left });
  }, [at]);
  const hide = () => {
    cancel();
    setAt(null);
  };
  // leaving the term for the definition must not close it
  const hideSoon = () => {
    cancel();
    timer.current = setTimeout(() => setAt(null), 150);
  };
  useEffect(() => cancel, []);
  useEffect(() => {
    if (!at) return;
    const dismiss = (event: Event) => {
      if (event.target !== tooltipRef.current) setAt(null);
    };
    window.addEventListener('scroll', dismiss, true);
    window.addEventListener('resize', dismiss);
    return () => {
      window.removeEventListener('scroll', dismiss, true);
      window.removeEventListener('resize', dismiss);
    };
  }, [at]);

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
          if (event.key === 'Escape' && at) {
            event.stopPropagation();
            hide();
          }
        }}
        className={cn('cursor-help rounded-sm border-0 bg-transparent p-0 font-[inherit] text-inherit', className ?? 'inline underline decoration-dotted underline-offset-4')}
      >
        {children}
      </button>
      {createPortal(
        <span ref={tooltipRef} role="tooltip" id={id} hidden={!at} onMouseEnter={cancel} onMouseLeave={hide} className="tooltip-note" style={at ?? undefined}>
          {definition}
        </span>,
        document.body,
      )}
    </>
  );
}
