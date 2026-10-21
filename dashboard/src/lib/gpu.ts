/**
 * A flavor's GPU requests and a VM's GPUs as the control plane reads them (planning/gpu.py `gpu_requests` and `vm_gpus`,
 * SDD §22.2): the mock's one port of them, which its eligibility, probe suggestions, waves and GPU mapping rule read, and
 * the dashboard's GPU column too. Both suites test it against one case table,
 * `seamless/tests/fixtures/gpu_request_cases.json` (PR #64 review). Then the rest of planning/gpu.py the mock's pre-flight
 * and cutover gate read - the GPU key, the GPU flavor rule, profile sizes, pool names and units - and the room rule they
 * share (planning/preflight.gpu_shortfalls), which the shared finding cases hold to the API's (eligibility_cases.json,
 * plan task G12). Below them, how the dashboard words GPUs, their mappings and a cutover's wait for them (SDD §16).
 */
import { GPU_CODES } from '../api/findingCatalog';
import type { DestinationInventory, GpuCapacity, GpuCheckReport, GpuDevice, GpuFlavor, GpuPool, GpuWait, Migration, Plan, Provider, ProviderInventory, ProviderKind, VMRef } from '../api/types';
import { byCodePoint, formatNumber, pyInt, pyStrip } from './format';

/** Nova's resource class of a vGPU. */
const VGPU = 'VGPU';
/** The flavor extra spec that asks for PCI devices by alias: <alias>:<n>[,<alias>:<n>...]. */
const PCI_ALIAS = 'pci_passthrough:alias';
/** A resource request, bare or in one of Nova's request groups: resources<suffix>:<class>. */
const RESOURCES = /^resources([A-Za-z0-9_-]*):([A-Z0-9_]+)$/;
/** A request group's trait, set to required or forbidden: trait<suffix>:<name> (Python's `.` takes all but a newline). */
const TRAIT = /^trait([A-Za-z0-9_-]*):([^\n]+)$/;

/** A GpuDevice with the fields the API's model fills when a request leaves them out. */
export function gpuDevice(fields: Partial<GpuDevice> & Pick<GpuDevice, 'kind'>): GpuDevice {
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
        gpuDevice({
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
  return [...vgpus, ...aliases(specs[PCI_ALIAS]).map(([alias, count]) => gpuDevice({ kind: 'passthrough', alias, count, profile: aliasProfile }))];
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
export function vmGpus(vm: Pick<VMRef, 'gpus' | 'flavor_extra_specs'>, declaredProfile: string | null = null): GpuDevice[] {
  if (vm.gpus?.length) return [...vm.gpus];
  const specs = vm.flavor_extra_specs ?? {};
  if (!Object.keys(specs).some((key) => key.startsWith('resources') || key.startsWith(PCI_ALIAS))) return [];
  return gpuRequests(specs, declaredProfile);
}

/** The profile its source declares for a VM's flavor (`gpu_profiles`, SDD §22.3), by own key: null without one. */
export function declaredProfileOf(vm: Pick<VMRef, 'flavor'>, profiles: Readonly<Record<string, string>> | undefined): string | null {
  return vm.flavor && profiles && Object.hasOwn(profiles, vm.flavor) ? profiles[vm.flavor]! : null;
}

/** What a plan's GPU mapping names a VMware device by: a vGPU's profile, a passthrough device's `<vendor_id>:<device_id>`;
 * null when it has neither (the API's gpu_key, SDD §22.2, §22.4). */
export function gpuKey(device: GpuDevice): string | null {
  if (device.kind === 'vgpu') return device.profile || null;
  return device.vendor_id && device.device_id ? `${device.vendor_id}:${device.device_id}` : null;
}

/**
 * A GPU VM's GPU flavor as its plan names it, the API's gpu_flavor (SDD §22.4): for a VMware VM the one flavor
 * `mappings.gpus` maps each of its GPU keys to - null when a device has no key, a key no mapping names, or its keys map
 * to different flavors -; for an OpenStack VM the explicit mapping of its flavor. Never a same-named or matched flavor;
 * null for a VM without a GPU, its GPUs read with the profile its source declares for its flavor.
 */
export function gpuFlavorOf(
  vm: Pick<VMRef, 'gpus' | 'flavor_extra_specs' | 'flavor'>,
  plan: Pick<Plan, 'mappings'>,
  sourceKind: ProviderKind,
  declaredProfile: string | null = null,
): string | null {
  const gpus = vmGpus(vm, declaredProfile);
  if (!gpus.length) return null;
  if (sourceKind !== 'vmware') return vm.flavor && Object.hasOwn(plan.mappings.flavors, vm.flavor) ? plan.mappings.flavors[vm.flavor]! : null;
  const mapped = plan.mappings.gpus ?? {};
  const named = new Set(gpus.map((g) => {
    const key = gpuKey(g);
    return key !== null && Object.hasOwn(mapped, key) ? mapped[key]! : null;
  }));
  return named.size === 1 ? ([...named][0] ?? null) : null;
}

/** What an NVIDIA vGPU profile's name says of its size (SDD §22.2): `grid_a100-3-20c` is an A100's 3 MIG slices with 20 GB
 * of framebuffer, series C. */
export interface ProfileSize {
  gpu: string;
  slices: number | null;
  framebuffer_gb: number;
  series: string;
}

/** NVIDIA's profile names, with or without vCenter's grid_/nvidia_ prefix or the "GRID " of the mediated device types, read
 * case-insensitively as Python's re.IGNORECASE reads them (simple case folding: the `u` flag) - the API's _PROFILE. */
const PROFILE = /^(?:grid_|nvidia_|grid )?([a-z0-9]+)-(?:([1-9][0-9]?)-)?([1-9][0-9]*)([abcq])$/iu;

/** The size an NVIDIA vGPU profile's name gives, the API's vgpu_profile_size (SDD §22.2); null for any other name - an
 * mdev type id such as `nvidia-471` among them. */
export function vgpuProfileSize(name: string): ProfileSize | null {
  const match = PROFILE.exec(name);
  if (!match) return null;
  return { gpu: match[1]!.toUpperCase(), slices: match[2] ? Number(match[2]) : null, framebuffer_gb: Number(match[3]), series: match[4]!.toUpperCase() };
}

/** A pool's name as the cutover gate's wait gives it, the API's pool_name (SDD §10, §22.7): the class, the required traits
 * sorted, then the forbidden ones sorted, each prefixed with "!" - "VGPU CUSTOM_NVIDIA_A100_4C". */
export function poolName(resourceClass: string, traits: readonly string[] = [], forbidden: readonly string[] = []): string {
  return [resourceClass, ...[...traits].sort(byCodePoint), ...[...forbidden].sort(byCodePoint).map((t) => `!${t}`)].join(' ');
}

/** A request's pool, as poolKey tells it from another: what the room rule counts units by (SDD §22.3). */
export function requestPoolKey(device: Pick<GpuDevice, 'resource_class' | 'traits' | 'forbidden_traits' | 'same_provider'>): string {
  return poolKey(device.resource_class ?? '', device.traits, device.forbidden_traits, Boolean(device.same_provider));
}

/** A class alone's pool, which unites the class's pools (SDD §22.3). */
export function classPoolKey(resourceClass: string): string {
  return poolKey(resourceClass);
}

/**
 * The units `requests` ask of each pool, the API's gpu_units (SDD §22.3, §22.5, §22.7): a vGPU request's count in its own
 * pool and in its class's, which unites the class's pools - once, for a request without traits, whose pool its class's
 * is. A PCI alias request asks of no pool.
 */
export function gpuUnits(requests: Iterable<GpuDevice>): Map<string, number> {
  const units = new Map<string, number>();
  for (const device of requests) {
    if (device.kind !== 'vgpu' || !device.resource_class) continue;
    for (const key of new Set([requestPoolKey(device), classPoolKey(device.resource_class)])) units.set(key, (units.get(key) ?? 0) + device.count);
  }
  return units;
}

/**
 * Where vGPU requests find no room in a capacity read (SDD §22.3, §22.5, §22.7): `kind` "refused" - Placement refused the
 * pool's request, its `detail` -, "pool" - the units asked of it pass its free -, "class" - across its class's pools - or
 * "group" - no provider has the `needed` units a request group takes from one, `free` the most on one.
 */
export interface Shortfall {
  key: string;
  /** the pool by poolName, or a class alone */
  pool: string;
  kind: 'refused' | 'pool' | 'class' | 'group';
  needed: number;
  free: number;
  detail: string | null;
}

/**
 * Where the vGPU requests among `requests` find no room in the read's `pools` when `asked` gives the units asked of each
 * pool (gpuUnits): the pre-flight's rule and the cutover gate's, the API's gpu_shortfalls (SDD §22.5, §22.7), in the
 * order the pre-flight words it - each pool they ask of that Placement refuses or whose free is below what is asked of it;
 * each class across its pools, unless a request without traits asks of the class's pool, whose check is its own; then each
 * request group of n > 1 units no provider holds. A pool the read does not hold is skipped: nothing is guessed.
 */
export function gpuShortfalls(requests: Iterable<GpuDevice>, asked: ReadonlyMap<string, number>, pools: Iterable<GpuPool>): Shortfall[] {
  const vgpus = [...requests].filter((g) => g.kind === 'vgpu' && g.resource_class);
  const read = new Map([...pools].map((p) => [poolKey(p.resource_class, p.traits, p.forbidden_traits, p.same_provider), p]));
  const keys = [...new Set(vgpus.map(requestPoolKey))];
  const out: Shortfall[] = [];
  for (const key of keys) {
    const pool = read.get(key);
    if (!pool) continue;
    const name = poolName(pool.resource_class, pool.traits, pool.forbidden_traits);
    if (pool.detail !== null && pool.detail !== undefined) out.push({ key, pool: name, kind: 'refused', needed: asked.get(key) ?? 0, free: pool.free, detail: pool.detail });
    else if ((asked.get(key) ?? 0) > pool.free) out.push({ key, pool: name, kind: 'pool', needed: asked.get(key)!, free: pool.free, detail: null });
  }
  for (const cls of new Set(vgpus.map((g) => g.resource_class!))) {
    const key = classPoolKey(cls);
    const alone = read.get(key);
    // a request without traits asks its class's pool: that check was its own
    if (!alone || (alone.detail !== null && alone.detail !== undefined) || keys.includes(key)) continue;
    if ((asked.get(key) ?? 0) > alone.free) out.push({ key, pool: cls, kind: 'class', needed: asked.get(key)!, free: alone.free, detail: null });
  }
  for (const device of vgpus) {
    const key = requestPoolKey(device);
    const pool = read.get(key);
    // a group of n > 1 units needs one provider with n free (SDD §22.3, §22.5)
    if (pool && (pool.detail === null || pool.detail === undefined) && device.count > 1 && device.count > pool.largest_free) {
      out.push({ key, pool: poolName(pool.resource_class, pool.traits, pool.forbidden_traits), kind: 'group', needed: device.count, free: pool.largest_free, detail: null });
    }
  }
  return out;
}

/** The root disk size a flavor must give a VM: an image-booted or ephemeral root's, else 0 - the API's _local_root_gb, its
 * root the first bootable disk, else the first (SDD §9.3). */
export function localRootGb(vm: Pick<VMRef, 'disks'>): number {
  const root = (vm.disks ?? []).find((d) => d.bootable) ?? vm.disks?.[0];
  return root && (root.kind === 'image_root' || root.kind === 'ephemeral') ? root.size_gb : 0;
}

/** What a GPU flavor gives a VM less of than the VM has, as `MAP_GPU_FLAVOR` warns (SDD §22.4, §22.5): vCPUs, RAM in MiB
 * and local root disk in GiB, each what the flavor gives and what the VM has; empty when it fits. */
export function flavorShortfalls(
  flavor: Pick<GpuFlavor, 'vcpus' | 'ram_mb' | 'disk_gb'>,
  vm: Pick<VMRef, 'vcpus' | 'ram_mb' | 'disks'>,
): Array<{ what: 'vcpus' | 'ram_mb' | 'root_gb'; gives: number; has: number }> {
  const root = localRootGb(vm);
  const pairs: Array<{ what: 'vcpus' | 'ram_mb' | 'root_gb'; gives: number; has: number }> = [
    { what: 'vcpus', gives: flavor.vcpus || 0, has: vm.vcpus },
    { what: 'ram_mb', gives: flavor.ram_mb || 0, has: vm.ram_mb },
    { what: 'root_gb', gives: flavor.disk_gb || 0, has: root },
  ];
  return pairs.filter((p) => p.gives < p.has);
}

// ---------------------------------------------------------------------------------------------
// How the dashboard words a VM's GPUs, a flavor's GPU requests and a destination's GPU capacity (SDD §16, §22.11)
// ---------------------------------------------------------------------------------------------

/** What an OpenStack request asks for: its class and traits, a forbidden one prefixed with "!", or its PCI alias. */
function askedFor(device: GpuDevice): string | null {
  if (device.resource_class) return [device.resource_class, ...(device.traits ?? []), ...(device.forbidden_traits ?? []).map((t) => `!${t}`)].join(', ');
  return device.alias ? `alias ${device.alias}` : null;
}

/**
 * A GPU as the Inventory's GPU column words it (SDD §16): its kind, then its profile or PCI ids, "×n" for n identical
 * ones, and for an OpenStack request " · " and what it asks for - "vGPU grid_a100-4c ×2", "Passthrough 10de:20b5",
 * "vGPU · VGPU, CUSTOM_NVIDIA_A100_4C", "Passthrough · alias a100". An OpenStack request's declared profile stands after
 * its kind; `declared: false` leaves it out where the profile is shown beside it.
 */
export function gpuLabel(device: GpuDevice, { declared = true }: { declared?: boolean } = {}): string {
  const request = askedFor(device);
  const ids = device.vendor_id && device.device_id ? `${device.vendor_id}:${device.device_id}` : null;
  // a VMware device is named by its own profile or its PCI ids; an OpenStack request by what it asks for
  const name =
    request !== null ? (declared ? device.profile : null) : device.kind === 'vgpu' ? device.profile || 'without a profile' : (ids ?? 'without device ids');
  const head = [device.kind === 'vgpu' ? 'vGPU' : 'Passthrough', name, device.count > 1 ? `×${device.count}` : null].filter(Boolean).join(' ');
  return request === null ? head : `${head} · ${request}`;
}

/** The vGPU profiles a destination's GPU flavor gives, as its provider declares them (SDD §22.3): none without a declaration. */
export function declaredProfiles(flavor: GpuFlavor): string[] {
  return [...new Set(flavor.gpus.map((g) => g.profile).filter((p): p is string => Boolean(p)))];
}

/**
 * What tells a pool from another, as the API's pool_key (SDD §22.3): its class, its required and forbidden traits, and
 * whether they are a numbered group's - a request without traits is its class's pool.
 */
function poolKey(resourceClass: string, traits: readonly string[] = [], forbidden: readonly string[] = [], sameProvider = false): string {
  const required = [...traits].sort(byCodePoint);
  const refused = [...forbidden].sort(byCodePoint);
  return JSON.stringify([resourceClass, required, refused, sameProvider && required.length + refused.length > 0]);
}

/** What a capacity read asks Placement of: a pool without its counts, as the API's PoolRequest (SDD §22.3). */
export type PoolRequest = Pick<GpuPool, 'resource_class' | 'traits' | 'forbidden_traits' | 'same_provider'>;

/** Whether two pools are one, as the API's pool_key tells them apart (SDD §22.3). */
export function samePool(a: PoolRequest, b: PoolRequest): boolean {
  return poolKey(a.resource_class, a.traits, a.forbidden_traits, a.same_provider) === poolKey(b.resource_class, b.traits, b.forbidden_traits, b.same_provider);
}

/**
 * The pools of the vGPU requests among `requests`, as the API's gpu_pools (SDD §22.3): one per distinct request with traits
 * - its class with its required and forbidden traits, sorted, and whether its group is a numbered one -, in the order they
 * come, then one per resource class alone, which unites that class's pools and is the pool of a request without traits.
 * A PCI alias request has none: the device class its alias asks for lives in nova.conf.
 */
export function gpuPools(requests: Iterable<GpuDevice>): PoolRequest[] {
  const pools = new Map<string, PoolRequest>();
  const add = (pool: PoolRequest) => {
    const key = poolKey(pool.resource_class, pool.traits, pool.forbidden_traits, pool.same_provider);
    if (!pools.has(key)) pools.set(key, pool);
  };
  for (const g of requests) {
    if (g.kind !== 'vgpu' || !g.resource_class) continue;
    const traits = [...(g.traits ?? [])].sort(byCodePoint);
    const forbidden = [...(g.forbidden_traits ?? [])].sort(byCodePoint);
    add({ resource_class: g.resource_class, traits, forbidden_traits: forbidden, same_provider: Boolean(g.same_provider) && traits.length + forbidden.length > 0 });
  }
  for (const resourceClass of new Set([...pools.values()].map((p) => p.resource_class))) {
    add({ resource_class: resourceClass, traits: [], forbidden_traits: [], same_provider: false });
  }
  return [...pools.values()];
}

/** A pool as the Inventory names it: its class and traits, a forbidden one prefixed with "!" (SDD §16). */
export function poolLabel(pool: GpuPool): string {
  return [pool.resource_class, ...pool.traits, ...pool.forbidden_traits.map((t) => `!${t}`)].join(', ');
}

/**
 * The GPU flavors that ask of a pool, as the pre-flight counts them (SDD §22.5): a vGPU request asks of its own pool and
 * of its class's, which unites the class's pools.
 */
export function flavorsOfPool(pool: GpuPool, flavors: readonly GpuFlavor[]): GpuFlavor[] {
  const key = poolKey(pool.resource_class, pool.traits, pool.forbidden_traits, pool.same_provider);
  return flavors.filter((f) =>
    f.gpus.some(
      (g) => g.kind === 'vgpu' && g.resource_class && (poolKey(g.resource_class, g.traits, g.forbidden_traits, g.same_provider) === key || poolKey(g.resource_class) === key),
    ),
  );
}

/**
 * A pool's free units and the providers that hold them, as the Inventory words them (SDD §16): "4 free on 1 GPU", the
 * most free on one past one provider; a pool Placement refused as a bad request names its detail.
 */
export function poolFreeText(pool: GpuPool): string {
  if (pool.detail) return `Refused by Placement (${pool.detail}): no server is created with its flavors`;
  if (pool.free <= 0) return 'None free';
  const on = `${pool.free} free on ${pool.providers} GPU${pool.providers === 1 ? '' : 's'}`;
  return pool.providers > 1 ? `${on}, at most ${pool.largest_free} on one` : on;
}

/**
 * Why a destination's GPU capacity was not read, in words (SDD §16, §22.3): the role Placement asks for, no Placement,
 * or the failure, each with its detail - the HTTP status and Placement's error code, never its text (§22.12).
 */
export function gpuCapacityReason(capacity: GpuCapacity): string {
  const detail = capacity.detail ? ` (${capacity.detail})` : '';
  if (capacity.reason === 'forbidden') return `the destination credential lacks the role Placement asks for, admin by default${detail}`;
  if (capacity.reason === 'no_placement') return `the destination's catalog lists no Placement${detail}`;
  return `the read failed${detail}`;
}

// ---------------------------------------------------------------------------------------------
// A destination card's GPU capacity and the provider form's vGPU profiles (SDD §16, §22.3, §22.11; plan task G11)
// ---------------------------------------------------------------------------------------------

/**
 * A destination's GPU capacity as its check report holds it (`gpu_capacity`, SDD §10, §22.3), or null: a source's report,
 * one from before 0.3.0 and one a PATCH reset (§12) hold none.
 */
export function checkedGpuCapacity(report: Record<string, unknown>): GpuCapacity | null {
  const capacity: unknown = (report as GpuCheckReport).gpu_capacity;
  if (!capacity || typeof capacity !== 'object') return null;
  const { status, pools } = capacity as Partial<GpuCapacity>;
  return (status === 'ok' || status === 'unknown') && Array.isArray(pools) ? (capacity as GpuCapacity) : null;
}

/**
 * A provider's GPU flavors as its inventory lists them, by name in code-point order (SDD §16, §22.3): a destination's
 * `gpu_flavors` - from an answer without them, its flavors that ask for a GPU as §22.2 reads them -, a source's the
 * flavors of its VMs with a GPU (`vm_gpus`), each read with the profile the provider declares for it.
 */
export function inventoryGpuFlavors(inventory: ProviderInventory, declared: Readonly<Record<string, string>> = {}): string[] {
  const profile = (flavor: string) => (Object.hasOwn(declared, flavor) ? declared[flavor]! : null);
  const names = Array.isArray(inventory)
    ? inventory.flatMap((vm) => (vm.flavor && vmGpus(vm, profile(vm.flavor)).length ? [vm.flavor] : []))
    : (inventory.gpu_flavors ?? inventory.flavors.filter((f) => gpuRequests(f.extra_specs, profile(f.name)).length)).map((f) => f.name);
  return [...new Set(names)].sort(byCodePoint);
}

// ---------------------------------------------------------------------------------------------
// The plan form's GPU mappings (SDD §16, §22.4, §22.11; plan task G12)
// ---------------------------------------------------------------------------------------------

/**
 * A destination's GPU flavors as its inventory lists them (`gpu_flavors`, SDD §22.3) - from an answer without them, its
 * flavors that ask for a GPU, read with the profiles it declares.
 */
export function destinationGpuFlavors(inventory: DestinationInventory, declared: Readonly<Record<string, string>> = {}): GpuFlavor[] {
  if (inventory.gpu_flavors) return inventory.gpu_flavors;
  return inventory.flavors.flatMap(({ name, vcpus, ram_mb, disk_gb, extra_specs }) => {
    const gpus = gpuRequests(extra_specs, Object.hasOwn(declared, name) ? declared[name]! : null);
    return gpus.length ? [{ name, vcpus, ram_mb, disk_gb, gpus }] : [];
  });
}

/** A selected VM with its GPUs, read with the profile its source declares for its flavor (SDD §22.2). */
export interface GpuVm {
  vm: VMRef;
  gpus: GpuDevice[];
}

/** One row per GPU key among the selected VMs, by key, and the devices no GPU key names (SDD §22.2, §22.4). */
export function gpuKeyRows(vms: readonly GpuVm[]): { rows: Array<{ key: string; vms: VMRef[] }>; keyless: Array<{ vm: VMRef; device: GpuDevice }> } {
  const byKey = new Map<string, VMRef[]>();
  const keyless: Array<{ vm: VMRef; device: GpuDevice }> = [];
  for (const { vm, gpus } of vms) {
    for (const device of gpus) {
      const key = gpuKey(device);
      if (key === null) keyless.push({ vm, device });
      else if (!byKey.get(key)?.includes(vm)) byKey.set(key, [...(byKey.get(key) ?? []), vm]);
    }
  }
  const rows = [...byKey.entries()].sort(([a], [b]) => byCodePoint(a, b)).map(([key, list]) => ({ key, vms: list }));
  return { rows, keyless };
}

/** The OpenStack flavors among the selected VMs that ask for a GPU, by flavor, with their VMs and what they ask for (SDD §22.4). */
export function gpuFlavorRows(vms: readonly GpuVm[]): Array<{ flavor: string; vms: VMRef[]; gpus: GpuDevice[] }> {
  const byFlavor = new Map<string, { vms: VMRef[]; gpus: GpuDevice[] }>();
  for (const { vm, gpus } of vms) {
    const flavor = vm.flavor ?? '';
    const row = byFlavor.get(flavor) ?? { vms: [], gpus };
    row.vms.push(vm);
    byFlavor.set(flavor, row);
  }
  return [...byFlavor.entries()].sort(([a], [b]) => byCodePoint(a, b)).map(([flavor, row]) => ({ flavor, ...row }));
}

/** MiB of RAM in GiB, as the form words a size: "32 GiB", "1.5 GiB" (SDD §16 sizes). */
export function ramGib(ramMb: number): string {
  return `${formatNumber(ramMb / 1024, 1)} GiB`;
}

/** A GPU flavor with its size and its GPU requests as the Inventory words them (SDD §16): "g1.a100-4c: 8 vCPUs, 32 GiB RAM,
 * 200 GiB disk, vGPU A100-4C · VGPU, CUSTOM_NVIDIA_A100_4C". */
export function gpuFlavorText(flavor: GpuFlavor): string {
  return `${flavor.name}: ${flavor.vcpus} vCPUs, ${ramGib(flavor.ram_mb)} RAM, ${flavor.disk_gb} GiB disk, ${flavor.gpus.map((g) => gpuLabel(g)).join('; ')}`;
}

/** What a GPU flavor gives a VM less of than it has, in words, as `MAP_GPU_FLAVOR` warns: "8 vCPUs for its 16 and 32 GiB RAM
 * for its 64 GiB"; null when it fits (SDD §22.4). */
export function misfitText(flavor: Pick<GpuFlavor, 'vcpus' | 'ram_mb' | 'disk_gb'>, vm: Pick<VMRef, 'vcpus' | 'ram_mb' | 'disks'>): string | null {
  const parts = flavorShortfalls(flavor, vm).map(({ what, gives, has }) =>
    what === 'vcpus' ? `${gives} vCPUs for its ${has}` : what === 'ram_mb' ? `${ramGib(gives)} RAM for its ${ramGib(has)}` : `${gives} GiB of root disk for its ${has} GiB`,
  );
  if (!parts.length) return null;
  return parts.length === 1 ? parts[0]! : `${parts.slice(0, -1).join(', ')} and ${parts.at(-1)}`;
}

/** The VMs' sizes as a GPU mapping row names them (SDD §16): "2 VMs, 8–16 vCPUs, 32–64 GiB". */
export function vmsSizeText(vms: ReadonlyArray<Pick<VMRef, 'vcpus' | 'ram_mb'>>): string {
  const range = (values: number[], word: (n: number) => string) => {
    const low = Math.min(...values);
    const high = Math.max(...values);
    return low === high ? word(low) : `${word(low).replace(/ .*$/, '')}–${word(high)}`;
  };
  return `${vms.length} VM${vms.length === 1 ? '' : 's'}, ${range(vms.map((v) => v.vcpus), (n) => `${n} vCPUs`)}, ${range(vms.map((v) => v.ram_mb), ramGib)}`;
}

// ---------------------------------------------------------------------------------------------
// The migration page's GPU panel (SDD §16, §22.11; plan task G12)
// ---------------------------------------------------------------------------------------------

/** The verification's GPU checks (SDD §7.5, §22.8). */
export const GPU_CHECKS: ReadonlySet<string> = new Set(['gpu_flavor', 'gpu_allocation']);
/** Whether the migration page shows its GPU panel (SDD §16): a VM with a GPU, a GPU finding, a wait or a GPU check. */
export function hasGpuPanel(m: Pick<Migration, 'vm' | 'gpu_wait' | 'findings' | 'verification'>, source: Pick<Provider, 'gpu_profiles'> | undefined): boolean {
  return (
    vmGpus(m.vm, declaredProfileOf(m.vm, source?.gpu_profiles)).length > 0 ||
    m.gpu_wait !== null ||
    m.findings.some((f) => GPU_CODES.has(f.code)) ||
    Boolean(m.verification?.checks.some((c) => GPU_CHECKS.has(c.name)))
  );
}

// ---------------------------------------------------------------------------------------------
// A cutover's wait for GPU capacity, as the dashboard words it (SDD §16, §22.7, §22.11; plan task G12)
// ---------------------------------------------------------------------------------------------

/**
 * What Needs attention says of a wait (SDD §16): "Waiting for GPU capacity: {pool}, {free} free, needs {needed}" - after a
 * read that failed, whose `free` is null, its reason, which names the failure, in place of the numbers it could not read.
 */
export function gpuWaitSummary(wait: GpuWait): string {
  return wait.free === null ? `Waiting for GPU capacity: ${wait.reason}` : `Waiting for GPU capacity: ${wait.pool}, ${wait.free} free, needs ${wait.needed}`;
}

/** What it waits for, as the migration page words it (SDD §16): "{free} free of {needed} in {pool}", or after a failed read
 * its reason. */
export function gpuWaitWhat(wait: GpuWait): string {
  return wait.free === null ? wait.reason : `${wait.free} free of ${wait.needed} in ${wait.pool}`;
}

/** The migration page's announcement when a wait begins (SDD §16): "{vm} waits for GPU capacity: ... The source VM keeps running." */
export function gpuWaitBegins(vm: string, wait: GpuWait): string {
  return `${vm} waits for GPU capacity: ${gpuWaitWhat(wait)}. The source VM keeps running.`;
}

/** The migration page's announcement when a wait ends (SDD §16). */
export function gpuWaitEnds(vm: string): string {
  return `${vm} no longer waits for GPU capacity.`;
}

/** A row of the provider form's declared vGPU profiles: a flavor and the profile it gives, as typed (SDD §16). */
export interface VgpuProfileRow {
  /** Stable while the row lives: what its fields and their errors are keyed by. */
  key: number;
  flavor: string;
  profile: string;
}

/** What the API's model takes of a declared vGPU profile (SDD §4.2, §22.3): a flavor name of 1 to 255 characters and a profile of 1 to 64. */
const FLAVOR_MAX = 255;
const PROFILE_MAX = 64;

/**
 * What the API refuses of a form's declared vGPU profiles, and what one map cannot hold - a flavor declared twice -, in
 * the form's words (SDD §16): each row's flavor and profile, trimmed, counted in code points as Python counts them.
 */
export function vgpuProfileErrors(rows: readonly VgpuProfileRow[]): Array<{ key: number; field: 'flavor' | 'profile'; message: string }> {
  const errors: Array<{ key: number; field: 'flavor' | 'profile'; message: string }> = [];
  const seen = new Set<string>();
  rows.forEach(({ key, flavor: typedFlavor, profile: typedProfile }, i) => {
    const n = i + 1;
    const flavor = typedFlavor.trim();
    const profile = typedProfile.trim();
    if (!flavor) errors.push({ key, field: 'flavor', message: `Enter the flavor of vGPU profile ${n}, or remove it.` });
    else if ([...flavor].length > FLAVOR_MAX) errors.push({ key, field: 'flavor', message: `Shorten the flavor of vGPU profile ${n}: at most ${FLAVOR_MAX} characters.` });
    else if (seen.has(flavor)) errors.push({ key, field: 'flavor', message: `${flavor} is declared twice: change or remove vGPU profile ${n}.` });
    if (flavor) seen.add(flavor);
    if (!profile) errors.push({ key, field: 'profile', message: `Enter the profile of vGPU profile ${n}, such as A100-4C.` });
    else if ([...profile].length > PROFILE_MAX) errors.push({ key, field: 'profile', message: `Shorten the profile of vGPU profile ${n}: at most ${PROFILE_MAX} characters.` });
  });
  return errors;
}

/** The map a form's rows declare, each name trimmed: `gpu_profiles` as POST and PATCH /providers send it (SDD §12, §22.3). */
export function vgpuProfileMap(rows: readonly VgpuProfileRow[]): Record<string, string> {
  // own keys, never an assignment: a flavor named "__proto__" is a pair like any other (review RV23-01)
  return Object.fromEntries(rows.map((row) => [row.flavor.trim(), row.profile.trim()]));
}

/** Whether two declarations are one map, their pairs in any order: a PATCH that changes nothing resets the check report (SDD §12, §16). */
export function sameVgpuProfiles(a: Readonly<Record<string, string>>, b: Readonly<Record<string, string>>): boolean {
  const pairs = (map: Readonly<Record<string, string>>) => JSON.stringify(Object.entries(map).sort(([x], [y]) => byCodePoint(x, y)));
  return pairs(a) === pairs(b);
}
