import { Pencil, Plus, Save, Trash2 } from 'lucide-react';
import { useRef, useState, type FormEvent } from 'react';
import { useCreateHook, useDeleteHook, useHooks, useUpdateHook } from '../api/hooks';
import { useRole } from '../api/session';
import type { Hook, HookPatch } from '../api/types';
import { Button } from '../components/Button';
import { ConfirmDialog } from '../components/ConfirmDialog';
import { ErrorBanner } from '../components/ErrorBanner';
import { SelectField, TextField } from '../components/Field';
import { PageHeader } from '../components/PageHeader';
import { Panel } from '../components/Panel';
import { LoadingBlock } from '../components/Skeleton';
import { TechPreviewBadge } from '../components/TechPreviewBadge';
import { usePageTitle } from '../lib/usePageTitle';

const KIND_LABELS: Record<Hook['kind'], string> = { playbook: 'Playbook', webhook: 'Webhook' };

interface HookForm {
  name: string;
  kind: Hook['kind'];
  description: string;
  playbook: string;
  url: string;
  secret: string;
  timeout: string;
  revert: string;
}

const EMPTY: HookForm = { name: '', kind: 'playbook', description: '', playbook: '', url: '', secret: '', timeout: '300', revert: '' };

function formOf(hook: Hook): HookForm {
  return {
    name: hook.name,
    kind: hook.kind,
    description: hook.description ?? '',
    playbook: hook.playbook ?? '',
    url: hook.url ?? '',
    secret: hook.secret ?? '',
    timeout: String(hook.timeout_s),
    revert: hook.revert ?? '',
  };
}

/** The fields the API takes for the form's kind (SDD §4.2, §12): the other kind's stay unset, an empty text is none. */
function fieldsOf(form: HookForm): HookPatch {
  const text = (value: string) => (value.trim() ? value.trim() : null);
  const timeout = Number(form.timeout);
  return {
    description: text(form.description),
    ...(form.kind === 'playbook' ? { playbook: text(form.playbook) } : { url: text(form.url), secret: text(form.secret) }),
    timeout_s: form.timeout.trim() && Number.isFinite(timeout) ? timeout : 300,
    revert: text(form.revert),
  };
}

/** The hook catalog (SDD §16, §23.3, plan task N12a): every role reads it, an admin keeps it. */
export default function Hooks() {
  usePageTitle('Hooks');
  const canManage = useRole() === 'admin';
  const hooks = useHooks();
  const create = useCreateHook();
  const update = useUpdateHook();
  const remove = useDeleteHook();
  const [form, setForm] = useState<HookForm>(EMPTY);
  const [editing, setEditing] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<Hook | null>(null);
  const nameRef = useRef<HTMLInputElement>(null);
  const write = editing ? update : create;
  const set = (key: keyof HookForm) => (event: { target: { value: string } }) => setForm((current) => ({ ...current, [key]: event.target.value }));

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
    const done = { onSuccess: () => { reset(); requestAnimationFrame(() => nameRef.current?.focus()); } };
    if (editing) update.mutate({ name: editing, patch: fields }, done);
    else create.mutate({ name: form.name.trim(), kind: form.kind, ...fields }, done);
  }

  const others = (hooks.data ?? []).filter((hook) => hook.name !== editing);
  return (
    <>
      <PageHeader
        title="Hooks"
        description="Playbooks and webhooks a plan runs at the points of its network cut-over. A hook runs code with the control plane's reach, so only an admin registers one."
        meta={<TechPreviewBadge feature="network" />}
      />
      <div className="grid min-w-0 gap-4 lg:grid-cols-2">
        {canManage ? (
          <Panel title={editing ? `Edit hook ${editing}` : 'Register hook'} className="[overflow-wrap:anywhere]">
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
                hint={editing ? 'A hook keeps its name.' : '2-63 lowercase letters, digits and hyphens, starting with a letter or a digit.'}
              />
              <SelectField
                label="Kind"
                value={form.kind}
                onChange={(event) => setForm((current) => ({ ...current, kind: event.target.value as Hook['kind'] }))}
                options={(['playbook', 'webhook'] as const).map((value) => ({ value, label: KIND_LABELS[value] }))}
                disabled={editing !== null}
                hint={editing ? 'A hook keeps its kind.' : form.kind === 'playbook' ? 'The AnsibleExecutor runs the playbook with the context document.' : 'The webhook is POSTed the context document with its bearer token.'}
              />
              <TextField label="Description" value={form.description} onChange={set('description')} autoComplete="off" />
              {form.kind === 'playbook' ? (
                <TextField label="Playbook path" value={form.playbook} onChange={set('playbook')} required autoComplete="off" spellCheck={false} hint="An absolute path to a .yml or .yaml file." />
              ) : (
                <>
                  <TextField label="URL" value={form.url} onChange={set('url')} required autoComplete="off" spellCheck={false} inputMode="url" hint="https://, with no user information." />
                  <TextField label="Secret" value={form.secret} onChange={set('secret')} required autoComplete="off" spellCheck={false} hint="The name of the secret whose key token is the webhook's bearer token." />
                </>
              )}
              <TextField label="Timeout (s)" value={form.timeout} onChange={set('timeout')} inputMode="numeric" autoComplete="off" hint="1-1800 seconds; 300 by default." />
              <SelectField
                label="Revert"
                value={form.revert}
                onChange={set('revert')}
                options={[{ value: '', label: 'None' }, ...others.map((hook) => ({ value: hook.name, label: hook.name }))]}
                hint="The hook a rollback runs to undo this one."
              />
              {write.error && <ErrorBanner error={write.error} title={editing ? 'The hook was not saved' : 'The hook was not registered'} />}
              <div className="flex flex-wrap gap-2">
                <Button size="lg" variant="primary" type="submit" icon={editing ? Save : Plus} loading={write.isPending}>
                  {editing ? 'Save hook' : 'Register hook'}
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
            Registering hooks requires the admin role.
          </p>
        )}
        <Panel title="Catalog" className="[overflow-wrap:anywhere]">
          {hooks.isPending && <LoadingBlock label="Loading hooks" rows={3} />}
          {hooks.error && <ErrorBanner error={hooks.error} title="Hooks are unavailable" onRetry={() => void hooks.refetch()} />}
          {hooks.data &&
            (hooks.data.length ? (
              <ul aria-label="Hook catalog" className="divide-y divide-border">
                {hooks.data.map((hook) => (
                  <li key={hook.name} aria-label={hook.name} className="flex flex-col gap-1 py-3">
                    <div className="flex flex-wrap items-baseline justify-between gap-2">
                      <span className="min-w-0 font-medium">{hook.name}</span>
                      <span className="text-sm text-muted-foreground">{KIND_LABELS[hook.kind]}</span>
                    </div>
                    {hook.description && <p className="text-sm">{hook.description}</p>}
                    {hook.kind === 'playbook' ? (
                      <code className="text-sm">{hook.playbook}</code>
                    ) : (
                      <p className="text-sm">
                        <code>{hook.url}</code> <span className="text-muted-foreground">with the secret</span> <code>{hook.secret}</code>
                      </p>
                    )}
                    <p className="text-sm text-muted-foreground">
                      Timeout {hook.timeout_s} s{hook.revert ? `. Reverted by ${hook.revert}` : ''}
                    </p>
                    {canManage && (
                      <div className="flex flex-wrap gap-2 pt-1">
                        <Button
                          icon={Pencil}
                          aria-label={`Edit ${hook.name}`}
                          onClick={() => {
                            create.reset();
                            update.reset();
                            setEditing(hook.name);
                            setForm(formOf(hook));
                            requestAnimationFrame(() => nameRef.current?.focus());
                          }}
                        >
                          Edit
                        </Button>
                        <Button
                          icon={Trash2}
                          variant="danger"
                          aria-label={`Delete ${hook.name}`}
                          onClick={() => {
                            remove.reset();
                            setDeleting(hook);
                          }}
                        >
                          Delete
                        </Button>
                      </div>
                    )}
                  </li>
                ))}
              </ul>
            ) : (
              <p className="text-sm text-muted-foreground">No hooks registered.</p>
            ))}
        </Panel>
      </div>
      <ConfirmDialog
        open={deleting !== null}
        title={`Delete hook ${deleting?.name ?? ''}?`}
        subject={deleting ? `Hook ${deleting.name}` : undefined}
        description="Plans can no longer name it. The API refuses while a plan or another hook still names it."
        confirmLabel="Delete hook"
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
              setDeleting(null);
            },
          });
        }}
      />
    </>
  );
}
