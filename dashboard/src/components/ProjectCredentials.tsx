import { CircleAlert, CircleCheck, CircleHelp, KeyRound, Plus, RefreshCw, Trash2 } from 'lucide-react';
import { useId, useRef, useState, type FormEvent } from 'react';
import { ApiError } from '../api/client';
import { useCheckProvider, useDeleteProjectCredentials, usePlans, useSetProjectCredentials } from '../api/hooks';
import type { Provider } from '../api/types';
import { cn } from '../lib/cn';
import { formatDateTime, formatRelative } from '../lib/format';
import { EXPIRY_WARNING_MS, ownProjectWords, projectCheckOf, projectCredentialRows, projectWords, type ProjectCredentialRow } from '../lib/projectCredentials';
import { useNow } from '../lib/useNow';
import { deleteRefusal, plansThatNeedProject, providerLabel } from '../lib/providerUse';
import { Button } from './Button';
import { ConfirmDialog } from './ConfirmDialog';
import { ErrorBanner } from './ErrorBanner';
import { TextField } from './Field';
import { Modal } from './Modal';
import { StatusBadge } from './StatusBadge';
import { TechPreviewBadge } from './TechPreviewBadge';

/** A Keystone project's id as the API takes it (SDD §4.2, §12): 1-64 letters, digits, `-` or `_`. */
const PROJECT_ID = /^[A-Za-z0-9_-]{1,64}$/;
const PROJECT_ID_HINT = "The Keystone project's id: 1-64 letters, digits, - or _. A project that already has a credential gets this one instead.";

/** A credential's status, in the card's words (SDD §16): icon, words and tone, never colour alone. */
function CredentialStatus({ row }: { row: ProjectCredentialRow }) {
  if (row.status === 'ok') {
    return (
      <span className="inline-flex flex-wrap items-center gap-1.5">
        <StatusBadge tone="success" icon={CircleCheck} label="Checked" />
        <time className="text-xs text-muted-foreground" dateTime={row.check!.checked_at} title={formatDateTime(row.check!.checked_at)}>
          {formatRelative(row.check!.checked_at)}
        </time>
      </span>
    );
  }
  if (row.status === 'error') {
    return (
      <span className="flex min-w-0 flex-col gap-0.5">
        <StatusBadge tone="danger" icon={CircleAlert} label="Check failed" className="self-start" />
        {row.check?.message && <span className="min-w-0 wrap-break-word text-xs text-status-danger">{row.check.message}</span>}
      </span>
    );
  }
  return <StatusBadge tone="neutral" icon={CircleHelp} label="Not checked since written" />;
}

/** Its expiry as the last check read it (SDD §16, §27.7): warned within 7 days, in the danger tone once passed. */
function Expiry({ expiresAt, now }: { expiresAt: string | null; now: number }) {
  if (expiresAt === null) return <span>Never expires</span>;
  const at = Date.parse(expiresAt);
  if (Number.isNaN(at)) return <span className="text-status-warning">Expiry not read</span>;
  const passed = at <= now;
  const soon = !passed && at - now < EXPIRY_WARNING_MS;
  // the warning in words too, never colour alone (WCAG 1.4.1)
  return (
    <span className={cn(passed ? 'text-status-danger' : soon ? 'text-status-warning' : undefined)}>
      {`${passed ? 'Expired' : 'Expires'} ${formatDateTime(expiresAt)}${soon ? ', within 7 days' : ''}`}
    </span>
  );
}

/** One project credential of the card (SDD §16): its project, status, roles, expiry, restricted flag and write time. */
function CredentialItem({
  row,
  canEdit,
  refusal,
  now,
  onDelete,
}: {
  row: ProjectCredentialRow;
  now: number;
  canEdit: boolean;
  refusal: string | null;
  onDelete: () => void;
}) {
  const check = row.check;
  return (
    <li className="flex min-w-0 flex-col gap-1 border-t border-border pt-2 first:border-t-0 first:pt-0">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <h5 className="min-w-0 wrap-anywhere text-sm font-medium text-foreground">{row.project}</h5>
        <CredentialStatus row={row} />
      </div>
      {check?.status === 'ok' && (
        <div className="flex flex-col gap-0.5 text-xs text-foreground">
          <span>Roles: {check.roles === null ? 'not read' : check.roles.join(', ') || 'none read'}</span>
          <Expiry expiresAt={check.expires_at} now={now} />
          {check.restricted === false ? (
            <span className="text-status-warning">Unrestricted: it may create application credentials and trusts</span>
          ) : check.restricted ? (
            <span>Restricted</span>
          ) : null}
        </div>
      )}
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="inline-flex items-center gap-1 text-xs text-muted-foreground">
          <KeyRound aria-hidden className="size-3.5" />
          Written{' '}
          <time dateTime={row.updatedAt} title={formatDateTime(row.updatedAt)}>
            {formatRelative(row.updatedAt)}
          </time>
        </span>
        {canEdit && (
          <Button variant="ghost" size="sm" icon={Trash2} onClick={onDelete} disabledReason={refusal}>
            Delete <span className="sr-only">the credential of {row.project}</span>
          </Button>
        )}
      </div>
    </li>
  );
}

interface AddOutcome {
  projectId: string;
  provider: Provider | null;
  /** The check that followed the write failed as a request: the credential stays written. */
  checkError: unknown;
}

/**
 * Add project credential (SDD §16, §27.4): the project's id and an application credential's id and secret, written once
 * with PUT /providers/{id}/projects/{project_id}/credentials and never read back, then the provider's check, as the
 * provider dialog does. The secret leaves the form whatever the answer.
 */
function AddProjectCredentialDialog({ provider, open, onClose, onAnnounce }: { provider: Provider; open: boolean; onClose: () => void; onAnnounce: (message: string) => void }) {
  const titleId = useId();
  const id = (key: string) => `project-credential-${provider.id}-${key}`;
  const projectRef = useRef<HTMLInputElement>(null);
  const summaryRef = useRef<HTMLDivElement>(null);
  const outcomeRef = useRef<HTMLHeadingElement>(null);
  const [projectId, setProjectId] = useState('');
  const [appId, setAppId] = useState('');
  const [appSecret, setAppSecret] = useState('');
  const [errors, setErrors] = useState<Partial<Record<'project' | 'appId' | 'appSecret', string>>>({});
  const [failure, setFailure] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [outcome, setOutcome] = useState<AddOutcome | null>(null);
  const write = useSetProjectCredentials();
  const check = useCheckProvider();

  // each opening starts afresh (react.dev, "Adjusting some state when a prop changes")
  const [shownOpen, setShownOpen] = useState(false);
  if (open !== shownOpen) {
    setShownOpen(open);
    if (open) {
      setProjectId('');
      setAppId('');
      setAppSecret('');
      setErrors({});
      setFailure(null);
      setOutcome(null);
    }
  }

  const runCheck = async (pid: string) => {
    try {
      const checked = await check.mutateAsync(provider.id);
      setOutcome({ projectId: pid, provider: checked, checkError: null });
    } catch (error) {
      setOutcome({ projectId: pid, provider: null, checkError: error });
    }
    requestAnimationFrame(() => outcomeRef.current?.focus());
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const pid = projectId.trim();
    const found: typeof errors = {};
    if (!PROJECT_ID.test(pid)) found.project = pid ? "Enter the project's id: 1-64 letters, digits, - or _." : "Enter the project's id.";
    if (!appId.trim()) found.appId = "Enter the application credential's id.";
    if (!appSecret) found.appSecret = "Enter the application credential's secret.";
    setErrors(found);
    setFailure(null);
    if (Object.keys(found).length) {
      requestAnimationFrame(() => summaryRef.current?.focus());
      return;
    }
    const secret = appSecret;
    // the secret leaves the form whatever the answer: it is never kept or shown again (SDD §16, §13.3)
    setAppSecret('');
    setBusy(true);
    try {
      await write.mutateAsync({ id: provider.id, projectId: pid, credentials: { application_credential_id: appId.trim(), application_credential_secret: secret } });
    } catch (error) {
      setFailure(error);
      setBusy(false);
      return;
    }
    setAppId('');
    await runCheck(pid);
    setBusy(false);
  };

  if (outcome) {
    const entry = outcome.provider ? projectCheckOf(outcome.provider, outcome.projectId) : null;
    const project = projectWords(outcome.projectId, entry);
    const ok = entry?.status === 'ok';
    const title = ok ? `${project} is checked` : `${project} was written, but its check did not pass`;
    return (
      <Modal open={open} onClose={onClose} labelledBy={titleId} dismissible={!busy}>
        <div className="flex flex-col gap-4 p-5">
          <div className="flex flex-wrap items-center gap-2">
            <h2 ref={outcomeRef} id={titleId} tabIndex={-1} className="min-w-0 wrap-anywhere text-lg font-semibold text-foreground outline-hidden">
              {title}
            </h2>
            <TechPreviewBadge feature="credentials" />
          </div>
          <p className="text-sm text-muted-foreground">
            Written to the secret store for {providerLabel(provider)}; it is never shown again.
          </p>
          {entry ? (
            <p role="status" className={cn('rounded-md border px-3 py-2 text-sm', ok ? 'border-status-success/40 bg-status-success/10' : 'border-status-warning/50 bg-status-warning/10')}>
              {ok ? `Signed in to ${project}${entry.roles?.length ? ` with ${entry.roles.join(', ')}` : ''}.` : (entry.message ?? 'Its check failed.')}
            </p>
          ) : outcome.checkError ? (
            <ErrorBanner error={outcome.checkError} title="The check could not run" />
          ) : (
            <p role="status" className="text-sm text-status-warning">The check read nothing for this project.</p>
          )}
          <div className="flex flex-wrap justify-end gap-2">
            {!ok && (
              <Button size="lg" variant="secondary" icon={RefreshCw} loading={busy} onClick={() => void (async () => { setBusy(true); await runCheck(outcome.projectId); setBusy(false); })()}>
                Check again
              </Button>
            )}
            <Button
              size="lg"
              variant="primary"
              onClick={() => {
                onAnnounce(`The credential of ${project} at ${providerLabel(provider)} ${ok ? 'is written and checked' : 'is written; its check did not pass'}.`);
                onClose();
              }}
            >
              Done
            </Button>
          </div>
        </div>
      </Modal>
    );
  }

  const entries = Object.entries(errors);
  const fieldOf = { project: 'project', appId: 'app-id', appSecret: 'app-secret' } as const;
  return (
    <Modal open={open} onClose={onClose} labelledBy={titleId} dismissible={!busy} initialFocusRef={projectRef}>
      <form noValidate onSubmit={(e) => void submit(e)} className="flex max-h-[85vh] flex-col">
        <div className="border-b border-border px-5 py-4">
          <div className="flex flex-wrap items-center gap-2">
            <h2 id={titleId} className="text-lg font-semibold text-foreground">
              Add project credential
            </h2>
            <TechPreviewBadge feature="credentials" />
          </div>
          <p className="text-sm text-muted-foreground">
            An application credential of one project of {providerLabel(provider)}: the plans that name the project act in it with this
            credential. Stored in the secret store and never shown again.
          </p>
        </div>
        <div className="flex min-h-0 flex-col gap-4 overflow-y-auto px-5 py-4">
          {entries.length > 0 && (
            <div ref={summaryRef} tabIndex={-1} role="alert" className="rounded-md border border-status-danger/60 bg-status-danger/10 px-3 py-2 text-sm outline-none">
              <p className="flex items-center gap-1.5 font-medium text-foreground">
                <CircleAlert aria-hidden className="size-4 text-status-danger" />
                {entries.length === 1 ? 'One field needs attention' : `${entries.length} fields need attention`}
              </p>
              <ul className="mt-1 list-disc pl-6 text-foreground">
                {entries.map(([key, message]) => (
                  <li key={key}>
                    <a
                      href={`#${id(fieldOf[key as keyof typeof fieldOf])}`}
                      className="inline-block py-1 underline underline-offset-2"
                      onClick={(e) => {
                        e.preventDefault();
                        document.getElementById(id(fieldOf[key as keyof typeof fieldOf]))?.focus();
                      }}
                    >
                      {message}
                    </a>
                  </li>
                ))}
              </ul>
            </div>
          )}
          <TextField
            ref={projectRef}
            id={id('project')}
            label="Project ID"
            required
            value={projectId}
            onChange={(e) => setProjectId(e.target.value)}
            error={errors.project}
            hint={PROJECT_ID_HINT}
            autoComplete="off"
            spellCheck={false}
            inputClassName="font-mono"
          />
          <TextField
            id={id('app-id')}
            label="Application credential ID"
            required
            value={appId}
            onChange={(e) => setAppId(e.target.value)}
            error={errors.appId}
            autoComplete="off"
            spellCheck={false}
            inputClassName="font-mono"
          />
          <TextField
            id={id('app-secret')}
            label="Application credential secret"
            type="password"
            required
            value={appSecret}
            onChange={(e) => setAppSecret(e.target.value)}
            error={errors.appSecret}
            hint="Keystone shows it once, when the credential is created. Seamless writes it to the secret store and never shows it again."
            autoComplete="new-password"
            spellCheck={false}
          />
          {failure ? <ErrorBanner error={failure} title={failure instanceof ApiError ? 'The credential was not written' : 'The credential could not be written'} /> : null}
        </div>
        <div className="flex flex-wrap justify-end gap-2 border-t border-border px-5 py-3">
          <Button size="lg" variant="secondary" onClick={onClose} disabledReason={busy ? 'The credential is being written and checked.' : null}>
            Cancel
          </Button>
          <Button size="lg" variant="primary" type="submit" icon={KeyRound} loading={busy}>
            Save and check
          </Button>
        </div>
      </form>
    </Modal>
  );
}

/**
 * A provider's project credentials on its card (SDD §16, §27.4; plan task TC8): its own credential's project, then each
 * project credential; for an admin Add project credential and each one's Delete - unavailable while a plan names the
 * project -, for any other role the list alone.
 */
export function ProjectCredentials({ provider, canEdit, onAnnounce }: { provider: Provider; canEdit: boolean; onAnnounce: (message: string) => void }) {
  const headingId = useId();
  const headingRef = useRef<HTMLHeadingElement>(null);
  const plans = usePlans();
  const remove = useDeleteProjectCredentials();
  const [adding, setAdding] = useState(false);
  const [deleting, setDeleting] = useState<ProjectCredentialRow | null>(null);
  // an expiry is judged at the browser's clock, read again each minute (SDD §16)
  const now = useNow(60_000);
  const rows = projectCredentialRows(provider);
  const own = ownProjectWords(provider);
  const refusalOf = (projectId: string): string | null =>
    plans.data ? deleteRefusal(plansThatNeedProject(provider.id, projectId, plans.data), 3, 'projects') : 'Reading the plans that may need this project.';

  return (
    <div role="group" aria-labelledby={headingId} className="flex min-w-0 flex-col gap-2 rounded-md border border-border px-3 py-2">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <h4 ref={headingRef} id={headingId} tabIndex={-1} className="text-sm font-medium text-foreground outline-hidden">
          Projects
        </h4>
        <TechPreviewBadge feature="credentials" />
      </div>
      <p className="min-w-0 wrap-anywhere text-xs text-muted-foreground">
        {own ? `Own credential: ${own}` : 'Own credential: not named yet - a check names its project'}
      </p>
      {rows.length === 0 ? (
        <p className="text-sm text-muted-foreground">No project credential yet.</p>
      ) : (
        <ul aria-label="Project credentials" className="flex flex-col gap-2">
          {rows.map((row) => (
            <CredentialItem key={row.projectId} row={row} canEdit={canEdit} refusal={refusalOf(row.projectId)} now={now} onDelete={() => setDeleting(row)} />
          ))}
        </ul>
      )}
      {canEdit ? (
        <Button variant="secondary" size="sm" icon={Plus} className="self-start" onClick={() => setAdding(true)}>
          Add project credential <span className="sr-only">to {providerLabel(provider)}</span>
        </Button>
      ) : (
        <p className="text-xs text-muted-foreground">Adding and deleting project credentials requires the admin role.</p>
      )}
      {canEdit && <AddProjectCredentialDialog provider={provider} open={adding} onClose={() => setAdding(false)} onAnnounce={onAnnounce} />}
      <ConfirmDialog
        open={deleting !== null}
        tone="danger"
        icon={Trash2}
        title={`Delete the credential of ${deleting?.project ?? ''}?`}
        subject={`Provider ${provider.id}`}
        consequences={[
          { kind: 'removed', text: "the project's application credential, from the secret store" },
          { kind: 'kept', text: 'the project and the credential in the cloud: nothing changes there' },
          { kind: 'then', text: 'no plan can act in the project until an admin writes a new one' },
        ]}
        confirmLabel="Delete credential"
        requireText={deleting?.projectId}
        pending={remove.isPending}
        error={remove.error}
        returnFocusRef={headingRef}
        onConfirm={() => {
          if (!deleting) return;
          const gone = deleting;
          remove.mutate(
            { id: provider.id, projectId: gone.projectId },
            {
              onSuccess: () => {
                setDeleting(null);
                onAnnounce(`The credential of ${gone.project} at ${providerLabel(provider)} deleted.`);
              },
            },
          );
        }}
        onCancel={() => {
          setDeleting(null);
          remove.reset();
        }}
      />
    </div>
  );
}
