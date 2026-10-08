import { Check, HardDrive, Minus, RefreshCw, Server, ShieldAlert, ShieldCheck, Trash2 } from 'lucide-react';
import { useId, useState } from 'react';
import { Link } from 'react-router-dom';
import { useCheckProvider, useDeleteProvider, useProviders } from '../api/hooks';
import { useRole } from '../api/session';
import type { Provider } from '../api/types';
import { Button } from '../components/Button';
import { ConfirmDialog } from '../components/ConfirmDialog';
import { EmptyState } from '../components/EmptyState';
import { ErrorBanner } from '../components/ErrorBanner';
import { PageHeader } from '../components/PageHeader';
import { LoadingBlock } from '../components/Skeleton';
import { ProviderStatusBadge } from '../components/StatusBadge';
import { buttonClassName } from '../lib/buttonStyles';
import { cn } from '../lib/cn';
import { formatDateTime, formatRelative } from '../lib/format';
import { hasRole } from '../lib/roles';
import { PROVIDER_KIND_LABELS, providerStatusMeta } from '../lib/status';
import { usePageTitle } from '../lib/usePageTitle';

const CAPABILITY_LABELS: Record<string, string> = {
  admin: 'Admin access',
  compute_microversion: 'Compute API',
  ovn: 'OVN networking',
  volume_backends: 'Volume backends',
  cbt: 'CBT support',
  version: 'Version',
  datastores: 'Datastores',
};

function Capabilities({ capabilities }: { capabilities: Record<string, unknown> }) {
  const entries = Object.entries(capabilities);
  if (entries.length === 0) {
    return <p className="text-sm text-muted-foreground">No capabilities reported yet — run a check.</p>;
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
        const text = Array.isArray(value) ? value.join(', ') : String(value);
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

function ProviderCard({
  provider,
  canCheck,
  canDelete,
  onAnnounce,
}: {
  provider: Provider;
  canCheck: boolean;
  canDelete: boolean;
  onAnnounce: (message: string) => void;
}) {
  const headingId = useId();
  const check = useCheckProvider();
  const remove = useDeleteProvider();
  const [confirmDelete, setConfirmDelete] = useState(false);
  const ch = provider.conversion_host;

  return (
    <article aria-labelledby={headingId} className="card flex min-w-0 flex-col gap-3 p-4">
      <header className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <h3 id={headingId} className="text-base font-semibold text-foreground">
            {provider.name}
          </h3>
          <p className="text-xs text-muted-foreground">
            <code>{provider.id}</code> · {PROVIDER_KIND_LABELS[provider.kind] ?? provider.kind} · {provider.role === 'source' ? 'Source' : 'Destination'}
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
        <dt className="text-muted-foreground">Credentials</dt>
        <dd className="min-w-0 wrap-break-word">
          {provider.cloud ? (
            <>
              clouds.yaml <code className="text-xs">{provider.cloud}</code>
            </>
          ) : provider.credentials_secret ? (
            <>
              secret <code className="text-xs">{provider.credentials_secret}</code>
            </>
          ) : (
            '—'
          )}
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
              {ch.name ?? 'managed'} <span className="num text-muted-foreground">{ch.address ?? ''}</span>
            </>
          ) : (
            <span className="text-muted-foreground">None configured</span>
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
        {canDelete && (
          <Button variant="ghost" icon={Trash2} onClick={() => setConfirmDelete(true)} className="ml-auto">
            Delete
          </Button>
        )}
      </footer>

      <ConfirmDialog
        open={confirmDelete}
        tone="danger"
        title={`Delete ${provider.name}?`}
        description="This removes the provider registration from Seamless only; the cloud and its credentials are untouched. Plans that still reference it must finish first."
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

export default function Providers() {
  usePageTitle('Providers');
  const providers = useProviders();
  const role = useRole();
  const [announcement, setAnnouncement] = useState('');
  const list = providers.data ?? [];
  const groups: Array<{ title: string; items: Provider[] }> = [
    { title: 'Sources', items: list.filter((p) => p.role === 'source') },
    { title: 'Destinations', items: list.filter((p) => p.role === 'destination') },
  ];

  return (
    <>
      <PageHeader
        title="Providers"
        description="Source clouds (RHOSP 17.1, community OpenStack, VMware vSphere) and the RHOSO 18.0 destination. Credentials stay in clouds.yaml or mounted secrets and are never shown here."
      />
      <p role="status" aria-live="polite" className="sr-only">
        {announcement}
      </p>
      {providers.isPending && <LoadingBlock label="Loading providers…" rows={4} />}
      {providers.error && <ErrorBanner error={providers.error} title="Providers are unavailable" onRetry={() => void providers.refetch()} />}
      {providers.data && list.length === 0 && (
        <EmptyState icon={Server} title="No providers registered" description="Register providers through the API or CLI (POST /api/v1/providers, admin role)." />
      )}
      <div className="flex flex-col gap-6">
        {groups
          .filter((g) => g.items.length > 0)
          .map((group) => (
            <section key={group.title} aria-label={group.title}>
              <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-muted-foreground">{group.title}</h2>
              <div className="grid gap-4 md:grid-cols-2 2xl:grid-cols-3">
                {group.items.map((provider) => (
                  <ProviderCard
                    key={provider.id}
                    provider={provider}
                    canCheck={hasRole(role, 'operator')}
                    canDelete={hasRole(role, 'admin')}
                    onAnnounce={setAnnouncement}
                  />
                ))}
              </div>
            </section>
          ))}
      </div>
    </>
  );
}
