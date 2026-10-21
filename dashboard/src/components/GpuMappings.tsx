import { Ban, CircleAlert, TriangleAlert } from 'lucide-react';
import type { DestinationInventory, GpuFlavor, Provider, ProviderKind, VMRef } from '../api/types';
import { gpuFlavorRows, gpuFlavorText, gpuKeyRows, gpuLabel, misfitText, vmsSizeText, type GpuVm } from '../lib/gpu';
import { providerLabel } from '../lib/providerUse';
import { ErrorBanner } from './ErrorBanner';
import { SelectField } from './Field';
import { LoadingBlock } from './Skeleton';
import { TechPreviewBadge } from './TechPreviewBadge';

/** The destination's GPU flavors as the form reads them, and why none are offered (SDD §16). */
export interface OfferedGpuFlavors {
  destination: Provider | undefined;
  /** Its inventory's answer, once read. */
  inventory: DestinationInventory | undefined;
  flavors: GpuFlavor[];
  pending: boolean;
  error: unknown;
  retry: () => void;
}

/** Where the destination's GPU flavors stand, when none can be offered yet (SDD §16). */
function Offer({ offered }: { offered: OfferedGpuFlavors }) {
  const name = offered.destination ? providerLabel(offered.destination) : null;
  if (!offered.destination) return <p className="text-sm text-muted-foreground">Choose the destination to map these GPUs to its GPU flavors.</p>;
  if (offered.error && !offered.inventory) return <ErrorBanner error={offered.error} title={`Cannot read the GPU flavors of ${name}`} onRetry={offered.retry} />;
  if (offered.pending) return <LoadingBlock label={`Reading the GPU flavors of ${name}…`} rows={2} />;
  if (!offered.flavors.length) return <p className="text-sm text-status-warning">{name} lists no GPU flavor: the VMs with a GPU stay blocked.</p>;
  return null;
}

/** "Does not fit {vm}: ..." for each VM a flavor does not fit, as MAP_GPU_FLAVOR warns (SDD §22.4). */
function misfits(flavor: GpuFlavor | undefined, vms: readonly VMRef[]): string[] {
  if (!flavor) return [];
  return vms.flatMap((vm) => {
    const text = misfitText(flavor, vm);
    return text ? [`Does not fit ${vm.name}: ${text}.`] : [];
  });
}

/**
 * The plan form's GPU mappings (SDD §16, §22.4; plan task G12), shown while a selected VM has a GPU: for a VMware source
 * one row per GPU key, a choice among the destination's GPU flavors or None, never chosen for the operator, marking the VMs
 * its flavor does not fit, and each device without a GPU key named as one no mapping can take; for an OpenStack source
 * each flavor of the selected VMs that asks for a GPU, with how the flavor mappings map it.
 */
export function GpuMappings({
  idFor,
  sourceKind,
  vms,
  offered,
  mappings,
  onChange,
  flavorMappings,
  onEditFlavorMappings,
}: {
  idFor: (key: string) => string;
  sourceKind: ProviderKind;
  vms: readonly GpuVm[];
  offered: OfferedGpuFlavors;
  /** The plan's GPU mappings as the form holds them. */
  mappings: Readonly<Record<string, string>>;
  onChange: (key: string, flavor: string | null) => void;
  /** The flavor mappings as typed, parsed; null while they cannot be read. */
  flavorMappings: Record<string, string> | null;
  onEditFlavorMappings: () => void;
}) {
  const destination = offered.destination ? providerLabel(offered.destination) : 'the destination';
  const offeredNames = new Set(offered.flavors.map((f) => f.name));
  const hintId = idFor('gpu-hint');
  return (
    <fieldset className="flex min-w-0 flex-col gap-3" aria-describedby={hintId}>
      <legend className="mb-1 text-xs font-semibold uppercase tracking-wide text-muted-foreground">GPU mappings</legend>
      <div className="-mt-2 flex flex-wrap items-center gap-2">
        <TechPreviewBadge />
      </div>
      <p id={hintId} className="text-xs text-muted-foreground">
        {sourceKind === 'vmware'
          ? "Each GPU key of the selected VMs goes to the GPU flavor its row names, and its VMs' servers are created with it; a key left at None keeps its VMs blocked."
          : `These VMs ask for a GPU through their flavor: its flavor mapping is their GPU mapping. Map each flavor to a GPU flavor of ${destination} under Flavor mappings; a same-named flavor is no mapping.`}
      </p>
      <Offer offered={offered} />
      {sourceKind === 'vmware' ? (
        <VmwareRows idFor={idFor} vms={vms} offered={offered} offeredNames={offeredNames} destination={destination} mappings={mappings} onChange={onChange} />
      ) : (
        <>
          <ul className="flex flex-col gap-2">
            {gpuFlavorRows(vms).map((row) => {
              const label = `${row.flavor}: ${vmsSizeText(row.vms)}`;
              const labelId = idFor(`gpu-flavor-${row.flavor}`);
              const target = flavorMappings && Object.hasOwn(flavorMappings, row.flavor) ? flavorMappings[row.flavor]! : null;
              const gpuFlavor = target === null ? undefined : offered.flavors.find((f) => f.name === target);
              const listed = Boolean(target !== null && offered.inventory?.flavors.some((f) => f.name === target));
              const status =
                flavorMappings === null
                  ? 'Fix the flavor mappings to see how they map it.'
                  : target === null
                    ? 'Not mapped: validation blocks its VMs until Flavor mappings map it to a GPU flavor.'
                    : gpuFlavor
                      ? `Flavor mappings map it to ${gpuFlavorText(gpuFlavor)}.`
                      : listed
                        ? `Flavor mappings map it to ${target}, which asks for no GPU: its server would lose its GPU, so validation blocks it.`
                        : offered.inventory
                          ? `Flavor mappings map it to ${target}, which ${destination} does not list.`
                          : `Flavor mappings map it to ${target}.`;
              const warned = flavorMappings === null || target === null || !gpuFlavor;
              return (
                <li key={row.flavor} aria-labelledby={labelId} className="rounded-md border border-border p-3 text-sm">
                  <p id={labelId} className="font-medium text-foreground wrap-break-word">
                    {label}
                  </p>
                  <p className="text-xs text-muted-foreground wrap-break-word">
                    {row.vms.map((vm) => vm.name).join(', ')}. Asks for {row.gpus.map((g) => gpuLabel(g)).join('; ')}
                  </p>
                  <p className={warned ? 'mt-1 flex items-start gap-1 text-xs text-status-warning' : 'mt-1 text-xs text-foreground'}>
                    {warned && <TriangleAlert aria-hidden className="mt-px size-3.5 shrink-0" />}
                    <span className="wrap-break-word">{status}</span>
                  </p>
                  {misfits(gpuFlavor, row.vms).map((text) => (
                    <p key={text} className="mt-1 flex items-start gap-1 text-xs text-status-warning">
                      <TriangleAlert aria-hidden className="mt-px size-3.5 shrink-0" />
                      <span className="wrap-break-word">{text}</span>
                    </p>
                  ))}
                </li>
              );
            })}
          </ul>
          <p className="text-sm">
            <a
              href={`#${idFor('flavors')}`}
              className="inline-block py-1 text-foreground underline underline-offset-4"
              onClick={(e) => {
                e.preventDefault();
                onEditFlavorMappings();
              }}
            >
              Edit flavor mappings
            </a>
          </p>
        </>
      )}
    </fieldset>
  );
}

function VmwareRows({
  idFor,
  vms,
  offered,
  offeredNames,
  destination,
  mappings,
  onChange,
}: {
  idFor: (key: string) => string;
  vms: readonly GpuVm[];
  offered: OfferedGpuFlavors;
  offeredNames: ReadonlySet<string>;
  destination: string;
  mappings: Readonly<Record<string, string>>;
  onChange: (key: string, flavor: string | null) => void;
}) {
  const { rows, keyless } = gpuKeyRows(vms);
  return (
    <>
      {rows.length > 0 && (
        <div className="grid gap-3 sm:grid-cols-2">
          {rows.map((row, index) => {
            const value = Object.hasOwn(mappings, row.key) ? mappings[row.key]! : '';
            const chosen = offered.flavors.find((f) => f.name === value);
            // a flavor the plan maps that is not one of the destination's GPU flavors stays selected, marked so
            const stray = value && !offeredNames.has(value) ? value : null;
            const marks = [
              ...(stray && offered.inventory ? [`${stray} is not a GPU flavor of ${destination}: validation blocks these VMs until their GPU is mapped to one.`] : []),
              ...misfits(chosen, row.vms),
            ];
            return (
              <SelectField
                key={row.key}
                id={idFor(`gpu-${index}`)}
                label={`${row.key}: ${vmsSizeText(row.vms)}`}
                value={value}
                onChange={(e) => onChange(row.key, e.target.value || null)}
                options={[
                  { value: '', label: 'None' },
                  ...offered.flavors.map((f) => ({ value: f.name, label: gpuFlavorText(f) })),
                  ...(stray ? [{ value: stray, label: offered.inventory ? `${stray}, not a GPU flavor of ${destination}` : stray }] : []),
                ]}
                hint={
                  <>
                    <span className="block wrap-break-word">{row.vms.map((vm) => vm.name).join(', ')}</span>
                    {marks.map((text) => (
                      <span key={text} className="mt-0.5 flex items-start gap-1 text-status-warning">
                        <TriangleAlert aria-hidden className="mt-px size-3.5 shrink-0" />
                        <span className="wrap-break-word">{text}</span>
                      </span>
                    ))}
                  </>
                }
              />
            );
          })}
        </div>
      )}
      {keyless.length > 0 && (
        <ul className="flex flex-col gap-1">
          {keyless.map(({ vm, device }, i) => (
            // no GPU key names it, so no mapping can take it (SDD §22.2, §22.4, VM_PCI_PASSTHROUGH)
            <li key={`${vm.source_id}-${i}`} className="flex items-start gap-2 text-sm text-status-danger">
              <Ban aria-hidden className="mt-0.5 size-4 shrink-0" />
              <span className="wrap-break-word">
                {vm.name}: {gpuLabel(device)}. No mapping can take {device.kind === 'vgpu' ? 'a vGPU without a profile' : 'a device without ids'}: validation blocks the VM.
              </span>
            </li>
          ))}
        </ul>
      )}
      {rows.length === 0 && keyless.length === 0 && (
        <p className="flex items-center gap-1 text-sm text-muted-foreground">
          <CircleAlert aria-hidden className="size-4" />
          No GPU of the selected VMs has a key to map.
        </p>
      )}
    </>
  );
}
