import { ArrowRight, Check, HardDrive, KeyRound, Minus, Pencil, Plus, RefreshCw, Server, ShieldAlert, ShieldCheck, Trash2 } from 'lucide-react';
import { useId, useState } from 'react';
import { Link } from 'react-router-dom';
import { useCheckAllProviders, useCheckProvider, useDeleteProvider, useProviders } from '../api/hooks';
import { useRole } from '../api/session';
import type { Provider } from '../api/types';
import { Button } from '../components/Button';
import { ConfirmDialog } from '../components/ConfirmDialog';
import { PlatformMark, ProviderDialog } from '../components/ProviderDialog';
import { EmptyState } from '../components/EmptyState';
import { ErrorBanner } from '../components/ErrorBanner';
import { PageHeader } from '../components/PageHeader';
import { LoadingBlock } from '../components/Skeleton';
import { ProviderStatusBadge } from '../components/StatusBadge';
import { buttonClassName } from '../lib/buttonStyles';
import { cn } from '../lib/cn';
import { formatDateTime, formatRelative } from '../lib/format';
import { hasRole } from '../lib/roles';
import { presetFor } from '../lib/distributions';
import { storageBackends, storageSummary } from '../lib/storage';
import { providerStatusMeta } from '../lib/status';
import { usePageTitle } from '../lib/usePageTitle';

const CAPABILITY_LABELS: Record<string, string> = {
  admin: 'Admin access',
  compute_microversion: 'Compute API',
  ovn: 'OVN networking',
  volume_backends: 'Volume backends',
  storage_backends: 'Storage',
  cbt: 'CBT support',
  version: 'Version',
  datastores: 'Datastores',
};

function Capabilities({ capabilities }: { capabilities: Record<string, unknown> }) {
  // storage_backends (SDD §10) supersedes the plain pool-name list and is shown by driver family
  const hasStorage = Array.isArray(capabilities.storage_backends);
  const entries = Object.entries(capabilities).filter(([key]) => !(hasStorage && key === 'volume_backends'));
  if (entries.length === 0) {
    return <p className="text-sm text-muted-foreground">No capabilities reported yet. Run a check to see them.</p>;
  }
  return (
    <ul aria-label="Capabilities" className="flex flex-wrap gap-1.5">
      {entries.map(([key, value]) => {
        const label = CAPABILITY_LABELS[key] ?? key.replace(/_/g, ' ');
        if (typeof value === 'boolean') {
          const Icon = value ? Check : Minus;
          return (
            <li key={key} className={cn('inline-flex items-center gap-1 rounded-sm border border-border px-1.5 py-0.5 text-xs', value ? 'text-foreground' : 'text-muted-foreground')}>
              <Icon aria-hidden className={cn('size-3.5', value ? 'text-status-success' : 'text-muted-foreground')} />
              {label}
              <span className="sr-only">: {value ? 'yes' : 'no'}</span>
            </li>
          );
        }
        const text =
          key === 'storage_backends'
            ? storageSummary(storageBackends(capabilities)) || 'no pools listed'
            : Array.isArray(value)
              ? value.join(', ')
              : String(value);
        return (
          <li key={key} className="inline-flex max-w-full items-center gap-1 rounded-sm border border-border px-1.5 py-0.5 text-xs text-foreground">
            <span className="text-muted-foreground">{label}</span>
            <span className="num truncate">{text}</span>
          </li>
        );
      })}
    </ul>
  );
}

/** Where the provider signs in from, without ever showing a credential. */
function CredentialState({ provider }: { provider: Provider }) {
  if (provider.credentials_secret && provider.credentials_updated_at) {
    return (
      <span className="inline-flex items-center gap-1">
        <KeyRound aria-hidden className="size-3.5 text-status-success" />
        Stored <time dateTime={provider.credentials_updated_at} title={formatDateTime(provider.credentials_updated_at)}>{formatRelative(provider.credentials_updated_at)}</time>
      </span>
    );
  }
  if (provider.credentials_secret) {
    return (
      <>
        Secret <code className="text-xs">{provider.credentials_secret}</code>
      </>
    );
  }
  if (provider.cloud) {
    return (
      <>
        clouds.yaml <code className="text-xs">{provider.cloud}</code>
      </>
    );
  }
  return <span className="text-status-warning">Not set. Edit the provider to add sign-in details.</span>;
}

function ProviderCard({
  provider,
  canCheck,
  canEdit,
  onEdit,
  onAnnounce,
}: {
  provider: Provider;
  canCheck: boolean;
  canEdit: boolean;
  onEdit: () => void;
  onAnnounce: (message: string) => void;
}) {
  const headingId = useId();
  const check = useCheckProvider();
  const remove = useDeleteProvider();
  const [confirmDelete, setConfirmDelete] = useState(false);
  const ch = provider.conversion_host;
  const preset = presetFor(provider.distribution, provider.kind);

  return (
    <article aria-labelledby={headingId} className="card flex min-w-0 flex-col gap-3 p-4">
      <header className="flex items-start gap-3">
        <PlatformMark preset={preset} />
        <div className="min-w-0 flex-1">
          <h3 id={headingId} className="text-base font-semibold text-foreground">
            {provider.name}
          </h3>
          <p className="text-xs text-muted-foreground">
            {preset.label} <span aria-hidden>/</span> <code>{provider.id}</code>
          </p>
        </div>
        <ProviderStatusBadge status={provider.status} size="md" />
      </header>

      {provider.status_message && (
        <p className={cn('text-sm', provider.status === 'error' ? 'text-status-danger' : 'text-muted-foreground')}>{provider.status_message}</p>
      )}

      <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1.5 text-sm">
        <dt className="text-muted-foreground">Endpoint</dt>
        <dd className="min-w-0">
          <code className="text-xs">{provider.endpoint}</code>
        </dd>
        <dt className="text-muted-foreground">Region</dt>
        <dd>{provider.region ?? '—'}</dd>
        <dt className="text-muted-foreground">Sign-in</dt>
        <dd className="min-w-0 wrap-break-word">
          <CredentialState provider={provider} />
        </dd>
        <dt className="text-muted-foreground">TLS</dt>
        <dd>
          {provider.verify_tls ? (
            <span className="inline-flex items-center gap-1">
              <ShieldCheck aria-hidden className="size-3.5 text-status-success" />
              Verified
            </span>
          ) : (
            <span className="inline-flex items-center gap-1 text-status-warning">
              <ShieldAlert aria-hidden className="size-3.5" />
              Verification off
            </span>
          )}
        </dd>
        <dt className="text-muted-foreground">Conversion host</dt>
        <dd className="min-w-0 wrap-break-word">
          {ch ? (
            <>
              {ch.manage ? 'Deployed by Seamless' : 'Existing host'}
              {ch.name ? <> · {ch.name}</> : null} <span className="num text-muted-foreground">{ch.address ?? ''}</span>
              {!ch.manage && !ch.ssh_key_secret && <span className="block text-xs text-status-warning">No SSH key stored</span>}
            </>
          ) : (
            <span className="text-muted-foreground">Deployed by Seamless when a plan starts</span>
          )}
        </dd>
        <dt className="text-muted-foreground">Last checked</dt>
        <dd>
          {provider.last_checked_at ? (
            <time dateTime={provider.last_checked_at} title={formatDateTime(provider.last_checked_at)}>
              {formatRelative(provider.last_checked_at)}
            </time>
          ) : (
            'Never'
          )}
        </dd>
      </dl>

      <Capabilities capabilities={provider.capabilities} />

      {check.error && <ErrorBanner error={check.error} title="Check failed" />}

      <footer className="mt-auto flex flex-wrap gap-2 pt-1">
        <Button
          icon={RefreshCw}
          loading={check.isPending}
          disabledReason={canCheck ? null : 'Checking a provider requires the operator role.'}
          onClick={() =>
            check.mutate(provider.id, {
              onSuccess: (p) => onAnnounce(`${p.name} checked: ${providerStatusMeta(p.status).label}.`),
            })
          }
        >
          Check
        </Button>
        <Link to={`/inventory/${provider.id}`} className={buttonClassName('ghost', 'md')}>
          <HardDrive aria-hidden className="size-4" />
          Inventory
        </Link>
        <Button variant="ghost" icon={Pencil} onClick={onEdit} disabledReason={canEdit ? null : 'Editing a provider requires the admin role.'}>
          Edit
        </Button>
        {canEdit && (
          <Button variant="ghost" icon={Trash2} onClick={() => setConfirmDelete(true)} className="ml-auto">
            Delete
          </Button>
        )}
      </footer>

      <ConfirmDialog
        open={confirmDelete}
        tone="danger"
        title={`Delete ${provider.name}?`}
        description="This removes the provider from Seamless together with the credentials and SSH key stored for it. The cloud itself is untouched. Plans that still use it must finish first."
        confirmLabel="Delete provider"
        requireText={provider.id}
        pending={remove.isPending}
        error={remove.error}
        onConfirm={() =>
          remove.mutate(provider.id, {
            onSuccess: () => {
              setConfirmDelete(false);
              onAnnounce(`${provider.name} deleted.`);
            },
          })
        }
        onCancel={() => {
          setConfirmDelete(false);
          remove.reset();
        }}
      />
    </article>
  );
}

const SUMMARY_ORDER: Array<Provider['status']> = ['ok', 'degraded', 'error', 'unknown'];

/** One line an operator reads first: how many clouds are healthy, and a way to re-check all. */
function FleetSummary({ providers, canCheck, onAnnounce }: { providers: Provider[]; canCheck: boolean; onAnnounce: (message: string) => void }) {
  const checkAll = useCheckAllProviders();
  const counts = SUMMARY_ORDER.map((status) => ({ status, count: providers.filter((p) => p.status === status).length })).filter((c) => c.count > 0);
  const missing = providers.filter((p) => !p.credentials_secret && !p.cloud).length;
  return (
    <div className="mb-5 flex flex-wrap items-center gap-x-4 gap-y-2 rounded-lg border border-border px-4 py-3">
      <p className="text-sm text-foreground">
        <span className="font-semibold">{providers.length}</span> {providers.length === 1 ? 'provider' : 'providers'}
      </p>
      <ul aria-label="Providers by status" className="flex flex-wrap items-center gap-2">
        {counts.map(({ status, count }) => (
          <li key={status} className="inline-flex items-center gap-1.5 text-sm">
            <ProviderStatusBadge status={status} />
            <span className="num">{count}</span>
          </li>
        ))}
      </ul>
      {missing > 0 && (
        <p className="inline-flex items-center gap-1.5 text-sm text-status-warning">
          <KeyRound aria-hidden className="size-4" />
          {missing} without sign-in details
        </p>
      )}
      <Button
        className="ml-auto"
        icon={RefreshCw}
        loading={checkAll.isPending}
        disabledReason={canCheck ? null : 'Checking providers requires the operator role.'}
        onClick={() =>
          checkAll.mutate(
            providers.map((p) => p.id),
            {
              onSuccess: (checked) => {
                const healthy = checked.filter((p) => p.status === 'ok').length;
                onAnnounce(`Checked ${checked.length} providers: ${healthy} healthy.`);
              },
            },
          )
        }
      >
        Check all
      </Button>
    </div>
  );
}

function ProviderColumn({
  title,
  description,
  items,
  canCheck,
  canEdit,
  onEdit,
  onAnnounce,
}: {
  title: string;
  description: string;
  items: Provider[];
  canCheck: boolean;
  canEdit: boolean;
  onEdit: (provider: Provider) => void;
  onAnnounce: (message: string) => void;
}) {
  const headingId = useId();
  return (
    <section aria-labelledby={headingId} className="flex min-w-0 flex-col gap-3">
      <div>
        <h2 id={headingId} className="text-base font-semibold text-foreground">
          {title}
        </h2>
        <p className="text-sm text-muted-foreground">{description}</p>
      </div>
      {items.length === 0 ? (
        <p className="rounded-lg border border-dashed border-border px-4 py-6 text-center text-sm text-muted-foreground">None connected yet.</p>
      ) : (
        items.map((provider) => (
          <ProviderCard
            key={provider.id}
            provider={provider}
            canCheck={canCheck}
            canEdit={canEdit}
            onEdit={() => onEdit(provider)}
            onAnnounce={onAnnounce}
          />
        ))
      )}
    </section>
  );
}

export default function Providers() {
  usePageTitle('Providers');
  const providers = useProviders();
  const role = useRole();
  const [announcement, setAnnouncement] = useState('');
  const [dialog, setDialog] = useState<{ open: boolean; provider?: Provider }>({ open: false });
  const list = providers.data ?? [];
  const canEdit = hasRole(role, 'admin');
  const canCheck = hasRole(role, 'operator');
  const sources = list.filter((p) => p.role === 'source');
  const destinations = list.filter((p) => p.role === 'destination');
  const openCreate = () => setDialog({ open: true });

  return (
    <>
      <PageHeader
        title="Providers"
        description="Every cloud Seamless migrates between: OpenStack Community, Kolla-Ansible, Red Hat OpenStack 17.1 and VMware vCenter as sources, RHOSO 18.0 as the destination."
        actions={
          <Button variant="primary" icon={Plus} onClick={openCreate} disabledReason={canEdit ? null : 'Adding a provider requires the admin role.'}>
            Add provider
          </Button>
        }
      />
      <p role="status" aria-live="polite" className="sr-only">
        {announcement}
      </p>
      {providers.isPending && <LoadingBlock label="Loading providers…" rows={4} />}
      {providers.error && <ErrorBanner error={providers.error} title="Providers are unavailable" onRetry={() => void providers.refetch()} />}
      {providers.data && list.length === 0 && (
        <EmptyState
          icon={Server}
          title="Connect your first cloud"
          description="Add the source you migrate from and the RHOSO destination you migrate to. Sign-in details go to the secret store, never to the database."
          action={
            <Button variant="primary" icon={Plus} onClick={openCreate} disabledReason={canEdit ? null : 'Adding a provider requires the admin role.'}>
              Add provider
            </Button>
          }
        />
      )}
      {list.length > 0 && <FleetSummary providers={list} canCheck={canCheck} onAnnounce={setAnnouncement} />}
      {list.length > 0 && (
        <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_auto_minmax(0,1fr)]">
          <ProviderColumn
            title="Migrate from"
            description={`${sources.length} source${sources.length === 1 ? '' : 's'}`}
            items={sources}
            canCheck={canCheck}
            canEdit={canEdit}
            onEdit={(provider) => setDialog({ open: true, provider })}
            onAnnounce={setAnnouncement}
          />
          <div aria-hidden className="hidden items-start justify-center pt-12 lg:flex">
            <span className="flex size-9 items-center justify-center rounded-full border border-border text-muted-foreground">
              <ArrowRight className="size-4" />
            </span>
          </div>
          <ProviderColumn
            title="Migrate to"
            description={`${destinations.length} destination${destinations.length === 1 ? '' : 's'}`}
            items={destinations}
            canCheck={canCheck}
            canEdit={canEdit}
            onEdit={(provider) => setDialog({ open: true, provider })}
            onAnnounce={setAnnouncement}
          />
        </div>
      )}
      <ProviderDialog
        open={dialog.open}
        provider={dialog.provider}
        onClose={() => setDialog({ open: false })}
        onSaved={(_, message) => setAnnouncement(message)}
      />
    </>
  );
}
