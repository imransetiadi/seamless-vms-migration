import { useEffect, useId, useRef, useState, type ReactNode } from 'react';
import { createPortal } from 'react-dom';

/**
 * A term with its one-sentence definition, shown on hover and focus (WCAG 1.4.13: the pointer may move
 * onto it, Escape dismisses it). The definition lives outside the sentence, so the sentence reads whole.
 */
export function Term({ children, definition }: { children: ReactNode; definition: string }) {
  const id = useId();
  const ref = useRef<HTMLButtonElement>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
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
          if (event.key === 'Escape') hide();
        }}
        className="inline cursor-help rounded-sm border-0 bg-transparent p-0 font-[inherit] text-inherit underline decoration-dotted underline-offset-4"
      >
        {children}
      </button>
      {createPortal(
        <span role="tooltip" id={id} hidden={!at} onMouseEnter={cancel} onMouseLeave={hide} className="tooltip-note" style={at ?? undefined}>
          {definition}
        </span>,
        document.body,
      )}
    </>
  );
}
