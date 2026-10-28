/** Router state of a control that switches the view in place and keeps the focus (WCAG 3.2.2, SDD §16). */
export const KEEP_FOCUS = { keepFocus: true } as const;

/** Whether a navigation came from such a control: the layout then leaves the focus where it is. */
export function keepsFocus(state: unknown): boolean {
  return typeof state === 'object' && state !== null && (state as { keepFocus?: unknown }).keepFocus === true;
}

/** A malformed URL fragment must not prevent the page from rendering. */
export function hashTarget(hash: string): HTMLElement | null {
  if (hash.length < 2) return null;
  let id: string;
  try {
    id = decodeURIComponent(hash.slice(1));
  } catch {
    return null;
  }
  return document.getElementById(id);
}
