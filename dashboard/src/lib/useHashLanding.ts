import { useEffect } from 'react';
import { useLocation } from 'react-router-dom';
import { hashTarget } from './navigation';

/**
 * A link to a part of a page (`#policy-…`, `#findings`) lands on it once the page shows it: scrolled to and
 * focused (SDD §16). The shell keeps that focus (components/Layout.tsx).
 */
export function useHashLanding(ready: boolean): void {
  const { hash } = useLocation();
  useEffect(() => {
    if (!ready || hash.length < 2) return;
    const target = hashTarget(hash);
    target?.scrollIntoView?.({ block: 'start' });
    target?.focus({ preventScroll: true });
  }, [hash, ready]);
}
