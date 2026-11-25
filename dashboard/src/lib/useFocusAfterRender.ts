import { useCallback, useLayoutEffect, useRef } from 'react';

type Target = () => HTMLElement | null | undefined;

/**
 * Moves the focus to an element a state update is about to draw (SDD §16: the focus follows content the
 * dialog swaps in place and never drops to the page).
 *
 * A bare `requestAnimationFrame(() => ref.current?.focus())` races React: on a busy machine the frame can
 * run before the commit, find no element and leave the focus on `<body>`. Here a frame that finds nothing
 * keeps the request, and every later commit schedules another frame until the element exists. The focus
 * always moves in a frame, never inside the commit, so a closing confirm dialog's own focus return (which
 * runs during the commit) cannot take it back afterwards.
 */
export function useFocusAfterRender(): (target: Target) => void {
  const pending = useRef<Target | null>(null);

  const attempt = useCallback(() => {
    const element = pending.current?.();
    if (!element) return;
    pending.current = null;
    element.focus();
  }, []);

  useLayoutEffect(() => {
    if (pending.current) requestAnimationFrame(attempt);
  });

  return useCallback(
    (target: Target) => {
      pending.current = target;
      requestAnimationFrame(attempt);
    },
    [attempt],
  );
}
