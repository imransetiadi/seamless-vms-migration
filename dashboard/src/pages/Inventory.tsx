import { useListState } from '../lib/useListState';
import { ClipboardPlus, HardDrive, Info, Server, TriangleAlert } from 'lucide-react';
import { useMemo } from 'react';
import { Navigate, useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { useInventory, useMigrationSummaries, usePlans, useProviders } from '../api/hooks';
import { useRole } from '../api/session';
import { isDestinationInventory, type DestinationInventory, type GpuCapacity, type GpuFlavor, type Provider } from '../api/types';
import { Button } from '../components/Button';
import { EmptyState } from '../components/EmptyState';
import { ScrollRegion } from '../components/ScrollRegion';
import { ErrorBanner } from '../components/ErrorBanner';
import { SelectField } from '../components/Field';
import { PageHeader } from '../components/PageHeader';
import { Panel } from '../components/Panel';
import { LoadingBlock } from '../components/Skeleton';
import { ProviderStatusBadge } from '../components/StatusBadge';
import { TechPreviewBadge } from '../components/TechPreviewBadge';
import { VmTable } from '../components/VmTable';
import { formatBytes, formatDateTime, formatNumber } from '../lib/format';
import { declaredProfiles, flavorsOfPool, gpuCapacityReason, gpuLabel, poolFreeText, poolLabel } from '../lib/gpu';
import { holdersOf } from '../lib/holds';
import { projectChoices } from '../lib/projectCredentials';
import { providerLabel } from '../lib/providerUse';
import { KEEP_FOCUS } from '../lib/navigation';
import { hasRole } from '../lib/roles';
import { PROVIDER_KIND_LABELS } from '../lib/status';
import { usePageTitle } from '../lib/usePageTitle';
import type { NewPlanState } from './Plans';

/** OpenStack's "GB" (a flavor's disk, a Cinder quota's gigabytes) is GiB (SDD §16). */
const GIB = 1024 ** 3;

/** A free quota (SDD §10): -1 is unlimited and null not reported, never shown as a number. */
function quotaText(value: number | null, format: (value: number) => string): string {
  if (value === null) return 'Not reported';
  return value < 0 ? 'Unlimited' : format(value);
}

/** A GPU flavor by its name and the profiles it gives: "g1.a100-4c (A100-4C)". */
function flavorName(flavor: GpuFlavor): string {
  const profiles = declaredProfiles(flavor);
  return profiles.length ? `${flavor.name} (${profiles.join(', ')})` : flavor.name;
}

/** A destination's GPU flavors: each one's size, its GPU requests and its declared profile (SDD §16, §22.3). */
function GpuFlavorsPanel({ flavors }: { flavors: GpuFlavor[] | undefined }) {
  return (
    <Panel
      title="GPU flavors"
      description="The flavors whose extra specs ask for a GPU, which a plan's GPU mapping names; a vGPU profile is the one the provider declares"
      actions={<TechPreviewBadge />}
    >
      {flavors === undefined ? (
        <p className="text-sm text-muted-foreground">Not reported.</p>
      ) : flavors.length === 0 ? (
        <p className="text-sm text-muted-foreground">No flavor asks for a GPU.</p>
      ) : (
        <ScrollRegion label="Destination GPU flavors">
          <table className="data-table">
            <caption className="sr-only">Destination GPU flavors</caption>
            <thead>
              <tr>
                <th scope="col">Flavor</th>
                <th scope="col" className="text-right">
                  vCPU
                </th>
                <th scope="col" className="text-right">
                  RAM
                </th>
                <th scope="col" className="text-right">
                  Disk
                </th>
                <th scope="col">GPU requests</th>
                <th scope="col">Declared profile</th>
              </tr>
            </thead>
            <tbody>
              {flavors.map((f) => {
                const profiles = declaredProfiles(f);
                return (
                  <tr key={f.name}>
                    <th scope="row" className="font-mono">
                      {f.name}
                    </th>
                    <td className="num text-right">{f.vcpus}</td>
                    <td className="num text-right">{formatBytes(f.ram_mb * 1024 * 1024)}</td>
                    <td className="num text-right">{formatBytes(f.disk_gb * GIB)}</td>
                    <td>
                      {f.gpus.map((g, i) => (
                        <span key={i} className="block">
                          {gpuLabel(g, { declared: false })}
                        </span>
                      ))}
                    </td>
                    <td className={profiles.length ? undefined : 'text-muted-foreground'}>{profiles.length ? profiles.join(', ') : 'None declared'}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </ScrollRegion>
      )}
    </Panel>
  );
}

/**
 * A destination's GPU capacity as its inventory read it (SDD §16, §22.3): each pool with the flavors that ask of it and
 * its free units, or why it was not read; an alias flavor's capacity is unknown whatever Placement answers.
 */
function GpuCapacityPanel({ capacity, flavors }: { capacity: GpuCapacity | undefined; flavors: GpuFlavor[] }) {
  const aliases = flavors.filter((f) => f.gpus.some((g) => g.kind === 'passthrough' && g.alias));
  const readAt = capacity?.read_at ? ` at ${formatDateTime(capacity.read_at)}` : '';
  return (
    <Panel title="GPU capacity" description={`Placement's free vGPU units, read with the inventory${readAt}`} actions={<TechPreviewBadge />}>
      <div className="flex flex-col gap-3">
        {capacity === undefined ? (
          <p className="text-sm text-muted-foreground">Not reported.</p>
        ) : capacity.status !== 'ok' ? (
          // nothing read is never shown as free units (SDD §16): the reason, in words
          <p className="flex items-start gap-1.5 text-sm text-status-warning">
            <TriangleAlert aria-hidden className="mt-0.5 size-4 shrink-0" />
            <span>Not read: {gpuCapacityReason(capacity)}.</span>
          </p>
        ) : capacity.pools.length === 0 ? (
          <p className="text-sm text-muted-foreground">No flavor asks for a vGPU: Placement has no pool to read.</p>
        ) : (
          <>
            <ScrollRegion label="Destination GPU capacity">
              <table className="data-table">
                <caption className="sr-only">Destination GPU capacity</caption>
                <thead>
                  <tr>
                    <th scope="col">Pool</th>
                    <th scope="col">GPU flavors</th>
                    <th scope="col">Free</th>
                  </tr>
                </thead>
                <tbody>
                  {capacity.pools.map((pool) => (
                    <tr key={JSON.stringify([pool.resource_class, pool.traits, pool.forbidden_traits, pool.same_provider])}>
                      <th scope="row" className="font-mono">
                        {poolLabel(pool)}
                      </th>
                      <td>{flavorsOfPool(pool, flavors).map(flavorName).join(', ') || '—'}</td>
                      <td>{poolFreeText(pool)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </ScrollRegion>
            {capacity.pools.some((p) => p.traits.length + p.forbidden_traits.length === 0) && (
              <p className="text-xs text-muted-foreground">A resource class alone unites its pools: a plan's GPU VMs count against it across them.</p>
            )}
          </>
        )}
        {aliases.length > 0 && (
          <ul className="flex flex-col gap-1">
            {aliases.map((f) => (
              <li key={f.name} className="flex items-start gap-1.5 text-sm">
                <Info aria-hidden className="mt-0.5 size-4 shrink-0 text-status-info" />
                <span>
                  <span className="font-mono">{f.name}</span>: Capacity unknown: PCI aliases are configured in nova.conf
                </span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </Panel>
  );
}

function DestinationInventoryView({ inventory }: { inventory: DestinationInventory }) {
  const networks = Object.entries(inventory.networks);
  const quotas = Object.entries(inventory.quotas);
  const projects = [...inventory.projects].sort((a, b) => a.localeCompare(b));
  return (
    <div className="grid gap-4 xl:grid-cols-2">
      <Panel title="Networks" description="Name and MTU (Geneve tenant networks are typically 1442)">
        {networks.length === 0 ? (
          <p className="text-sm text-muted-foreground">No networks reported.</p>
        ) : (
          <ScrollRegion label="Destination networks">
            <table className="data-table">
              <caption className="sr-only">Destination networks</caption>
              <thead>
                <tr>
                  <th scope="col">Network</th>
                  <th scope="col" className="text-right">
                    MTU
                  </th>
                </tr>
              </thead>
              <tbody>
                {networks.map(([name, mtu]) => (
                  <tr key={name}>
                    <th scope="row" className="font-mono">
                      {name}
                    </th>
                    <td className="num text-right">{mtu ?? '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </ScrollRegion>
        )}
      </Panel>
      <Panel title="Flavors">
        {inventory.flavors.length === 0 ? (
          <p className="text-sm text-muted-foreground">No flavors reported.</p>
        ) : (
          <ScrollRegion label="Destination flavors">
            <table className="data-table">
              <caption className="sr-only">Destination flavors</caption>
              <thead>
                <tr>
                  <th scope="col">Flavor</th>
                  <th scope="col" className="text-right">
                    vCPU
                  </th>
                  <th scope="col" className="text-right">
                    RAM
                  </th>
                  <th scope="col" className="text-right">
                    Disk
                  </th>
                  <th scope="col">Extra specs</th>
                </tr>
              </thead>
              <tbody>
                {inventory.flavors.map((f) => (
                  <tr key={f.name}>
                    <th scope="row" className="font-mono">
                      {f.name}
                    </th>
                    <td className="num text-right">{f.vcpus}</td>
                    <td className="num text-right">{formatBytes(f.ram_mb * 1024 * 1024)}</td>
                    <td className="num text-right">{formatBytes(f.disk_gb * GIB)}</td>
                    <td className="text-xs text-muted-foreground">
                      {Object.entries(f.extra_specs)
                        .map(([k, v]) => `${k}=${v}`)
                        .join(', ') || '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </ScrollRegion>
        )}
      </Panel>
      <GpuFlavorsPanel flavors={inventory.gpu_flavors} />
      {/* its GPU capacity while it has a GPU flavor: without one, Placement is sent nothing (SDD §16, §22.3) */}
      {inventory.gpu_flavors && inventory.gpu_flavors.length > 0 && <GpuCapacityPanel capacity={inventory.gpu_capacity} flavors={inventory.gpu_flavors} />}
      <Panel title="Volume types">
        {inventory.volume_types.length === 0 ? (
          <p className="text-sm text-muted-foreground">No volume types reported.</p>
        ) : (
          <ul className="flex flex-wrap gap-1.5">
            {inventory.volume_types.map((t) => (
              <li key={t} className="rounded-sm border border-border px-2 py-0.5 font-mono text-xs">
                {t}
              </li>
            ))}
          </ul>
        )}
      </Panel>
      <Panel title="Projects" description="The names plan project mappings point to; pre-flight blocks a VM whose project is not here (DST_PROJECT_MISSING)">
        {projects.length === 0 ? (
          <p className="text-sm text-muted-foreground">No projects reported.</p>
        ) : (
          <ul className="flex flex-wrap gap-1.5">
            {projects.map((name) => (
              <li key={name} className="rounded-sm border border-border px-2 py-0.5 font-mono text-xs">
                {name}
              </li>
            ))}
          </ul>
        )}
      </Panel>
      <Panel title="Free quota per project" description="Pre-flight checks the plan's cumulative demand against these (DST_QUOTA_INSUFFICIENT)">
        {quotas.length === 0 ? (
          <p className="text-sm text-muted-foreground">No quotas reported.</p>
        ) : (
          <ScrollRegion label="Free quota per destination project">
            <table className="data-table">
              <caption className="sr-only">Free quota per destination project</caption>
              <thead>
                <tr>
                  <th scope="col">Project</th>
                  <th scope="col" className="text-right">
                    Cores
                  </th>
                  <th scope="col" className="text-right">
                    RAM
                  </th>
                  <th scope="col" className="text-right">
                    Instances
                  </th>
                  <th scope="col" className="text-right">
                    Volumes
                  </th>
                  <th scope="col" className="text-right">
                    Storage
                  </th>
                </tr>
              </thead>
              <tbody>
                {quotas.map(([project, q]) => (
                  <tr key={project}>
                    <th scope="row">{project}</th>
                    <td className="num text-right">{quotaText(q.cores, formatNumber)}</td>
                    <td className="num text-right">{quotaText(q.ram_mb, (mb) => formatBytes(mb * 1024 * 1024))}</td>
                    <td className="num text-right">{quotaText(q.instances, formatNumber)}</td>
                    <td className="num text-right">{quotaText(q.volumes, formatNumber)}</td>
                    <td className="num text-right">{quotaText(q.gigabytes, (gb) => formatBytes(gb * GIB))}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </ScrollRegion>
        )}
      </Panel>
    </div>
  );
}

function providerOption(p: Provider): string {
  return `${providerLabel(p)} (${p.role === 'source' ? 'source' : 'destination'})`;
}

export default function Inventory() {
  usePageTitle('Inventory');
  const { providerId } = useParams();
  const [search] = useSearchParams();
  const wanted = search.get('vm');
  // whose credential reads it: a project's, kept in the address, else the provider's own (SDD §12, §16, §27.4)
  const project = search.get('project') || null;
  const navigate = useNavigate();
  const providers = useProviders();
  const inventory = useInventory(providerId, project);
  const role = useRole();
  const listState = useListState('inventory-selection');
  const [selected, setSelected] = listState.field<{ providerId: string; ids: Set<string> }>('selection', { providerId: '', ids: new Set() });
  const provider = providers.data?.find((p) => p.id === providerId);
  // planning starts from a source; viewers see the action unavailable, with the reason (SDD §16)
  const isSource = provider?.role === 'source';
  const canPlan = hasRole(role, 'operator') && isSource;
  const selection = new Set(selected.providerId === providerId ? [...selected.ids].filter(id => !Array.isArray(inventory.data) || inventory.data.some(vm => vm.source_id === id)) : []);
  // which plans hold each VM of a source (SDD §5.4, §16): unknown, never free, while the plans or migrations fail
  const plans = usePlans();
  const migrations = useMigrationSummaries();
  const holders = useMemo(() => {
    if (!isSource || !provider) return undefined;
    if (plans.data && migrations.data) return holdersOf(provider.id, plans.data, migrations.data);
    return (plans.error && !plans.data) || (migrations.error && !migrations.data) ? null : undefined;
  }, [isSource, provider, plans.data, plans.error, migrations.data, migrations.error]);

  if (!providerId) {
    if (providers.isPending) return <LoadingBlock label="Loading providers…" />;
    const first = providers.data?.find((p) => p.role === 'source') ?? providers.data?.[0];
    if (first) return <Navigate to={`/inventory/${first.id}`} replace />;
    return (
      <>
        <PageHeader title="Inventory" />
        {providers.error ? (
          <ErrorBanner error={providers.error} title="Providers are unavailable" onRetry={() => void providers.refetch()} />
        ) : (
          <EmptyState icon={Server} title="No providers registered" description="Register a source provider to discover its VMs." />
        )}
      </>
    );
  }

  const data = inventory.data;
  return (
    <>
      <PageHeader
        title="Inventory"
        description={
          provider
            ? `${PROVIDER_KIND_LABELS[provider.kind] ?? provider.kind} · ${provider.role === 'source' ? 'VMs with disk sizes and readiness flags' : 'what migrated VMs land on'}`
            : 'Discovered resources'
        }
        meta={provider ? <ProviderStatusBadge status={provider.status} /> : undefined}
        actions={
          <div className="flex w-full flex-col gap-3 sm:w-auto sm:flex-row sm:flex-wrap sm:items-end">
            <SelectField
              label="Provider"
              className="w-full sm:w-80"
              value={providerId}
              // switches the inventory in place: focus stays here and the address is replaced, a project dropped (WCAG 3.2.2, SDD §16)
              onChange={(e) => navigate(`/inventory/${e.target.value}`, { replace: true, state: KEEP_FOCUS })}
              options={(providers.data ?? []).map((p) => ({ value: p.id, label: providerOption(p) }))}
            />
            {provider && provider.kind !== 'vmware' && (Object.keys(provider.project_credentials ?? {}).length > 0 || project) && (
              <SelectField
                label="Project credential"
                aside={<TechPreviewBadge feature="credentials" />}
                className="w-full sm:w-80"
                value={project ?? ''}
                // in place, as the Provider select: the address keeps the project (SDD §16)
                onChange={(e) =>
                  navigate(`/inventory/${provider.id}${e.target.value ? `?project=${encodeURIComponent(e.target.value)}` : ''}`, { replace: true, state: KEEP_FOCUS })
                }
                options={[
                  ...projectChoices(provider),
                  // a project an old address names that the provider holds no credential for: read as the API answers it
                  ...(project && !projectChoices(provider).some((c) => c.value === project) ? [{ value: project, label: `${project} (no credential)` }] : []),
                ]}
              />
            )}
          </div>
        }
      />

      {/* the inventory shows with its provider: while that is loading or failed, say so (SDD §16) */}
      {providers.error && <ErrorBanner error={providers.error} title="Providers are unavailable" onRetry={() => void providers.refetch()} />}
      {providers.data && !provider && (
        <EmptyState icon={Server} title={`Provider ${providerId} does not exist`} description="Pick another provider above." />
      )}
      {(providers.isPending || (inventory.isPending && provider)) && <LoadingBlock label="Reading inventory…" rows={6} />}
      {inventory.error && (!providers.data || provider) && (
        <ErrorBanner error={inventory.error} title={`Cannot read the inventory of ${provider ? providerLabel(provider) : providerId}`} onRetry={() => void inventory.refetch()} />
      )}
      {data && provider && !isDestinationInventory(data) && (
        data.length === 0 ? (
          <EmptyState icon={HardDrive} title="No VMs found" description="The provider reported an empty inventory." />
        ) : (
          <div className="flex flex-col gap-3">
            {isSource && (
              <div className="flex flex-wrap items-center gap-2">
                <Button
                  variant="primary"
                  size="lg"
                  icon={ClipboardPlus}
                  disabledReason={!canPlan ? 'Creating a plan requires the operator role.' : selection.size === 0 ? 'Select VMs in the table first.' : null}
                  onClick={() =>
                    navigate('/plans', { state: { newPlan: { sourceId: provider.id, vmIds: [...selection] } } satisfies NewPlanState })
                  }
                >
                  Create plan{selection.size ? ` with ${selection.size} VM${selection.size === 1 ? '' : 's'}` : ''}
                </Button>
                {canPlan && <span className="text-xs text-muted-foreground">Select VMs below to start a plan with them.</span>}
              </div>
            )}
            <VmTable
              persistenceKey={`inventory:${providerId}:${wanted ?? ''}`}
              // a link to one VM by its source id (`?vm=`, SDD §16) opens the table searching for its name
              key={`${providerId}:${wanted ?? ''}`}
              initialQuery={wanted ? (data.find((vm) => vm.source_id === wanted)?.name ?? wanted) : ''}
              vms={data}
              providerKind={provider.kind}
              caption={`VMs on ${providerLabel(provider)}`}
              selected={canPlan ? selection : undefined}
              onSelectedChange={canPlan ? (ids) => setSelected({ providerId: provider.id, ids }) : undefined}
              holders={holders}
            />
          </div>
        )
      )}
      {data && provider && isDestinationInventory(data) && <DestinationInventoryView inventory={data} />}
    </>
  );
}
