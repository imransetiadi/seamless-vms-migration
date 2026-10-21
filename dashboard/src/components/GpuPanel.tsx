import { CircleCheck, CircleMinus, CircleX, Hourglass, TriangleAlert } from 'lucide-react';
import type { Migration, Plan, Provider, ProviderKind, VerificationCheck } from '../api/types';
import { cn } from '../lib/cn';
import { formatRelative, namesText } from '../lib/format';
import { declaredProfileOf, GPU_CHECKS, gpuFlavorOf, gpuKey, gpuLabel, gpuWaitWhat, vmGpus } from '../lib/gpu';
import { preflightRan } from '../lib/phase';
import { providerLabel } from '../lib/providerUse';
import { TONE_CLASSES } from '../lib/status';
import { useNow } from '../lib/useNow';
import { Panel } from './Panel';
import { SeverityBadge } from './StatusBadge';
import { TechPreviewBadge } from './TechPreviewBadge';

/** The capacity findings (SDD §22.5). */
const CAPACITY_CODES: ReadonlySet<string> = new Set(['DST_GPU_CAPACITY_INSUFFICIENT', 'DST_GPU_CAPACITY_UNKNOWN']);
/** The blockers of a GPU no mapping applies to (SDD §9.3, §22.5). */
const GPU_BLOCKERS: ReadonlySet<string> = new Set(['VM_VGPU', 'VM_PCI_PASSTHROUGH']);

const CHECK_STATE = {
  ok: { label: 'passed', icon: CircleCheck, className: TONE_CLASSES.success.text },
  failed: { label: 'failed', icon: CircleX, className: TONE_CLASSES.danger.text },
  skipped: { label: 'skipped', icon: CircleMinus, className: 'text-muted-foreground' },
} as const;

function checkState(c: VerificationCheck): keyof typeof CHECK_STATE {
  return c.skipped ? 'skipped' : c.ok ? 'ok' : 'failed';
}

/**
 * Its GPU flavor and the mapping that chose it, as the plan names it now (SDD §22.4): a VMware VM's GPU keys all mapped to
 * one flavor, an OpenStack VM's flavor mapping - for a VM without a GPU of its own, the flavor mapping that sends it to a
 * GPU flavor -, or why none applies.
 */
function mappingText(m: Migration, plan: Plan, kind: ProviderKind, declared: string | null): string {
  const vm = m.vm;
  const gpus = vmGpus(vm, declared);
  const flavorMapping = vm.flavor && Object.hasOwn(plan.mappings.flavors, vm.flavor) ? plan.mappings.flavors[vm.flavor]! : null;
  if (!gpus.length || kind !== 'vmware') {
    return flavorMapping ? `${flavorMapping}, by the flavor mapping ${vm.flavor} → ${flavorMapping}` : `None: no flavor mapping names ${vm.flavor ?? 'its flavor'}.`;
  }
  const flavor = gpuFlavorOf(vm, plan, kind, declared);
  const keys = [...new Set(gpus.map(gpuKey))];
  const mapped = plan.mappings.gpus ?? {};
  if (keys.includes(null)) return 'None: no mapping can name its device without ids.';
  const named = keys as string[];
  const unnamed = named.filter((key) => !Object.hasOwn(mapped, key));
  if (unnamed.length) return `None: no GPU mapping names ${namesText(unnamed, unnamed.length)}.`;
  const pairs = named.map((key) => `${key} → ${mapped[key]}`);
  if (!flavor) return `None: its GPUs map to different flavors (${pairs.join(', ')}), and its server is created with one.`;
  return `${flavor}, by the GPU mapping${pairs.length > 1 ? 's' : ''} ${namesText(pairs, pairs.length)}`;
}

/**
 * The migration page's GPU panel (SDD §16, §22.11; plan task G12): the VM's GPUs as the Inventory words them, its GPU
 * flavor and the mapping that chose it, the vGPU profiles its source and its destination declare, the capacity findings
 * and the last verification's GPU checks; while its cutover waits for GPU capacity, the wait, its source running.
 */
export function GpuPanel({ migration: m, plan, source, destination }: { migration: Migration; plan: Plan | null | undefined; source: Provider | undefined; destination: Provider | undefined }) {
  const now = useNow(5000, m.gpu_wait !== null);
  const declared = declaredProfileOf(m.vm, source?.gpu_profiles);
  const gpus = vmGpus(m.vm, declared);
  // the source's kind decides, else what the VM shows - a VMware VM has no flavor (the API's _kind_of, SDD §22.4)
  const kind: ProviderKind = source?.kind ?? (m.vm.flavor === null ? 'vmware' : 'openstack');
  const flavor = plan ? (gpus.length ? gpuFlavorOf(m.vm, plan, kind, declared) : (m.vm.flavor && Object.hasOwn(plan.mappings.flavors, m.vm.flavor) ? plan.mappings.flavors[m.vm.flavor]! : null)) : null;
  const blockers = m.findings.filter((f) => GPU_BLOCKERS.has(f.code));
  const capacity = m.findings.filter((f) => CAPACITY_CODES.has(f.code));
  const checks = m.verification?.checks.filter((c) => GPU_CHECKS.has(c.name)) ?? [];
  const destinationProfile = flavor && destination?.gpu_profiles && Object.hasOwn(destination.gpu_profiles, flavor) ? destination.gpu_profiles[flavor]! : null;
  const profiles = [
    ...(declared && m.vm.flavor && source ? [`${m.vm.flavor} gives ${declared}, declared by ${providerLabel(source)}`] : []),
    ...(destinationProfile && destination ? [`${flavor} gives ${destinationProfile}, declared by ${providerLabel(destination)}`] : []),
  ];
  const wait = m.gpu_wait;
  return (
    <Panel title="GPU" description="Its GPUs, the GPU flavor its plan maps them to, and what the validation and the verification found (SDD §22)" actions={<TechPreviewBadge />}>
      <div className="flex flex-col gap-3">
        {wait && (
          // the source keeps running while the cutover waits (SDD §5.4 rule 5, §22.7): announced from the page's region
          <div className="flex items-start gap-2 rounded-md border border-status-info/40 bg-status-info/10 p-3 text-sm">
            <Hourglass aria-hidden className="mt-0.5 size-4 shrink-0 text-status-info" />
            <div className="min-w-0 text-foreground">
              <p className="wrap-break-word">
                Waiting for GPU capacity: {gpuWaitWhat(wait)}, checked {formatRelative(wait.checked_at, now)}. The source VM keeps running.
              </p>
              {wait.free !== null && <p className="mt-1 wrap-break-word text-xs text-muted-foreground">{wait.reason}</p>}
            </div>
          </div>
        )}
        <dl className="grid grid-cols-1 gap-x-4 gap-y-2 text-sm sm:grid-cols-[auto_minmax(0,1fr)]">
          <dt className="text-muted-foreground">GPUs</dt>
          <dd className="min-w-0">
            {gpus.length ? (
              <ul className="flex flex-col gap-0.5">
                {gpus.map((g, i) => (
                  <li key={i} className="wrap-break-word">
                    {gpuLabel(g)}
                  </li>
                ))}
              </ul>
            ) : (
              'None of its own'
            )}
          </dd>
          <dt className="text-muted-foreground">GPU flavor</dt>
          <dd className="min-w-0">
            <p className="wrap-break-word">{plan ? mappingText(m, plan, kind, declared) : 'Unknown while the plan is not loaded.'}</p>
            {blockers.map((f) => (
              // the validation's own reason, which the destination's flavors decide: a mapping to a flavor without a GPU
              <p key={f.code} className="mt-1 flex items-start gap-1 text-xs text-status-danger">
                <TriangleAlert aria-hidden className="mt-px size-3.5 shrink-0" />
                <span className="wrap-break-word">{f.message}</span>
              </p>
            ))}
          </dd>
          <dt className="text-muted-foreground">Declared profiles</dt>
          <dd className="min-w-0">
            {profiles.length ? (
              <ul className="flex flex-col gap-0.5">
                {profiles.map((text) => (
                  <li key={text} className="wrap-break-word">
                    {text}
                  </li>
                ))}
              </ul>
            ) : (
              'None declared'
            )}
          </dd>
          <dt className="text-muted-foreground">Capacity</dt>
          <dd className="min-w-0">
            {capacity.length ? (
              <ul className="flex flex-col gap-1">
                {capacity.map((f) => (
                  <li key={f.code} className="flex flex-wrap items-start gap-2">
                    <SeverityBadge severity={f.severity} />
                    <span className="min-w-0 flex-1 wrap-break-word">{f.message}</span>
                  </li>
                ))}
              </ul>
            ) : !preflightRan(m) ? (
              'The validation checks it.'
            ) : m.findings.some((f) => f.code === 'MAP_GPU_FLAVOR') ? (
              'The validation found room for its GPU flavor.'
            ) : (
              'The validation checks it once a GPU mapping applies.'
            )}
          </dd>
          <dt className="text-muted-foreground">GPU checks</dt>
          <dd className="min-w-0">
            {m.verification === null ? (
              'Not verified yet: the verification checks its GPU flavor and allocation after the cutover.'
            ) : checks.length === 0 ? (
              'The last verification ran no GPU check.'
            ) : (
              <ul className="flex flex-col gap-1">
                {checks.map((c) => {
                  const state = CHECK_STATE[checkState(c)];
                  return (
                    <li key={c.name} className="flex items-start gap-2">
                      <state.icon aria-hidden className={cn('mt-0.5 size-3.5 shrink-0', state.className)} />
                      <span className="min-w-0">
                        <code className="text-xs">{c.name}</code>: {state.label}
                        <span className="block wrap-anywhere text-xs text-muted-foreground">{` ${c.detail}`}</span>
                      </span>
                    </li>
                  );
                })}
              </ul>
            )}
          </dd>
        </dl>
      </div>
    </Panel>
  );
}
