import { CircleCheck, Pencil, Plus, Save, Trash2 } from 'lucide-react';
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
import { SectionNav } from '../components/SectionNav';
import { LoadingBlock } from '../components/Skeleton';
import { TechPreviewBadge } from '../components/TechPreviewBadge';
import { HOOK_KIND_LABELS } from '../lib/cutover';
import { usePageTitle } from '../lib/usePageTitle';
import { useDraftGuard } from '../lib/useDraftGuard';
import { useHashLanding } from '../lib/useHashLanding';

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
    secret: '',
    timeout: String(hook.timeout_s),
    revert: hook.revert ?? '',
  };
}

/** The fields the API takes for the form's kind (SDD §4.2, §12): the other kind's stay unset, an empty text is none; the bearer token is sent only when typed - it is write-only, and an empty field keeps the one set. */
function fieldsOf(form: HookForm): HookPatch {
  const text = (value: string) => (value.trim() ? value.trim() : null);
  const timeout = Number(form.timeout);
  return {
    description: text(form.description),
    ...(form.kind === 'playbook' ? { playbook: text(form.playbook) } : { url: text(form.url), ...(form.secret ? { secret: form.secret } : {}) }),
    timeout_s: form.timeout.trim() && Number.isFinite(timeout) ? timeout : 300,
    revert: text(form.revert),
  };
}

/** The hook catalog (SDD §16, §23.3, plan task N12a): every role reads it, an admin keeps it. */
export default function Hooks() {
  usePageTitle('Hooks');
  useHashLanding(true);
  const canManage = useRole() === 'admin';
  const hooks = useHooks();
  const create = useCreateHook();
  const update = useUpdateHook();
  const remove = useDeleteHook();
  const [form, setForm] = useState<HookForm>(EMPTY);
  const [initial, setInitial] = useState<HookForm>(EMPTY);
  const [announcement, setAnnouncement] = useState('');
  const [editing, setEditing] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<Hook | null>(null);
  const nameRef = useRef<HTMLInputElement>(null);
  const catalogRef = useRef<HTMLHeadingElement>(null);
  const deleteReturnRef = useRef<HTMLElement | null>(null);
  const write = editing ? update : create;
  const pending = create.isPending || update.isPending || remove.isPending;
  const waitReason = pending ? 'Wait for the current hook operation to finish.' : null;
  const draft = useDraftGuard(JSON.stringify(form) !== JSON.stringify(initial), pending, reset);
  const set = (key: keyof HookForm) => (event: { target: { value: string } }) => setForm((current) => ({ ...current, [key]: event.target.value }));

  function reset(focus = true) {
    setForm(EMPTY);
    setInitial(EMPTY);
    setEditing(null);
    create.reset();
    update.reset();
    if (focus) requestAnimationFrame(() => nameRef.current?.focus());
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    if (pending) return;
    // Enter can submit from a field: move focus before its fieldset becomes disabled.
    event.currentTarget.querySelector<HTMLButtonElement>('button[type="submit"]')?.focus({ preventScroll: true });
    setAnnouncement('');
    const fields = fieldsOf(form);
    const done = { onSuccess: () => { setAnnouncement(`Hook ${form.name.trim()} ${editing ? 'saved' : 'registered'}.`); reset(); } };
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
      {canManage && (
        <p role="status" className="mb-3 flex items-start gap-2 text-sm text-status-success empty:hidden">
          {announcement && <><CircleCheck aria-hidden className="mt-0.5 size-4 shrink-0" />{announcement}</>}
        </p>
      )}
      <SectionNav label="Hook sections" sections={[['hook-catalog', 'Catalog'], ...(canManage ? [['hook-form', editing ? 'Edit hook' : 'Register hook'] as const] : [])]} />
      <div className="grid min-w-0 gap-4 lg:grid-cols-2">
        {canManage ? (
          <Panel id="hook-form" title={editing ? `Edit hook ${editing}` : 'Register hook'} className="[overflow-wrap:anywhere]">
            <form onSubmit={submit} className="flex flex-col gap-4">
              <fieldset disabled={pending} className="flex min-w-0 flex-col gap-4">
                <legend className="sr-only">Hook details</legend>
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
                  options={(['playbook', 'webhook'] as const).map((value) => ({ value, label: HOOK_KIND_LABELS[value] }))}
                  disabled={editing !== null}
                  hint={editing ? 'A hook keeps its kind.' : form.kind === 'playbook' ? 'The AnsibleExecutor runs the playbook with the context document.' : 'The webhook is POSTed the context document with its bearer token.'}
                />
                <TextField label="Description" value={form.description} onChange={set('description')} autoComplete="off" />
                {form.kind === 'playbook' ? (
                  <TextField label="Playbook path" value={form.playbook} onChange={set('playbook')} required autoComplete="off" spellCheck={false} hint="An absolute path to a .yml or .yaml file." />
                ) : (
                  <>
                    <TextField label="URL" value={form.url} onChange={set('url')} required autoComplete="off" spellCheck={false} inputMode="url" hint="https://, with no user information." />
                    <TextField
                      label="Bearer token"
                      type="password"
                      value={form.secret}
                      onChange={set('secret')}
                      autoComplete="new-password"
                      spellCheck={false}
                      hint={
                        editing && hooks.data?.find((hook) => hook.name === editing)?.secret_set
                          ? 'A token is set. Leave empty to keep it; type a new one to replace it. It is never shown again.'
                          : 'Sent to the URL as Authorization: Bearer. Stored in the secret store and never shown again.'
                      }
                    />
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
              </fieldset>
              {write.error && <ErrorBanner error={write.error} title={editing ? 'The hook was not saved' : 'The hook was not registered'} reveal />}
              <div className="flex flex-wrap gap-2">
                <Button size="lg" variant="primary" type="submit" icon={editing ? Save : Plus} loading={write.isPending} disabledReason={remove.isPending ? waitReason : null}>
                  {editing ? 'Save hook' : 'Register hook'}
                </Button>
                {editing && (
                  <Button size="lg" onClick={draft.close} disabledReason={waitReason}>
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
        <Panel id="hook-catalog" headingRef={catalogRef} title="Catalog" className="[overflow-wrap:anywhere]">
          {hooks.isPending && <LoadingBlock label="Loading hooks" rows={3} />}
          {hooks.error && <ErrorBanner error={hooks.error} title="Hooks are unavailable" onRetry={() => void hooks.refetch()} />}
          {hooks.data &&
            (hooks.data.length ? (
              <ul aria-label="Hook catalog" className="divide-y divide-border">
                {hooks.data.map((hook) => (
                  <li key={hook.name} aria-label={hook.name} className="flex flex-col gap-1 py-3">
                    <div className="flex flex-wrap items-baseline justify-between gap-2">
                      <span className="min-w-0 font-medium">{hook.name}</span>
                      <span className="text-sm text-muted-foreground">{HOOK_KIND_LABELS[hook.kind]}</span>
                    </div>
                    {hook.description && <p className="text-sm">{hook.description}</p>}
                    {hook.kind === 'playbook' ? (
                      <code className="text-sm">{hook.playbook}</code>
                    ) : (
                      <p className="text-sm">
                        {hook.url ? <code>{hook.url}</code> : <span>Receiver {hook.url_host ?? 'unknown'}</span>}{' '}
                        <span className="text-muted-foreground">{hook.secret_set ? 'with a bearer token set' : 'with no bearer token set'}</span>
                      </p>
                    )}
                    <p className="text-sm text-muted-foreground">
                      Timeout {hook.timeout_s} s{hook.revert ? `. Reverted by ${hook.revert}` : ''}
                    </p>
                    {canManage && (
                      <div className="flex flex-wrap gap-2 pt-1">
                        <Button
                          icon={Pencil}
                          disabledReason={waitReason}
                          aria-label={`Edit ${hook.name}`}
                          onClick={() => draft.request(() => {
                            create.reset();
                            update.reset();
                            setEditing(hook.name);
                            const values = formOf(hook);
                            setForm(values);
                            setInitial(values);
                            requestAnimationFrame(() => nameRef.current?.focus());
                          })}
                        >
                          Edit
                        </Button>
                        <Button
                          icon={Trash2}
                          variant="danger"
                          disabledReason={waitReason}
                          aria-label={`Delete ${hook.name}`}
                          onClick={(event) => {
                            deleteReturnRef.current = event.currentTarget;
                            setAnnouncement('');
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
        returnFocusRef={deleteReturnRef}
        onCancel={() => setDeleting(null)}
        onConfirm={() => {
          if (!deleting) return;
          remove.mutate(deleting.name, {
            onSuccess: () => {
              if (editing === deleting.name) reset(false);
              deleteReturnRef.current = catalogRef.current;
              setAnnouncement(`Hook ${deleting.name} deleted.`);
              setDeleting(null);
            },
          });
        }}
      />
      {draft.guard}
    </>
  );
}
