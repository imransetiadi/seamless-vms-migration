import { createContext, type RefObject } from 'react';

/**
 * Where the focus goes after a decision that removes a recommendation's actions (SDD §16): an entry of a recommendation
 * list provides its Details link, which stays, to the actions it shows.
 */
export const FocusAfterDecision = createContext<RefObject<HTMLElement | null> | null>(null);
