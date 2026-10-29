import type { NotificationChannel, NotifyTopic } from '../api/types';
import { NOTIFY_TOPICS } from '../api/types';

/** The names of the channel kinds (SDD §16, §24.3). */
export const CHANNEL_KIND_LABELS: Readonly<Record<NotificationChannel['kind'], string>> = { email: 'Email', slack: 'Slack', webhook: 'Webhook' };

/** Each topic's name and what it notifies, as §24.5 puts it (SDD §16). */
export const TOPIC_LABELS: Readonly<Record<NotifyTopic, { label: string; hint: string }>> = {
  approval: { label: 'Approval', hint: 'A cutover, a wave or an advisor decision waits for an approval.' },
  downtime: { label: 'Downtime', hint: 'A source VM stopped, and its downtime ended.' },
  problem: { label: 'Problem', hint: 'A migration failed, its figures do not hold together, or a completed VM runs again at the source.' },
  outcome: { label: 'Outcome', hint: 'A migration completed, rolled back or was finalized.' },
  progress: { label: 'Progress', hint: 'A plan or a wave started, paused, completed, was cancelled or finalized.' },
};

/** A subscription's topics in words, in the order §24.5 lists them (SDD §16). */
export function topicsText(topics: readonly NotifyTopic[]): string {
  return NOTIFY_TOPICS.filter((topic) => topics.includes(topic))
    .map((topic) => TOPIC_LABELS[topic].label)
    .join(', ');
}

/** The bytes of a signing key the browser makes (SDD §16, §24.3): within the 24-64 the API takes. */
export const SIGNING_KEY_BYTES = 32;

/** A Standard Webhooks signing key made in the browser: `whsec_` and the base64 of 32 random bytes (SDD §16, §24.3). */
export function generateSigningKey(): string {
  const bytes = new Uint8Array(SIGNING_KEY_BYTES);
  crypto.getRandomValues(bytes);
  return `whsec_${btoa(String.fromCharCode(...bytes))}`;
}
