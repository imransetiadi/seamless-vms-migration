import { ApiError } from '../api/client';
import { UnsavedChangesGuard } from '../components/UnsavedChangesGuard';
import { Copy, Trash2, UserPlus } from 'lucide-react';
import { useEffect, useRef, useState, type FormEvent } from 'react';
import { useCreateUser, useDeleteUser, useHealth, useMe, useUsers } from '../api/hooks';
import { useRole } from '../api/session';
import { ROLES, type Role } from '../api/types';
import { Checkbox } from '../components/Checkbox';
import { Button } from '../components/Button';
import { ConfirmDialog } from '../components/ConfirmDialog';
import { ErrorBanner } from '../components/ErrorBanner';
import { SelectField, TextAreaField, TextField } from '../components/Field';
import { PageHeader } from '../components/PageHeader';
import { Panel } from '../components/Panel';
import { LoadingBlock } from '../components/Skeleton';
import { ROLE_LABELS } from '../lib/roles';
import { usePageTitle } from '../lib/usePageTitle';
import { useFocusAfterRender } from '../lib/useFocusAfterRender';

const ROLE_HELP: Record<Role, string> = {
  viewer: 'View dashboard data without changing it.',
  operator: 'Viewer access, plus create plans and run migrations.',
  approver: 'Operator access, plus approve cutovers and finalize migrations.',
  admin: 'Full access, including providers, advisor policies, approvals and dashboard users.',
};

export default function Users() {
  usePageTitle('Users');
  const canManage = useRole() === 'admin';
  const health = useHealth();
  const users = useUsers(canManage);
  const create = useCreateUser();
  const remove = useDeleteUser();
  const me = useMe();
  const [removing, setRemoving] = useState<{ name: string; role: Role } | null>(null);
  const [removed, setRemoved] = useState('');
  const removeReturnRef = useRef<HTMLElement | null>(null);
  const listHeadingRef = useRef<HTMLHeadingElement>(null);
  const [name, setName] = useState('');
  const [role, setRole] = useState<Role>('viewer');
  const [copyStatus, setCopyStatus] = useState('');
  const [acknowledged, setAcknowledged] = useState(false);
  const [nameError, setNameError] = useState<string | null>(null);
  const nameRef = useRef<HTMLInputElement>(null);
  const focusAfterRender = useFocusAfterRender();
  const tokenId = 'new-user-token';
  const created = create.data;
  const unsavedToken = Boolean(created) && !acknowledged;
  const dirtyDraft = !created && (name !== '' || role !== 'viewer');
  const disabledReason = !canManage
    ? 'Adding users requires the admin role.'
    : health.data?.auth_disabled === false
      ? null
      : health.data?.auth_disabled
        ? 'Enable authentication on the control plane before adding users.'
        : 'Wait until authentication status is available.';

  useEffect(() => {
    if (created) document.getElementById(tokenId)?.focus();
  }, [created]);

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (disabledReason || create.isPending || created) return;
    event.currentTarget.querySelector<HTMLButtonElement>('button[type="submit"]')?.focus({ preventScroll: true });
    setCopyStatus('');
    setNameError(null);
    setAcknowledged(false);
    create.mutate({ name: name.trim(), role }, {
      onError: (error) => {
        if (error instanceof ApiError && (error.status === 409 || error.status === 422)) {
          setNameError(error.message);
          focusAfterRender(() => nameRef.current);
        }
      },
    });
  }

  return (
    <>
      <UnsavedChangesGuard
        dirty={create.isPending || dirtyDraft || unsavedToken}
        pending={create.isPending}
        title={unsavedToken ? 'Leave without saving this token?' : undefined}
        description={unsavedToken ? 'This token cannot be shown again. To replace a lost token, remove this user and add them again.' : undefined}
        confirmLabel={unsavedToken ? 'Leave and lose token' : undefined}
      />
      <PageHeader title="Users" description="Give each person their own dashboard access and role. Their name identifies their actions in Events." />
      {!canManage ? (
        <p role="status" className="card p-4">Managing dashboard users requires the admin role.</p>
      ) : (
        <div className="grid min-w-0 gap-4 lg:grid-cols-2">
          <Panel title={created ? `User ${created.name} created` : 'Add user'} className="[overflow-wrap:anywhere]">
            {created ? (
              <div className="flex flex-col gap-4">
                <p role="status" className="text-sm">Save this token now. It cannot be shown again after leaving this page or selecting Done.</p>
                <p className="text-sm text-muted-foreground">Share it securely with {created.name}. They can paste it into Sign in for {ROLE_LABELS[created.role]} access.</p>
                <TextAreaField id={tokenId} label="New user API token" value={created.token} readOnly autoComplete="off" spellCheck={false} onFocus={(event) => event.currentTarget.select()} />
                <div className="flex flex-wrap gap-2">
                  <Button size="lg" icon={Copy} onClick={async () => {
                    try {
                      await navigator.clipboard.writeText(created.token);
                      setCopyStatus('Token copied.');
                    } catch {
                      setCopyStatus('Copy failed. Select the token and copy it manually.');
                    }
                  }}>Copy token</Button>
                  <Checkbox label="I have saved this token securely" checked={acknowledged} onChange={setAcknowledged} />
                  <Button size="lg" variant="primary" disabledReason={acknowledged ? null : 'Confirm that you saved the token first.'} onClick={() => {
                    create.reset();
                    setName('');
                    setRole('viewer');
                    setCopyStatus('');
                    focusAfterRender(() => nameRef.current);
                  }}>Done</Button>
                </div>
                <p role="status" className="text-sm">{copyStatus}</p>
              </div>
            ) : (
              <form onSubmit={submit} className="flex flex-col gap-4">
                {health.error && <ErrorBanner error={health.error} title="Authentication status is unavailable" onRetry={() => void health.refetch()} />}
                <TextField ref={nameRef} label="User name" value={name} onChange={(event) => { setName(event.target.value); setNameError(null); }} error={nameError} maxLength={128} pattern="[A-Za-z0-9][A-Za-z0-9_.@\-]*" required autoComplete="off" disabled={create.isPending} hint="1-128 letters, digits, dots, underscores, @ or hyphens. Start with a letter or digit; names must be unique, ignoring case." />
                <SelectField label="Role" value={role} onChange={(event) => setRole(event.target.value as Role)} options={ROLES.map((value) => ({ value, label: ROLE_LABELS[value] }))} hint={ROLE_HELP[role]} disabled={create.isPending} />
                <p className="text-sm text-muted-foreground">Creates a login token. Store it securely before closing this page.</p>
                {create.error && <ErrorBanner error={create.error} title="User creation could not be confirmed" />}
                {disabledReason && <p className="text-sm text-muted-foreground">{disabledReason}</p>}
                <Button size="lg" variant="primary" icon={UserPlus} type="submit" loading={create.isPending} disabledReason={disabledReason}>Add user</Button>
              </form>
            )}
          </Panel>
          <Panel title="Dashboard users" headingRef={listHeadingRef} description="Includes users configured by the server administrator.">
            {users.isPending && <LoadingBlock label="Loading users" rows={3} />}
            {users.error && <ErrorBanner error={users.error} title="Users are unavailable" onRetry={() => void users.refetch()} />}
            {users.data && (users.data.length ? (
              <ul aria-label="Dashboard users" className="divide-y divide-border">
                {users.data.map((user, index) => {
                  const own = me.data?.name?.toLowerCase() === user.name.toLowerCase();
                  return (
                    <li key={`${user.name}-${index}`} aria-label={user.name} className="flex flex-wrap items-center justify-between gap-2 py-3">
                      <span className="min-w-0 font-medium [overflow-wrap:anywhere]">{user.name}</span>
                      <span className="flex items-center gap-3">
                        <span className="text-sm text-muted-foreground">{ROLE_LABELS[user.role]}</span>
                        <Button
                          icon={Trash2}
                          variant="danger"
                          aria-label={`Remove ${user.name}`}
                          disabledReason={own ? 'You cannot remove the user you are signed in as.' : remove.isPending ? 'Wait for the current removal to finish.' : null}
                          onClick={(event) => {
                            removeReturnRef.current = event.currentTarget;
                            setRemoved('');
                            remove.reset();
                            setRemoving(user);
                          }}
                        >
                          Remove
                        </Button>
                      </span>
                    </li>
                  );
                })}
              </ul>
            ) : <p className="text-sm text-muted-foreground">No dashboard users configured.</p>)}
            <p role="status" className="text-sm text-status-success empty:sr-only">{removed}</p>
          </Panel>
        </div>
      )}
      <ConfirmDialog
        open={removing !== null}
        title={`Remove user ${removing?.name ?? ''}?`}
        subject={removing ? `User ${removing.name} (${ROLE_LABELS[removing.role]})` : undefined}
        description="Their token stops working at once. To give them access again, add the user and share the new token. Accounts managed in the server’s tokens file must be removed there."
        confirmLabel="Remove user"
        tone="danger"
        icon={Trash2}
        pending={remove.isPending}
        error={remove.error}
        returnFocusRef={removeReturnRef}
        onCancel={() => setRemoving(null)}
        onConfirm={() => {
          if (!removing) return;
          remove.mutate(removing.name, {
            onSuccess: () => {
              removeReturnRef.current = listHeadingRef.current;
              setRemoved(`User ${removing.name} removed.`);
              setRemoving(null);
            },
          });
        }}
      />
    </>
  );
}
