import { CircleCheck, CircleOff, CircleX, Copy, KeyRound, Pencil, Plus, Power, PowerOff, Save, Send, Trash2 } from 'lucide-react';
import { useId, useRef, useState, type FormEvent } from 'react';
import {
  useCreateNotificationChannel,
  useDeleteNotificationChannel,
  useNotificationChannels,
  useNotificationSubscriptions,
  useSetNotificationChannelSecret,
  useTestNotificationChannel,
  useUpdateNotificationChannel,
} from '../api/hooks';
import { useRole } from '../api/session';
import type { NotificationChannelPatch, NotificationChannelView } from '../api/types';
import { Button } from '../components/Button';
import { Checkbox } from '../components/Checkbox';
import { ConfirmDialog } from '../components/ConfirmDialog';
import { ErrorBanner } from '../components/ErrorBanner';
import { SelectField, TextAreaField, TextField } from '../components/Field';
import { Modal } from '../components/Modal';
import { PageHeader } from '../components/PageHeader';
import { Panel } from '../components/Panel';
import { LoadingBlock } from '../components/Skeleton';
import { StatusBadge } from '../components/StatusBadge';
import { SubscriptionForm, SubscriptionItem } from '../components/Subscriptions';
import { formatDateTime } from '../lib/format';
import { CHANNEL_KIND_LABELS, generateSigningKey } from '../lib/notifications';
import { usePageTitle } from '../lib/usePageTitle';

type Kind = NotificationChannelView['kind'];

interface ChannelForm {
  name: string;
  kind: Kind;
  description: string;
  /** One address a line. */
  recipients: string;
  url: string;
  redactNames: boolean;
}

const EMPTY: ChannelForm = { name: '', kind: 'email', description: '', recipients: '', url: '', redactNames: false };

const KIND_HINTS: Record<Kind, string> = {
  email: "One message through the installation's SMTP relay to the recipients: a channel is one audience.",
  slack: "One Slack incoming webhook. Its URL is the credential: set it as the channel's secret once the channel exists.",
  webhook: 'A JSON document POSTed to the URL, signed as Standard Webhooks specify with the signing key set as its secret.',
};

function formOf(channel: NotificationChannelView): ChannelForm {
  return {
    name: channel.name,
    kind: channel.kind,
    description: channel.description ?? '',
    recipients: channel.recipients.join('\n'),
    url: channel.url ?? '',
    redactNames: channel.redact_names,
  };
}

/** The fields the API takes for the form's kind (SDD §4.2, §12, §24.3): the other kinds' stay unset, an empty text is none. */
function fieldsOf(form: ChannelForm): NotificationChannelPatch {
  const text = (value: string) => (value.trim() ? value.trim() : null);
  const recipients = form.recipients
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);
  return {
    description: text(form.description),
    redact_names: form.redactNames,
    ...(form.kind === 'email' ? { recipients } : form.kind === 'webhook' ? { url: text(form.url) } : {}),
  };
}

/** A channel's state (SDD §16, §24.3): enabled or not, and its last outcome with its time, reason and failures in a row. */
function ChannelState({ channel }: { channel: NotificationChannelView }) {
  const cursor = channel.cursor;
  const failures = cursor?.failures_in_a_row ?? 0;
  return (
    <div className="flex flex-wrap items-center gap-2 text-sm">
      <StatusBadge tone={channel.enabled ? 'success' : 'neutral'} icon={channel.enabled ? CircleCheck : CircleOff} label={channel.enabled ? 'Enabled' : 'Disabled'} />
      {cursor?.last_outcome ? (
        <>
          <StatusBadge tone={cursor.last_outcome === 'sent' ? 'success' : 'danger'} icon={cursor.last_outcome === 'sent' ? CircleCheck : CircleX} label={cursor.last_outcome === 'sent' ? 'Sent' : 'Failed'} />
          <span className="text-muted-foreground [overflow-wrap:anywhere]">
            <time dateTime={cursor.last_outcome_at ?? undefined}>{formatDateTime(cursor.last_outcome_at)}</time>
            {cursor.last_outcome === 'failed' && cursor.last_reason ? `: ${cursor.last_reason}` : ''}
            {failures > 0 ? `; ${failures} failure${failures === 1 ? '' : 's'} in a row` : ''}
          </span>
        </>
      ) : (
        <span className="text-muted-foreground">No message yet</span>
      )}
    </div>
  );
}

/** Where a channel sends (SDD §16, §24.3): the recipients or the URL when the API gave them in full, else their number or the host. */
function Audience({ channel }: { channel: NotificationChannelView }) {
  if (channel.kind === 'email') {
    if (channel.recipients.length) return <p className="text-sm [overflow-wrap:anywhere]">{channel.recipients.join(', ')}</p>;
    return (
      <p className="text-sm text-muted-foreground">
        {channel.recipient_count} recipient{channel.recipient_count === 1 ? '' : 's'}
      </p>
    );
  }
  if (channel.kind === 'webhook') {
    return channel.url ? (
      <code className="text-sm [overflow-wrap:anywhere]">{channel.url}</code>
    ) : (
      <p className="text-sm text-muted-foreground">Receiver {channel.url_host ?? 'unknown'}</p>
    );
  }
  return <p className="text-sm text-muted-foreground">One Slack incoming webhook; its URL is the channel's secret.</p>;
}

/**
 * Set secret (SDD §16, §24.3): write-only. The Slack webhook's URL, or a signing key typed or generated in the browser and
 * shown once, with Copy, until the dialog closes. No value the page did not just type or make is ever shown.
 */
function SecretDialog({ channel, onClose }: { channel: NotificationChannelView | null; onClose: () => void }) {
  const titleId = useId();
  const write = useSetNotificationChannelSecret();
  const inputRef = useRef<HTMLInputElement>(null);
  const [value, setValue] = useState('');
  const [copyStatus, setCopyStatus] = useState('');
  // keyed by its channel where it is rendered: each opening is a new dialog that starts empty, with no request state
  const open = channel !== null;
  const slack = channel?.kind === 'slack';

  function submit(event: FormEvent) {
    event.preventDefault();
    if (!channel || write.isPending) return;
    write.mutate({ name: channel.name, secret: slack ? { url: value.trim() } : { signing_key: value.trim() } }, { onSuccess: onClose });
  }

  async function copy() {
    try {
      await navigator.clipboard.writeText(value);
      setCopyStatus('Key copied.');
    } catch {
      setCopyStatus('The browser refused the clipboard: select the key and copy it by hand.');
    }
  }

  return (
    <Modal open={open} onClose={onClose} labelledBy={titleId} dismissible={!write.isPending} initialFocusRef={inputRef}>
      <form onSubmit={submit} noValidate className="flex min-h-0 flex-col">
        <div className="border-b border-border px-5 py-4">
          <h2 id={titleId} className="text-lg font-semibold text-foreground [overflow-wrap:anywhere]">
            Set secret for {channel?.name ?? ''}
          </h2>
          <p className="text-sm text-muted-foreground">
            {slack ? 'Written once under the channel and never shown again. Slack revokes a webhook URL it finds published.' : 'Written once under the channel and never shown again: the receiver verifies each message with it.'}
          </p>
        </div>
        <div className="flex min-h-0 flex-col gap-4 overflow-y-auto px-5 py-4">
          {slack ? (
            <TextField
              ref={inputRef}
              label="Slack webhook URL"
              value={value}
              onChange={(event) => setValue(event.target.value)}
              required
              autoComplete="off"
              spellCheck={false}
              inputMode="url"
              hint="https://hooks.slack.com/services/…, as Slack gave it."
            />
          ) : (
            <>
              <TextField
                ref={inputRef}
                label="Signing key"
                value={value}
                onChange={(event) => {
                  setValue(event.target.value);
                  setCopyStatus('');
                }}
                required
                autoComplete="off"
                spellCheck={false}
                inputClassName="font-mono"
                hint="whsec_ and the base64 of 24 to 64 random bytes. A generated key is shown once: copy it to the receiver before saving."
              />
              <div className="flex flex-wrap gap-2">
                <Button
                  icon={KeyRound}
                  onClick={() => {
                    setValue(generateSigningKey());
                    setCopyStatus('');
                    inputRef.current?.focus();
                  }}
                >
                  Generate key
                </Button>
                <Button icon={Copy} disabledReason={value.trim() ? null : 'Type or generate a key first.'} onClick={() => void copy()}>
                  Copy key
                </Button>
              </div>
              <p role="status" className="text-sm text-muted-foreground">
                {copyStatus}
              </p>
            </>
          )}
          {write.error ? <ErrorBanner error={write.error} title="The secret was not saved" reveal /> : null}
        </div>
        <div className="flex flex-col-reverse gap-2 border-t border-border bg-card px-5 py-3 sm:flex-row sm:justify-end">
          <Button size="lg" onClick={onClose} disabledReason={write.isPending ? 'Wait for the request to finish.' : null}>
            Cancel
          </Button>
          <Button size="lg" type="submit" variant="primary" icon={Save} loading={write.isPending} disabledReason={value.trim() ? null : slack ? 'Paste the webhook URL first.' : 'Type or generate a key first.'}>
            Save secret
          </Button>
        </div>
      </form>
    </Modal>
  );
}

/** The notification catalog and the every-plan subscriptions (SDD §16, §24.9, plan task NT9): every role reads them, an admin keeps them. */
export default function Notifications() {
  usePageTitle('Notifications');
  const canManage = useRole() === 'admin';
  const channels = useNotificationChannels();
  const subscriptions = useNotificationSubscriptions();
  const create = useCreateNotificationChannel();
  const update = useUpdateNotificationChannel();
  const remove = useDeleteNotificationChannel();
  const test = useTestNotificationChannel();
  const [form, setForm] = useState<ChannelForm>(EMPTY);
  const [editing, setEditing] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<NotificationChannelView | null>(null);
  const [secretFor, setSecretFor] = useState<NotificationChannelView | null>(null);
  /** The outcome of each channel's last test on this page, until the next (SDD §16). */
  const [tested, setTested] = useState<Record<string, { outcome: 'sent' | 'failed'; reason: string | null }>>({});
  /** The channel whose Enable, Disable or Send test runs, so the other channels' buttons stay as they are. */
  const [busy, setBusy] = useState<string | null>(null);
  const nameRef = useRef<HTMLInputElement>(null);
  const write = editing ? update : create;
  const set = (key: keyof ChannelForm) => (event: { target: { value: string } }) => setForm((current) => ({ ...current, [key]: event.target.value }));

  function reset() {
    setForm(EMPTY);
    setEditing(null);
    create.reset();
    update.reset();
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    if (write.isPending) return;
    const fields = fieldsOf(form);
    const done = {
      onSuccess: () => {
        reset();
        requestAnimationFrame(() => nameRef.current?.focus());
      },
    };
    if (editing) update.mutate({ name: editing, patch: fields }, done);
    else create.mutate({ name: form.name.trim(), kind: form.kind, ...fields }, done);
  }

  function toggle(channel: NotificationChannelView) {
    setBusy(channel.name);
    update.mutate({ name: channel.name, patch: { enabled: !channel.enabled } }, { onSettled: () => setBusy(null) });
  }

  function sendTest(channel: NotificationChannelView) {
    setBusy(channel.name);
    test.mutate(channel.name, {
      onSuccess: (result) => setTested((current) => ({ ...current, [channel.name]: result })),
      onSettled: () => setBusy(null),
    });
  }

  const everyPlan = (subscriptions.data ?? []).filter((sub) => sub.plan_id === null);
  const kindOf = new Map((channels.data ?? []).map((c) => [c.name, c.kind]));
  const toggleError = update.error && busy === null && editing === null ? update.error : null;
  return (
    <>
      <PageHeader
        title="Notifications"
        description="Where the installation's events go: email, Slack and webhook channels, and the plans each one follows. A channel decides where the installation's data goes, so only an admin keeps the catalog."
      />
      <div className="grid min-w-0 gap-4 lg:grid-cols-2">
        {canManage ? (
          <Panel title={editing ? `Edit channel ${editing}` : 'Add channel'} className="[overflow-wrap:anywhere]">
            <form onSubmit={submit} className="flex flex-col gap-4">
              <TextField
                ref={nameRef}
                label="Name"
                value={form.name}
                onChange={set('name')}
                readOnly={editing !== null}
                required
                autoComplete="off"
                maxLength={63}
                hint={editing ? 'A channel keeps its name.' : '2-63 lowercase letters, digits and hyphens, starting with a letter or a digit. Its secret is named notify-{name}.'}
              />
              <SelectField
                label="Kind"
                value={form.kind}
                onChange={(event) => setForm((current) => ({ ...current, kind: event.target.value as Kind }))}
                options={(['email', 'slack', 'webhook'] as const).map((value) => ({ value, label: CHANNEL_KIND_LABELS[value] }))}
                disabled={editing !== null}
                hint={editing ? 'A channel keeps its kind.' : KIND_HINTS[form.kind]}
              />
              <TextField label="Description" value={form.description} onChange={set('description')} autoComplete="off" />
              {form.kind === 'email' && (
                <TextAreaField
                  label="Recipients"
                  value={form.recipients}
                  onChange={set('recipients')}
                  required
                  autoComplete="off"
                  spellCheck={false}
                  rows={3}
                  hint="One address a line, 1-50, each local-part@domain. Recipients are personal data: only an admin reads them."
                />
              )}
              {form.kind === 'webhook' && (
                <TextField label="URL" value={form.url} onChange={set('url')} required autoComplete="off" spellCheck={false} inputMode="url" hint="https://, with no user information. The signing key is set as the channel's secret." />
              )}
              <Checkbox label="Redact names" hint="Its messages name the VM, the plan and the wave by their ids." checked={form.redactNames} onChange={(on) => setForm((current) => ({ ...current, redactNames: on }))} />
              {write.error && busy === null ? <ErrorBanner error={write.error} title={editing ? 'The channel was not saved' : 'The channel was not added'} /> : null}
              <div className="flex flex-wrap gap-2">
                <Button size="lg" variant="primary" type="submit" icon={editing ? Save : Plus} loading={write.isPending && busy === null}>
                  {editing ? 'Save channel' : 'Add channel'}
                </Button>
                {editing && (
                  <Button size="lg" onClick={reset}>
                    Cancel
                  </Button>
                )}
              </div>
            </form>
          </Panel>
        ) : (
          <p role="status" className="card self-start p-4">
            Changing channels requires the admin role.
          </p>
        )}
        <Panel title="Catalog" description="Each channel with its state: the last message's outcome, and the failures in a row." className="[overflow-wrap:anywhere]">
          {channels.isPending && <LoadingBlock label="Loading channels" rows={3} />}
          {channels.error && <ErrorBanner error={channels.error} title="Channels are unavailable" onRetry={() => void channels.refetch()} />}
          {toggleError ? <ErrorBanner error={toggleError} title="The channel was not changed" className="mb-3" /> : null}
          {test.error ? <ErrorBanner error={test.error} title="The test was not sent" className="mb-3" /> : null}
          {channels.data &&
            (channels.data.length ? (
              <ul aria-label="Channel catalog" className="divide-y divide-border">
                {channels.data.map((channel) => {
                  const outcome = tested[channel.name];
                  const running = busy === channel.name;
                  return (
                    <li key={channel.name} aria-label={channel.name} className="flex flex-col gap-1.5 py-3">
                      <div className="flex flex-wrap items-baseline justify-between gap-2">
                        <span className="min-w-0 font-medium">{channel.name}</span>
                        <span className="text-sm text-muted-foreground">{CHANNEL_KIND_LABELS[channel.kind]}</span>
                      </div>
                      {channel.description && <p className="text-sm">{channel.description}</p>}
                      <Audience channel={channel} />
                      <ChannelState channel={channel} />
                      {channel.redact_names && <p className="text-xs text-muted-foreground">Names redacted: its messages carry ids.</p>}
                      {outcome ? (
                        <p role="status" className="flex items-center gap-1.5 text-sm">
                          {outcome.outcome === 'sent' ? <CircleCheck aria-hidden className="size-4 shrink-0 text-status-success" /> : <CircleX aria-hidden className="size-4 shrink-0 text-status-danger" />}
                          {outcome.outcome === 'sent' ? 'Test sent' : `Test failed: ${outcome.reason ?? 'unknown'}`}
                        </p>
                      ) : null}
                      {canManage && (
                        <div className="flex flex-wrap gap-2 pt-1">
                          <Button
                            icon={Pencil}
                            aria-label={`Edit ${channel.name}`}
                            onClick={() => {
                              create.reset();
                              update.reset();
                              setEditing(channel.name);
                              setForm(formOf(channel));
                              requestAnimationFrame(() => nameRef.current?.focus());
                            }}
                          >
                            Edit
                          </Button>
                          <Button icon={channel.enabled ? PowerOff : Power} aria-label={`${channel.enabled ? 'Disable' : 'Enable'} ${channel.name}`} loading={running && update.isPending} onClick={() => toggle(channel)}>
                            {channel.enabled ? 'Disable' : 'Enable'}
                          </Button>
                          {channel.kind !== 'email' && (
                            <Button icon={KeyRound} aria-label={`Set secret for ${channel.name}`} onClick={() => setSecretFor(channel)}>
                              Set secret
                            </Button>
                          )}
                          <Button
                            icon={Send}
                            aria-label={`Send test to ${channel.name}`}
                            loading={running && test.isPending}
                            disabledReason={channel.enabled ? null : 'Enable the channel first.'}
                            onClick={() => sendTest(channel)}
                          >
                            Send test
                          </Button>
                          <Button
                            icon={Trash2}
                            variant="danger"
                            aria-label={`Delete ${channel.name}`}
                            onClick={() => {
                              remove.reset();
                              setDeleting(channel);
                            }}
                          >
                            Delete
                          </Button>
                        </div>
                      )}
                    </li>
                  );
                })}
              </ul>
            ) : (
              <p className="text-sm text-muted-foreground">No channels registered.</p>
            ))}
        </Panel>
        <Panel
          title="Every-plan subscriptions"
          description="Channels that follow every plan, for their topics. An admin adds and removes them; a plan's own subscriptions are on its page."
          className="lg:col-span-2 [overflow-wrap:anywhere]"
        >
          {subscriptions.isPending && <LoadingBlock label="Loading subscriptions" rows={2} />}
          {subscriptions.error && <ErrorBanner error={subscriptions.error} title="Subscriptions are unavailable" onRetry={() => void subscriptions.refetch()} />}
          {subscriptions.data &&
            (everyPlan.length ? (
              <ul aria-label="Every-plan subscriptions" className="divide-y divide-border">
                {everyPlan.map((sub) => (
                  <SubscriptionItem key={sub.id} subscription={sub} kind={kindOf.get(sub.channel)} everyPlanMark={false} removable={canManage} removeReason={null} />
                ))}
              </ul>
            ) : (
              <p className="text-sm text-muted-foreground">No every-plan subscriptions.</p>
            ))}
          {canManage && (
            <div className="mt-4 border-t border-border pt-4">
              <h3 className="mb-3 text-sm font-semibold text-foreground">Add every-plan subscription</h3>
              <SubscriptionForm planId={null} channels={channels.data} channelsError={channels.error} />
            </div>
          )}
        </Panel>
      </div>
      <ConfirmDialog
        open={deleting !== null}
        title={`Delete channel ${deleting?.name ?? ''}?`}
        subject={deleting ? `Channel ${deleting.name}` : undefined}
        consequences={[
          { kind: 'removed', text: 'The channel, its cursor and the secret the control plane stores for it.' },
          { kind: 'kept', text: 'The events already recorded, and every other channel.' },
          { kind: 'then', text: 'Nothing is sent through it. The API refuses while a subscription still names it.' },
        ]}
        confirmLabel="Delete channel"
        tone="danger"
        icon={Trash2}
        pending={remove.isPending}
        error={remove.error}
        onCancel={() => setDeleting(null)}
        onConfirm={() => {
          if (!deleting) return;
          remove.mutate(deleting.name, {
            onSuccess: () => {
              if (editing === deleting.name) reset();
              setTested((current) => {
                const rest = { ...current };
                delete rest[deleting.name];
                return rest;
              });
              setDeleting(null);
            },
          });
        }}
      />
      <SecretDialog key={secretFor?.name ?? ''} channel={secretFor} onClose={() => setSecretFor(null)} />
    </>
  );
}
