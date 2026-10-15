import { describe, expect, it } from 'vitest';
import { PERSISTED_EVENT_KINDS } from '../api/types';
import { EVENT_CATEGORIES } from './eventCategories';

const categoriesOf = (kind: string) => EVENT_CATEGORIES.filter((c) => c.value !== 'all' && c.match(kind)).map((c) => c.value);

describe('the Events page categories (SDD §4.3, §14.5, §16)', () => {
  it.each(PERSISTED_EVENT_KINDS)('files %s under a category besides All (review RV13-05)', (kind) => {
    expect(categoriesOf(kind)).not.toEqual([]);
  });

  it.each(['policy.created', 'policy.updated', 'policy.deleted'])(
    'files %s under Advisor and Security: it is advisor activity, and a policy change can weaken an approval gate (review RV13-05)',
    (kind) => {
      expect(categoriesOf(kind)).toEqual(['advisor', 'security']);
    },
  );
});
