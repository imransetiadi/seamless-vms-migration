import { Globe, Plus, X } from 'lucide-react';
import { useState, type FormEvent } from 'react';
import { useCreateNotificationSubscription, useDeleteNotificationSubscription, useNotificationChannels, useNotificationSubscriptions } from '../api/hooks';
import { useRole } from '../api/session';
import { DEFAULT_NOTIFY_TOPICS, NOTIFY_TOPICS, type NotificationChannelView, type NotificationSubscription, type NotifyTopic } from '../api/types';
import { CHANNEL_KIND_LABELS, TOPIC_LABELS, topicsText } from '../lib/notifications';
import { hasRole } from '../lib/roles';
import { Button } from './Button';
import { Checkbox } from './Checkbox';
import { ErrorBanner } from './ErrorBanner';
import { SelectField } from './Field';
import { Panel } from './Panel';
import { LoadingBlock } from './Skeleton';
import { StatusBadge } from './StatusBadge';

/** Why a subscription cannot be added or removed below the operator role (SDD §24.4). */
export const OPERATOR_REASON = 'Requires the operator role.';
/** Why an every-plan subscription is not removed from a plan's page (SDD §16, §24.4). */
export const EVERY_PLAN_REASON = 'An every-plan subscription is removed on the Notifications page by an admin.';

export interface SubscriptionFormProps {
  /** The plan the subscription is to, or null for every plan (an admin's). */
  planId: string | null;
  /** The catalog, once loaded; undefined while it is not. */
  channels: NotificationChannelView[] | undefined;
  /** Why the catalog could not be loaded, when it could not. */
  channelsError?: unknown;
  /** Why Add is unavailable to this role, when it is. */
  disabledReason?: string | null;
  onAdded?: () => void;
}

/**
 * Add subscription (SDD §16, §24.4): a channel of the catalog and the topics, the defaults checked. A refused one shows the
 * API's reason; Add stays focusable, saying why, while it cannot be used.
 */
export function SubscriptionForm({ planId, channels, channelsError, disabledReason, onAdded }: SubscriptionFormProps) {
  const create = useCreateNotificationSubscription();
  const [channel, setChannel] = useState('');
  const [topics, setTopics] = useState<NotifyTopic[]>([...DEFAULT_NOTIFY_TOPICS]);
  const options = (channels ?? []).map((c) => ({ value: c.name, label: `${c.name} (${CHANNEL_KIND_LABELS[c.kind]})` }));
  const chosen = options.some((option) => option.value === channel) ? channel : (options[0]?.value ?? '');
  const reason =
    disabledReason ??
    (channelsError ? 'The channel catalog could not be loaded.' : channels === undefined ? 'The channel catalog is not loaded yet.' : null) ??
    (options.length === 0 ? 'Register a channel on the Notifications page first.' : topics.length === 0 ? 'Choose at least one topic.' : null);

  function submit(event: FormEvent) {
    event.preventDefault();
    if (reason || create.isPending) return;
    create.mutate(
      { channel: chosen, plan_id: planId, topics: NOTIFY_TOPICS.filter((topic) => topics.includes(topic)) },
      {
        onSuccess: () => {
          setTopics([...DEFAULT_NOTIFY_TOPICS]);
          onAdded?.();
        },
      },
    );
  }

  return (
    <form onSubmit={submit} className="flex flex-col gap-3" noValidate>
      <SelectField
        label="Channel"
        value={chosen}
        onChange={(event) => {
          create.reset();
          setChannel(event.target.value);
        }}
        options={options.length ? options : [{ value: '', label: 'No channels registered' }]}
        disabled={options.length === 0}
        hint={planId === null ? 'A channel of the catalog, to follow every plan.' : 'A channel of the catalog, to follow this plan.'}
      />
      <fieldset className="flex min-w-0 flex-col">
        <legend className="field-label">Topics</legend>
        <p className="text-xs text-muted-foreground">Approval, Downtime and Problem unless you say otherwise; at least one.</p>
        <div className="grid gap-x-4 sm:grid-cols-2">
          {NOTIFY_TOPICS.map((topic) => (
            <Checkbox
              key={topic}
              label={TOPIC_LABELS[topic].label}
              checked={topics.includes(topic)}
              onChange={(on) => setTopics((current) => (on ? [...current, topic] : current.filter((t) => t !== topic)))}
            />
          ))}
        </div>
      </fieldset>
      {create.error ? <ErrorBanner error={create.error} title="The subscription was not added" /> : null}
      <div>
        <Button size="lg" variant="primary" type="submit" icon={Plus} loading={create.isPending} disabledReason={reason}>
          Add subscription
        </Button>
      </div>
    </form>
  );
}

export interface SubscriptionItemProps {
  subscription: NotificationSubscription;
  /** The channel's kind, when the catalog names it. */
  kind: NotificationChannelView['kind'] | undefined;
  /** Marked "Every plan": one that reaches a plan from every plan. */
  everyPlanMark: boolean;
  /** Why Remove is unavailable, when it is. */
  removeReason: string | null;
  /** The Remove button, when the role may see it at all. */
  removable: boolean;
}

/** One subscription (SDD §16): its channel with its kind, its topics in words, and Remove. */
export function SubscriptionItem({ subscription, kind, everyPlanMark, removeReason, removable }: SubscriptionItemProps) {
  const remove = useDeleteNotificationSubscription();
  return (
    <li aria-label={subscription.channel} className="flex flex-wrap items-start justify-between gap-2 py-2">
      <div className="min-w-0 [overflow-wrap:anywhere]">
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-medium">{subscription.channel}</span>
          {kind ? <span className="text-sm text-muted-foreground">{CHANNEL_KIND_LABELS[kind]}</span> : null}
          {everyPlanMark ? <StatusBadge tone="info" icon={Globe} label="Every plan" /> : null}
        </div>
        <p className="text-sm text-muted-foreground">{topicsText(subscription.topics)}</p>
        {remove.error ? <ErrorBanner error={remove.error} title="The subscription was not removed" className="mt-2" /> : null}
      </div>
      {removable ? (
        <Button icon={X} aria-label={`Remove ${subscription.channel}`} loading={remove.isPending} disabledReason={removeReason} onClick={() => remove.mutate(subscription.id)}>
          Remove
        </Button>
      ) : null}
    </li>
  );
}

/**
 * The plan page's Notifications panel (SDD §16, §24.4, plan task NT9): the plan's subscriptions and the every-plan ones
 * that reach it; an operator adds one or removes the plan's own. Adding or removing one leaves the plan as it is.
 */
export function PlanNotificationsPanel({ planId }: { planId: string }) {
  const role = useRole();
  const own = useNotificationSubscriptions({ plan_id: planId });
  const all = useNotificationSubscriptions();
  const channels = useNotificationChannels();
  const canEdit = hasRole(role, 'operator');
  const kindOf = new Map((channels.data ?? []).map((c) => [c.name, c.kind]));
  const rows = own.data && all.data ? [...own.data, ...all.data.filter((sub) => sub.plan_id === null)] : null;
  const loadError = own.error ?? all.error;
  return (
    <Panel title="Notifications" description="The channels that follow this plan, for their topics. Adding or removing one leaves the plan as it is.">
      {(own.isPending || all.isPending) && <LoadingBlock label="Loading subscriptions…" rows={2} />}
      {loadError ? (
        <ErrorBanner
          error={loadError}
          title="Subscriptions are unavailable"
          onRetry={() => {
            void own.refetch();
            void all.refetch();
          }}
        />
      ) : null}
      {rows &&
        (rows.length ? (
          <ul aria-label="Subscriptions" className="divide-y divide-border">
            {rows.map((sub) => (
              <SubscriptionItem
                key={sub.id}
                subscription={sub}
                kind={kindOf.get(sub.channel)}
                everyPlanMark={sub.plan_id === null}
                removable
                removeReason={!canEdit ? OPERATOR_REASON : sub.plan_id === null ? EVERY_PLAN_REASON : null}
              />
            ))}
          </ul>
        ) : (
          <p className="text-sm text-muted-foreground">No channel follows this plan.</p>
        ))}
      <div className="mt-4 border-t border-border pt-4">
        <h3 className="mb-3 text-sm font-semibold text-foreground">Add subscription</h3>
        <SubscriptionForm planId={planId} channels={channels.data} channelsError={channels.error} disabledReason={canEdit ? null : OPERATOR_REASON} />
      </div>
    </Panel>
  );
}
