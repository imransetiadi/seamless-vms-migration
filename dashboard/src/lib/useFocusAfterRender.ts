import { useCallback, useLayoutEffect, useRef } from 'react';

type Target = () => HTMLElement | null | undefined;

/**
 * Moves the focus to an element a state update is about to draw (SDD §16: the focus follows content the
 * dialog swaps in place and never drops to the page).
 *
 * A bare `requestAnimationFrame(() => ref.current?.focus())` races React: on a busy machine the frame can
 * run before the commit, find no element and leave the focus on `<body>`. The target is looked up after
 * every commit until it exists, with one frame as the fallback for an update that draws nothing new.
 */
export function useFocusAfterRender(): (target: Target) => void {
  const pending = useRef<Target | null>(null);

  const attempt = useCallback(() => {
    const element = pending.current?.();
    if (!element) return;
    pending.current = null;
    element.focus();
  }, []);

  useLayoutEffect(attempt);

  return useCallback(
    (target: Target) => {
      pending.current = target;
      requestAnimationFrame(attempt);
    },
    [attempt],
  );
}
