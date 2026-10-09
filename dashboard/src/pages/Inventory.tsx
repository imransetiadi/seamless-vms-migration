import { ClipboardPlus, HardDrive, Server } from 'lucide-react';
import { useState } from 'react';
import { Navigate, useNavigate, useParams } from 'react-router-dom';
import { useInventory, useProviders } from '../api/hooks';
import { useRole } from '../api/session';
import { isDestinationInventory, type DestinationInventory, type Provider } from '../api/types';
import { Button } from '../components/Button';
import { EmptyState } from '../components/EmptyState';
import { ScrollRegion } from '../components/ScrollRegion';
import { ErrorBanner } from '../components/ErrorBanner';
import { SelectField } from '../components/Field';
import { PageHeader } from '../components/PageHeader';
import { Panel } from '../components/Panel';
import { LoadingBlock } from '../components/Skeleton';
import { ProviderStatusBadge } from '../components/StatusBadge';
import { VmTable } from '../components/VmTable';
import { formatBytes, formatNumber } from '../lib/format';
import { hasRole } from '../lib/roles';
import { PROVIDER_KIND_LABELS } from '../lib/status';
import { usePageTitle } from '../lib/usePageTitle';
import type { NewPlanState } from './Plans';

function DestinationInventoryView({ inventory }: { inventory: DestinationInventory }) {
  const networks = Object.entries(inventory.networks);
  const quotas = Object.entries(inventory.quotas);
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
                    <td className="num text-right">{f.disk_gb} GB</td>
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
                    <td className="num text-right">{formatNumber(q.cores)}</td>
                    <td className="num text-right">{formatBytes(q.ram_mb * 1024 * 1024)}</td>
                    <td className="num text-right">{formatNumber(q.instances)}</td>
                    <td className="num text-right">{formatNumber(q.volumes)}</td>
                    <td className="num text-right">{formatNumber(q.gigabytes)} GB</td>
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

function providerLabel(p: Provider): string {
  return `${p.name} (${p.role === 'source' ? 'source' : 'destination'})`;
}

export default function Inventory() {
  usePageTitle('Inventory');
  const { providerId } = useParams();
  const navigate = useNavigate();
  const providers = useProviders();
  const inventory = useInventory(providerId);
  const role = useRole();
  const [selected, setSelected] = useState<{ providerId: string; ids: Set<string> }>({ providerId: '', ids: new Set() });
  const provider = providers.data?.find((p) => p.id === providerId);
  const canPlan = hasRole(role, 'operator') && provider?.role === 'source';
  const selection = selected.providerId === providerId ? selected.ids : new Set<string>();

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
          <SelectField
            label="Provider"
            className="w-full sm:w-80"
            value={providerId}
            onChange={(e) => navigate(`/inventory/${e.target.value}`)}
            options={(providers.data ?? []).map((p) => ({ value: p.id, label: providerLabel(p) }))}
          />
        }
      />

      {/* the inventory shows with its provider: while that is loading or failed, say so (SDD §16) */}
      {providers.error && <ErrorBanner error={providers.error} title="Providers are unavailable" onRetry={() => void providers.refetch()} />}
      {providers.data && !provider && (
        <EmptyState icon={Server} title={`Provider ${providerId} does not exist`} description="Pick another provider above." />
      )}
      {(providers.isPending || (inventory.isPending && provider)) && <LoadingBlock label="Reading inventory…" rows={6} />}
      {inventory.error && (
        <ErrorBanner error={inventory.error} title={`Cannot read the inventory of ${provider?.name ?? providerId}`} onRetry={() => void inventory.refetch()} />
      )}
      {data && provider && !isDestinationInventory(data) && (
        data.length === 0 ? (
          <EmptyState icon={HardDrive} title="No VMs found" description="The provider reported an empty inventory." />
        ) : (
          <div className="flex flex-col gap-3">
            {canPlan && (
              <div className="flex flex-wrap items-center gap-2">
                <Button
                  variant="primary"
                  size="lg"
                  icon={ClipboardPlus}
                  disabledReason={selection.size === 0 ? 'Select VMs in the table first.' : null}
                  onClick={() =>
                    navigate('/plans', { state: { newPlan: { sourceId: provider.id, vmIds: [...selection] } } satisfies NewPlanState })
                  }
                >
                  Create plan{selection.size ? ` with ${selection.size} VM${selection.size === 1 ? '' : 's'}` : ''}
                </Button>
                <span className="text-xs text-muted-foreground">Select VMs below to start a plan with them.</span>
              </div>
            )}
            <VmTable
              vms={data}
              providerKind={provider.kind}
              caption={`VMs on ${provider.name}`}
              selected={canPlan ? selection : undefined}
              onSelectedChange={canPlan ? (ids) => setSelected({ providerId: provider.id, ids }) : undefined}
            />
          </div>
        )
      )}
      {data && provider && isDestinationInventory(data) && <DestinationInventoryView inventory={data} />}
    </>
  );
}
