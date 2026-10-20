/**
 * A flavor's GPU requests and a VM's GPUs as the control plane reads them (planning/gpu.py `gpu_requests` and `vm_gpus`,
 * SDD §22.2): the mock's one port of them, which its eligibility, probe suggestions, waves and GPU mapping rule read. Both
 * suites test it against one case table, `seamless/tests/fixtures/gpu_request_cases.json` (PR #64 review).
 */
import type { GpuDevice, VMRef } from '../api/types';
import { byCodePoint, pyInt, pyStrip } from './format';

/** Nova's resource class of a vGPU. */
const VGPU = 'VGPU';
/** The flavor extra spec that asks for PCI devices by alias: <alias>:<n>[,<alias>:<n>...]. */
const PCI_ALIAS = 'pci_passthrough:alias';
/** A resource request, bare or in one of Nova's request groups: resources<suffix>:<class>. */
const RESOURCES = /^resources([A-Za-z0-9_-]*):([A-Z0-9_]+)$/;
/** A request group's trait, set to required or forbidden: trait<suffix>:<name> (Python's `.` takes all but a newline). */
const TRAIT = /^trait([A-Za-z0-9_-]*):([^\n]+)$/;

/** A GpuDevice with the fields the API's model fills when a request leaves them out. */
function device(fields: Partial<GpuDevice> & Pick<GpuDevice, 'kind'>): GpuDevice {
  return { profile: null, vendor_id: null, device_id: null, count: 1, resource_class: null, traits: [], forbidden_traits: [], same_provider: false, alias: null, ...fields };
}

/** A resource amount as Nova's RequestGroup.add_resource reads it, 0 for none: a negative or unreadable amount is skipped,
 * and a zero takes the resource out of the request (the API's _amount). */
function amount(value: string): number {
  const n = pyInt(value);
  return n !== null && n > 0 ? n : 0;
}

/** The aliases of `pci_passthrough:alias` with their counts, as Nova asks for int(count) devices: a zero or negative count
 * asks for none; a count left out, or one int() cannot read, reads as 1 (the API's _aliases). */
function aliases(value: string | undefined): Array<[string, number]> {
  const out: Array<[string, number]> = [];
  for (const item of (value || '').split(',')) {
    const at = item.indexOf(':');
    const name = pyStrip(at < 0 ? item : item.slice(0, at));
    if (!name) continue;
    const count = pyInt(at < 0 ? '' : item.slice(at + 1));
    if (count === null || count > 0) out.push([name, count ?? 1]);
  }
  return out;
}

/**
 * The GPU requests of a flavor's extra specs, as the API's gpu_requests reads them (SDD §22.2): each request group's VGPU
 * with the group's required and forbidden traits, in suffix order (the unnumbered group first, a numbered one
 * same_provider), then each PCI alias. A CUSTOM_ class is a vGPU only with a declared profile, which each vGPU entry takes
 * - or each alias entry, on a flavor whose GPU requests are aliases alone.
 */
export function gpuRequests(specs: Record<string, string>, declaredProfile: string | null = null): GpuDevice[] {
  const classes = new Map<string, Array<[string, number]>>();
  const traits = new Map<string, { required: string[]; forbidden: string[] }>();
  for (const [key, value] of Object.entries(specs)) {
    const resource = RESOURCES.exec(key);
    const trait = resource ? null : TRAIT.exec(key);
    if (resource) classes.set(resource[1]!, [...(classes.get(resource[1]!) ?? []), [resource[2]!, amount(String(value))]]);
    else if (trait && (value === 'required' || value === 'forbidden')) {
      const group = traits.get(trait[1]!) ?? { required: [], forbidden: [] };
      group[value].push(trait[2]!);
      traits.set(trait[1]!, group);
    }
  }
  const vgpus = [...classes.keys()].sort(byCodePoint).flatMap((suffix) =>
    classes
      .get(suffix)!
      .sort(([a], [b]) => byCodePoint(a, b))
      .filter(([cls, n]) => n > 0 && (cls === VGPU || (Boolean(declaredProfile) && cls.startsWith('CUSTOM_'))))
      .map(([cls, n]) =>
        device({
          kind: 'vgpu',
          profile: declaredProfile,
          count: n,
          resource_class: cls,
          traits: [...(traits.get(suffix)?.required ?? [])].sort(byCodePoint),
          forbidden_traits: [...(traits.get(suffix)?.forbidden ?? [])].sort(byCodePoint),
          same_provider: suffix !== '',
        }),
      ),
  );
  const aliasProfile = vgpus.length ? null : declaredProfile;
  return [...vgpus, ...aliases(specs[PCI_ALIAS]).map(([alias, count]) => device({ kind: 'passthrough', alias, count, profile: aliasProfile }))];
}

/** The extra-spec keys whose requests gpuRequests reads as vGPUs, in code-point order: what §9.3's VM_VGPU names for an
 * OpenStack flavor - a key whose amount asks for none is not one (the API's vgpu_request_keys, SDD §9.3, §22.2). */
export function vgpuRequestKeys(specs: Record<string, string>, declaredProfile: string | null = null): string[] {
  return Object.entries(specs)
    .filter(([key, value]) => {
      const resource = RESOURCES.exec(key);
      return resource !== null && amount(String(value)) > 0 && (resource[2] === VGPU || (Boolean(declaredProfile) && resource[2]!.startsWith('CUSTOM_')));
    })
    .map(([key]) => key)
    .sort(byCodePoint);
}

/** The VM's GPUs, as the API's vm_gpus reads them (SDD §22.2): as its source reported them, else - a VM recorded before
 * 0.3.0 - what its flavor's extra specs ask for, with the profile its source declares for the flavor. */
export function vmGpus(vm: VMRef, declaredProfile: string | null = null): GpuDevice[] {
  if (vm.gpus?.length) return [...vm.gpus];
  const specs = vm.flavor_extra_specs ?? {};
  if (!Object.keys(specs).some((key) => key.startsWith('resources') || key.startsWith(PCI_ALIAS))) return [];
  return gpuRequests(specs, declaredProfile);
}
