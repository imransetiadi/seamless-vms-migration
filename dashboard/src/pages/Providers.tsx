import { ArrowRight, Check, HardDrive, KeyRound, Minus, Pencil, Plus, RefreshCw, Server, ShieldAlert, ShieldCheck, Trash2, TriangleAlert } from 'lucide-react';
import { useEffect, useId, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { useCheckAllProviders, useCheckProvider, useDeleteProvider, useMe, usePlans, useProviders } from '../api/hooks';
import { useRole } from '../api/session';
import type { HostKeyPin, Provider } from '../api/types';
import { Button } from '../components/Button';
import { ConfirmDialog } from '../components/ConfirmDialog';
import { PlatformMark, ProviderDialog } from '../components/ProviderDialog';
import { EmptyState } from '../components/EmptyState';
import { ErrorBanner } from '../components/ErrorBanner';
import { PageHeader } from '../components/PageHeader';
import { LoadingBlock } from '../components/Skeleton';
import { ProviderStatusBadge } from '../components/StatusBadge';
import { TechPreviewBadge } from '../components/TechPreviewBadge';
import { buttonClassName } from '../lib/buttonStyles';
import { cn } from '../lib/cn';
import { formatDateTime, formatRelative, namesText } from '../lib/format';
import { checkedGpuCapacity, gpuCapacityReason, poolFreeText, poolLabel } from '../lib/gpu';
import { useFingerprints } from '../lib/hostKeys';
import { deleteRefusal, plansThatNeed, providerLabel } from '../lib/providerUse';
import { hasRole } from '../lib/roles';
import { presetFor } from '../lib/distributions';
import { policyProfileOf } from '../lib/networks';
import { DESTINATION_PLATFORMS, plainHttpText, profileText, rangeText, releaseRange, seriesText, tierText, type SupportTier } from '../lib/platforms';
import { storageBackends, storageSummary } from '../lib/storage';
import { providerStatusMeta } from '../lib/status';
import { usePageTitle } from '../lib/usePageTitle';

const CAPABILITY_LABELS: Record<string, string> = {
  admin: 'Admin access',
  roles: 'Token roles',
  compute_microversion: 'Compute API',
  block_storage_microversion: 'Block storage API',
  ovn: 'OVN networking',
  volume_backends: 'Volume backends',
  storage_backends: 'Storage',
  // a vCenter's check (SDD §10): its version and its VMs counted
  api_version: 'vSphere API',
  version: 'Version',
  vm_count: 'VMs',
  cbt_enabled_vms: 'VMs with CBT',
  vms_with_snapshots: 'VMs with snapshots',
  vms_with_independent_disks: 'VMs with independent disks',
  vms_without_tools: 'VMs without VMware Tools',
};

/** The check-report keys the card shows in its own rows: a destination's series, tier, profile and GPU capacity, and TLS (SDD §10, §16). */
const SHOWN_IN_ROWS = new Set(['release_range', 'release', 'support_tier', 'policy_profile', 'gpu_capacity', 'tls', 'plain_http']);

function Capabilities({ capabilities }: { capabilities: Record<string, unknown> }) {
  // storage_backends (SDD §10) supersedes the plain pool-name list and is shown by driver family; a value the card
  // cannot say in words - an object, or none - is left out, never printed as "[object Object]" or "null" (RV18-08)
  const hasStorage = Array.isArray(capabilities.storage_backends);
  const entries = Object.entries(capabilities).filter(
    ([key, value]) =>
      !(hasStorage && key === 'volume_backends') &&
      !SHOWN_IN_ROWS.has(key) &&
      value !== null &&
      value !== undefined &&
      (key === 'storage_backends' || Array.isArray(value) || typeof value !== 'object'),
  );
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
        // a maximum the check could not read is "" (SDD §10) and an empty list lists nothing: said so, never a bare label (RV19-03)
        const text =
          key === 'storage_backends'
            ? storageSummary(storageBackends(capabilities)) || 'no pools listed'
            : Array.isArray(value)
              ? value.join(', ') || 'none'
              : String(value) || 'not read';
        return (
          <li key={key} className="inline-flex max-w-full flex-wrap items-baseline gap-1 rounded-sm border border-border px-1.5 py-0.5 text-xs text-foreground">
            <span className="text-muted-foreground">{label}</span>
            {/* wraps on a narrow screen: never cut off with an ellipsis (SDD §16) */}
            <span className="num min-w-0 wrap-break-word">{text}</span>
          </li>
        );
      })}
    </ul>
  );
}

/** A source's Ceph access (SDD §16, §28.4, plan task CE10): what it declares, whether its key is stored, and the check's rbd pools of another cluster. */
function CephAccessSummary({ provider }: { provider: Provider }) {
  const headingId = useId();
  const ceph = provider.ceph;
  if (!ceph) return null;
  const elsewhere = storageBackends(provider.capabilities).filter((b) => b.family === 'rbd' && b.cephFsid !== null && b.cephFsid !== ceph.fsid);
  return (
    <div role="group" aria-labelledby={headingId} className="flex min-w-0 flex-col gap-1.5 rounded-md border border-border px-3 py-2">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <h4 id={headingId} className="text-sm font-medium text-foreground">
          Ceph access
        </h4>
        <TechPreviewBadge feature="extents" />
      </div>
      <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1 text-xs">
        <dt className="text-muted-foreground">Cluster</dt>
        <dd className="min-w-0 wrap-break-word">
          <code>{ceph.fsid}</code>
        </dd>
        <dt className="text-muted-foreground">Monitors</dt>
        <dd className="num min-w-0 wrap-break-word">{ceph.monitors.join(', ')}</dd>
        <dt className="text-muted-foreground">User</dt>
        <dd>
          <code>{ceph.client}</code>
        </dd>
        <dt className="text-muted-foreground">Key</dt>
        <dd>
          {ceph.key_updated_at ? (
            <span className="inline-flex items-center gap-1">
              <KeyRound aria-hidden className="size-3.5 text-status-success" />
              Key stored{' '}
              <time dateTime={ceph.key_updated_at} title={formatDateTime(ceph.key_updated_at)}>
                {formatRelative(ceph.key_updated_at)}
              </time>
            </span>
          ) : (
            <span className="text-status-warning">No key stored</span>
          )}
        </dd>
      </dl>
      {elsewhere.length > 0 && <RowWarning>{`On another cluster, untracked: ${elsewhere.map((b) => b.pool).join(', ')}`}</RowWarning>}
    </div>
  );
}

/** A warning under a card row, in the row's words (SDD §16). */
function RowWarning({ children }: { children: string }) {
  return (
    <span className="mt-0.5 flex items-start gap-1 text-xs text-status-warning">
      <TriangleAlert aria-hidden className="mt-0.5 size-3.5 shrink-0" />
      <span>{children}</span>
    </span>
  );
}

/** The support tier the check reports, when it reports one (since 0.2.0, SDD §10.1). */
function reportedTier(capabilities: Record<string, unknown>): SupportTier | null {
  const tier = capabilities.support_tier;
  return tier && typeof tier === 'object' && typeof (tier as SupportTier).tier === 'string' ? (tier as SupportTier) : null;
}

/**
 * A destination's series, support tier and policy profile, as its check reports them (SDD §10.1, §16): the declared
 * series, else the detected range, with codenames; the tier in words; and a warning when the APIs contradict the
 * declared series or the profile is unknown.
 */
function DestinationRows({ provider }: { provider: Provider }) {
  const caps = provider.capabilities;
  const tier = reportedTier(caps);
  if (!tier) {
    return (
      <>
        <dt className="text-muted-foreground">Series</dt>
        <dd>
          {provider.release ? (
            <>
              {seriesText(provider.release)} <span className="text-xs text-muted-foreground">declared, not checked yet</span>
            </>
          ) : (
            <span className="text-muted-foreground">Run a check to detect it</span>
          )}
        </dd>
      </>
    );
  }
  const settled = typeof caps.release === 'string' ? caps.release : null;
  const range = Array.isArray(caps.release_range) ? caps.release_range.map(String) : [];
  const detected = settled === null ? rangeText(range) : null;
  // an empty range says why, by the maxima the check read: older or newer than the window, or nothing read (RV19-04);
  // RHOSO 18.0's tier is Validated by its kind, so the tier's reason cannot say it
  const outside = range.length ? null : releaseRange(caps.compute_microversion, caps.block_storage_microversion).outside;
  const profile = policyProfileOf(caps, provider);
  const warn = tier.tier === 'tier3' || tier.tier === 'outside';
  return (
    <>
      <dt className="text-muted-foreground">Series</dt>
      <dd className="min-w-0 wrap-break-word">
        {settled !== null ? (
          <>
            {seriesText(settled)} <span className="text-xs text-muted-foreground">{settled === provider.release ? 'declared' : 'detected'}</span>
          </>
        ) : detected !== null ? (
          <>
            {detected} <span className="text-xs text-muted-foreground">detected</span>
          </>
        ) : (
          <span className="text-muted-foreground">{outside === 'older' ? 'Older than the window' : outside === 'newer' ? 'Newer than the window' : 'Not detected'}</span>
        )}
        {tier.declared_outside_range && provider.release && (
          <RowWarning>{`Declared ${seriesText(provider.release)}, which its APIs contradict: correct the declared series, or clear it so the check detects the series.`}</RowWarning>
        )}
      </dd>
      <dt className="text-muted-foreground">Support tier</dt>
      <dd className={cn(warn && 'text-status-warning')}>{tierText(tier)}</dd>
      <dt className="text-muted-foreground">Policy profile</dt>
      <dd className="min-w-0 wrap-break-word">
        {profile === 'unknown' ? (
          <>
            <span className="text-status-warning">{profileText(profile)}.</span>{' '}
            {/* both ways out, as the API's refusals: a check is allowed while a plan runs, a PATCH that declares is not
                (SDD §16, §21.5, review RV20-09) */}
            <RowWarning>
              Check the provider so its series is detected, or declare its policy profile or series: until then only the admin
              role may put a port on another project&apos;s network.
            </RowWarning>
          </>
        ) : (
          profileText(profile)
        )}
      </dd>
    </>
  );
}

/**
 * A destination's GPU capacity as its check read it, with the check's time (SDD §16, §22.3): each pool as the Inventory
 * words it, by what it asks Placement for - a check report holds no flavor to name it by a declared profile -, or why it
 * was not read; nothing while the check read no pool or the report holds none.
 */
function GpuCapacitySummary({ provider }: { provider: Provider }) {
  const headingId = useId();
  const capacity = checkedGpuCapacity(provider.capabilities);
  if (!capacity || (capacity.status === 'ok' && capacity.pools.length === 0)) return null;
  return (
    <div role="group" aria-labelledby={headingId} className="flex min-w-0 flex-col gap-1.5 rounded-md border border-border px-3 py-2">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <h4 id={headingId} className="text-sm font-medium text-foreground">
          GPU capacity
        </h4>
        <TechPreviewBadge />
      </div>
      {capacity.status === 'ok' ? (
        <ul className="flex flex-col gap-0.5 text-sm text-foreground">
          {capacity.pools.map((pool) => (
            <li key={JSON.stringify([pool.resource_class, pool.traits, pool.forbidden_traits, pool.same_provider])} className="min-w-0 wrap-break-word">
              <span className="font-mono text-xs">{poolLabel(pool)}</span>: {poolFreeText(pool)}
            </li>
          ))}
        </ul>
      ) : (
        // nothing read is never shown as free units (SDD §16): the reason, in the Inventory's words
        <p className="flex items-start gap-1.5 text-sm text-status-warning">
          <TriangleAlert aria-hidden className="mt-0.5 size-4 shrink-0" />
          <span className="min-w-0 wrap-break-word">Not read: {gpuCapacityReason(capacity)}.</span>
        </p>
      )}
      {capacity.read_at && (
        <p className="text-xs text-muted-foreground">
          Checked{' '}
          <time dateTime={capacity.read_at} title={formatDateTime(capacity.read_at)}>
            {formatRelative(capacity.read_at)}
          </time>
        </p>
      )}
    </div>
  );
}

/** Where a pin's keys came from, in words (SDD §16, §26.3). */
const PIN_SOURCE: Record<HostKeyPin['source'], string> = {
  injected: 'Injected at creation',
  console: 'Read from the console',
};

/** Each key's fingerprint with what says where it came from, once the browser computed them (SDD §16). */
function Fingerprints({ keys, origin }: { keys: readonly string[]; origin: string }) {
  const prints = useFingerprints(keys);
  if (!prints) return null;
  return (
    <ul className="flex flex-col gap-0.5 text-sm text-foreground">
      {prints.map((print, i) => (
        <li key={`${i}:${keys[i]}`} className="min-w-0 wrap-break-word">
          {print ? <code className="text-xs">{print}</code> : <span className="text-status-warning">A key it cannot read</span>}{' '}
          <span className="text-muted-foreground">{origin}</span>
        </li>
      ))}
    </ul>
  );
}

/**
 * The host keys of an OpenStack or RHOSO provider's conversion host (SDD §16, §26.4; plan task ST7): the declared keys'
 * fingerprints, which win over the pin; else the pin's, with its source, its server and its time; else that the next
 * check pins them from the host's console.
 */
function HostKeys({ provider }: { provider: Provider }) {
  const headingId = useId();
  const declared = provider.conversion_host?.ssh_host_keys ?? [];
  const pin = provider.conversion_host_pin ?? null;
  return (
    <div role="group" aria-labelledby={headingId} className="flex min-w-0 flex-col gap-1.5 rounded-md border border-border px-3 py-2">
      <h4 id={headingId} className="text-sm font-medium text-foreground">
        Host keys
      </h4>
      {declared.length > 0 ? (
        <Fingerprints keys={declared} origin="Declared" />
      ) : pin ? (
        <>
          <Fingerprints keys={pin.keys} origin={PIN_SOURCE[pin.source]} />
          <p className="min-w-0 wrap-break-word text-xs text-muted-foreground">
            Server <code>{pin.server_id}</code>
          </p>
          <p className="text-xs text-muted-foreground">
            Pinned{' '}
            <time dateTime={pin.pinned_at} title={formatDateTime(pin.pinned_at)}>
              {formatRelative(pin.pinned_at)}
            </time>
          </p>
        </>
      ) : (
        <p className="text-sm text-muted-foreground">Not pinned yet: the next check reads the host&apos;s console.</p>
      )}
    </div>
  );
}

/** Where the provider signs in from, without ever showing a credential. */
function CredentialState({ provider }: { provider: Provider }) {
  if (provider.credentials_secret && provider.credentials_updated_at) {
    return (
      <span className="inline-flex max-w-full flex-wrap items-center gap-1">
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
  onDeleted,
}: {
  provider: Provider;
  canCheck: boolean;
  canEdit: boolean;
  onEdit: () => void;
  onAnnounce: (message: string) => void;
  onDeleted: () => void;
}) {
  const headingId = useId();
  const check = useCheckProvider();
  const remove = useDeleteProvider();
  const [confirmDelete, setConfirmDelete] = useState(false);
  // the plans that still need it: Delete waits for them, as the API refuses it (SDD §12, §16)
  const plans = usePlans();
  const refusal = plans.data ? deleteRefusal(plansThatNeed(provider.id, plans.data)) : null;
  const ch = provider.conversion_host;
  const preset = presetFor(provider.distribution, provider.kind);

  return (
    <article aria-labelledby={headingId} className="card flex min-w-0 flex-col wrap-anywhere gap-4 p-4 sm:p-5">
      <header className="flex flex-wrap items-start gap-3">
        <PlatformMark preset={preset} />
        <div className="min-w-0 flex-1">
          <h3 id={headingId} className="text-base font-semibold text-foreground">
            {providerLabel(provider)}
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

      <dl className="grid grid-cols-[minmax(0,1fr)_minmax(0,2fr)] gap-x-3 gap-y-1.5 text-sm">
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
        <dd className="min-w-0 wrap-break-word">
          {provider.verify_tls ? (
            <span className="inline-flex max-w-full flex-wrap items-center gap-1">
              <ShieldCheck aria-hidden className="size-3.5 text-status-success" />
              Verified
            </span>
          ) : (
            <span className="inline-flex max-w-full flex-wrap items-center gap-1 text-status-warning">
              <ShieldAlert aria-hidden className="size-3.5" />
              Verification off
            </span>
          )}
          {/* the endpoints the check found on plain HTTP (SDD §10, Security.md) */}
          {Array.isArray(provider.capabilities.plain_http) && provider.capabilities.plain_http.length > 0 && (
            <RowWarning>{`Plain HTTP: ${plainHttpText(provider.capabilities.plain_http.map(String))}. Credentials travel unencrypted: use HTTPS outside a lab.`}</RowWarning>
          )}
        </dd>
        {provider.role === 'destination' && provider.kind !== 'vmware' && <DestinationRows provider={provider} />}
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

      {provider.kind !== 'vmware' && ch && <HostKeys provider={provider} />}

      {provider.role === 'destination' && provider.kind !== 'vmware' && <GpuCapacitySummary provider={provider} />}
      {provider.ceph && <CephAccessSummary provider={provider} />}

      <Capabilities capabilities={provider.capabilities} />

      {check.error && <ErrorBanner error={check.error} title="Check failed" />}

      <footer className="mt-auto flex flex-wrap gap-2 border-t border-border pt-3">
        <Button
          icon={RefreshCw}
          loading={check.isPending}
          disabledReason={canCheck ? null : 'Checking a provider requires the operator role.'}
          onClick={() =>
            check.mutate(provider.id, {
              onSuccess: (p) => onAnnounce(`${providerLabel(p)} checked: ${providerStatusMeta(p.status).label}.`),
            })
          }
        >
          {/* the card's heading shows the provider: it is named for assistive technology only (SDD §16). The space
              before it stays outside the hidden span, so the name never relies on CSS to keep its words apart */}
          Check <span className="sr-only">{providerLabel(provider)}</span>
        </Button>
        <Link to={`/inventory/${provider.id}`} className={buttonClassName('ghost', 'md')}>
          <HardDrive aria-hidden className="size-4" />
          Inventory <span className="sr-only">of {providerLabel(provider)}</span>
        </Link>
        <Button variant="ghost" icon={Pencil} onClick={onEdit} disabledReason={canEdit ? null : 'Editing a provider requires the admin role.'}>
          Edit <span className="sr-only">{providerLabel(provider)}</span>
        </Button>
        <Button
          variant="ghost"
          icon={Trash2}
          onClick={() => setConfirmDelete(true)}
          className="ml-auto"
          disabledReason={canEdit ? refusal : 'Deleting a provider requires the admin role.'}
        >
          Delete <span className="sr-only">{providerLabel(provider)}</span>
        </Button>
      </footer>

      <ConfirmDialog
        open={confirmDelete}
        tone="danger"
        icon={Trash2}
        title={`Delete ${providerLabel(provider)}?`}
        subject={`Provider ${provider.id}`}
        consequences={[
          { kind: 'removed', text: 'the provider in Seamless, with the sign-in details and SSH key stored for it' },
          { kind: 'kept', text: 'the cloud itself: nothing changes there' },
          { kind: 'then', text: 'finished plans keep its id: none of them needs it any more' },
        ]}
        confirmLabel="Delete provider"
        requireText={provider.id}
        pending={remove.isPending}
        error={remove.error}
        onConfirm={() =>
          remove.mutate(provider.id, {
            onSuccess: () => {
              setConfirmDelete(false);
              onAnnounce(`${providerLabel(provider)} deleted.`);
              onDeleted();
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
  // a check request that failed is named and counted, never left out (SDD §16); the names stay while a retry runs,
  // until its answer replaces them, so Retry keeps the focus
  const [failed, setFailed] = useState<Array<{ id: string; error: unknown }>>([]);
  const check = (ids: string[]) =>
    checkAll.mutate(ids, {
      onSuccess: ({ checked, failed: notChecked }) => {
        setFailed(notChecked);
        const healthy = checked.filter((p) => p.status === 'ok').length;
        const count = notChecked.length ? `${checked.length} of ${ids.length}` : `${checked.length}`;
        onAnnounce(
          `Checked ${count} provider${ids.length === 1 ? '' : 's'}: ${healthy} healthy${notChecked.length ? `; ${notChecked.length} could not be checked` : ''}.`,
        );
      },
    });
  return (
    <div className="mb-5 flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2 rounded-lg border border-border px-4 py-3">
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
          onClick={() => check(providers.map((p) => p.id))}
        >
          Check all
        </Button>
      </div>
      {failed.length > 0 && (
        <ErrorBanner
          error={failed[0]!.error}
          title={`Could not check ${namesText(failed.map((f) => providerLabel(providers.find((p) => p.id === f.id) ?? { id: f.id, name: '' })))}`}
          onRetry={() => check(failed.map((f) => f.id))}
        />
      )}
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
  const headingRef = useRef<HTMLHeadingElement>(null);
  const [deleted, setDeleted] = useState<string | null>(null);
  // the deleted card is gone (with the button the dialog returned the focus to): the column
  // heading takes the focus, never the page body (SDD §16) - the deletion is taken as the column
  // renders without the card, the heading focused once that is committed
  const [headingFocus, setHeadingFocus] = useState<{ deleted: string } | null>(null);
  if (deleted && !items.some((p) => p.id === deleted)) {
    setDeleted(null);
    setHeadingFocus({ deleted });
  }
  useEffect(() => {
    if (headingFocus) headingRef.current?.focus();
  }, [headingFocus]);
  return (
    <section aria-labelledby={headingId} className="flex min-w-0 flex-col gap-4">
      <div>
        <h2 ref={headingRef} id={headingId} tabIndex={-1} className="text-base font-semibold text-foreground outline-hidden">
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
            onDeleted={() => setDeleted(provider.id)}
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
  // off, no hop of the data path checks a conversion host's key: said once, above the cards (SDD §12, §16, §26.8)
  const unchecked = useMe().data?.host_key_checking === false;
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
        description={`Every cloud Seamless migrates between: OpenStack Community, Kolla-Ansible, Red Hat OpenStack 17.1 and VMware vCenter as sources, ${DESTINATION_PLATFORMS} as destinations.`}
        actions={
          <Button size="lg" variant="primary" icon={Plus} onClick={openCreate} disabledReason={canEdit ? null : 'Adding a provider requires the admin role.'}>
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
          description={`Add the source you migrate from and the destination you migrate to. Destinations are ${DESTINATION_PLATFORMS}. Sign-in details go to the secret store, never to the database.`}
          action={
            <Button size="lg" variant="primary" icon={Plus} onClick={openCreate} disabledReason={canEdit ? null : 'Adding a provider requires the admin role.'}>
              Add provider
            </Button>
          }
        />
      )}
      {list.length > 0 && <FleetSummary providers={list} canCheck={canCheck} onAnnounce={setAnnouncement} />}
      {list.length > 0 && unchecked && (
        <p className="mb-5 flex items-start gap-2 rounded-md border border-status-warning/40 bg-status-warning/10 p-3 text-sm text-foreground">
          <TriangleAlert aria-hidden className="mt-0.5 size-4 shrink-0 text-status-warning" />
          <span>
            Host keys are not checked: SEAMLESS_ANSIBLE_HOST_KEY_CHECKING is false, so no hop of the data path checks a conversion
            host&apos;s key.
          </span>
        </p>
      )}
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
