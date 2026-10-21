import { describe, expect, it } from 'vitest';
// One case table for the control plane and the dashboard's mock (SDD §22.2, PR #64 review).
import raw from '../../../seamless/tests/fixtures/gpu_request_cases.json?raw';
import type { DestinationFlavor, DestinationInventory, GpuCapacity, GpuDevice, GpuFlavor, GpuPool, VMRef } from '../api/types';
import {
  checkedGpuCapacity,
  declaredProfiles,
  flavorsOfPool,
  gpuCapacityReason,
  gpuDevice,
  gpuLabel,
  gpuPools,
  gpuRequests,
  inventoryGpuFlavors,
  poolFreeText,
  poolLabel,
  samePool,
  sameVgpuProfiles,
  vgpuProfileErrors,
  vgpuProfileMap,
  vmGpus,
} from './gpu';

const fixture = JSON.parse(raw) as {
  cases: Array<{ why: string; extra_specs: Record<string, string>; declared_profile?: string; requests: Array<Partial<GpuDevice> & Pick<GpuDevice, 'kind'>> }>;
};

/** A GpuDevice's fields as the API's model fills them when a request leaves them out. */
const DEFAULTS = { profile: null, vendor_id: null, device_id: null, count: 1, resource_class: null, traits: [], forbidden_traits: [], same_provider: false, alias: null };

const vm = (fields: Partial<VMRef>) => ({ gpus: [], flavor_extra_specs: {}, ...fields }) as VMRef;

describe("a flavor's GPU requests in the mock, like the control plane (SDD §22.2)", () => {
  it.each(fixture.cases.map((c) => [c.why, c] as const))('%s', (_why, c) => {
    expect(gpuRequests(c.extra_specs, c.declared_profile ?? null)).toEqual(c.requests.map((r) => ({ ...DEFAULTS, ...r })));
  });

  it("reads a VM's GPUs as its source reports them, else - a VM recorded before 0.3.0 - its flavor's requests", () => {
    const reported: GpuDevice = { kind: 'passthrough', vendor_id: '10de', device_id: '20b5', count: 1 };
    expect(vmGpus(vm({ gpus: [reported], flavor_extra_specs: { 'resources:VGPU': '1' } }))).toEqual([reported]);
    expect(vmGpus(vm({ flavor_extra_specs: { 'pci_passthrough:alias': 'PCI device 0' } }))).toEqual([{ ...DEFAULTS, kind: 'passthrough', alias: 'PCI device 0' }]);
    expect(vmGpus(vm({ flavor_extra_specs: { 'resources:CUSTOM_A100': '1' } }), 'A100-4C')).toEqual([
      { ...DEFAULTS, kind: 'vgpu', profile: 'A100-4C', resource_class: 'CUSTOM_A100' },
    ]);
    expect(vmGpus(vm({ flavor_extra_specs: { 'resources:VGPU': '0', 'hw:cpu_policy': 'dedicated' } }))).toEqual([]);
    expect(vmGpus(vm({}))).toEqual([]);
  });
});

describe('how the dashboard words GPUs and GPU capacity (SDD §16, §22.11)', () => {
  const pool = (fields: Partial<GpuPool>): GpuPool => ({
    resource_class: 'VGPU',
    traits: [],
    forbidden_traits: [],
    same_provider: false,
    free: 0,
    providers: 0,
    largest_free: 0,
    detail: null,
    ...fields,
  });
  const flavor = (name: string, gpus: GpuDevice[]): GpuFlavor => ({ name, vcpus: 8, ram_mb: 32768, disk_gb: 80, gpus });

  it("words a VMware GPU by its profile or PCI ids, and an OpenStack request by what it asks for, its count past one", () => {
    expect(gpuLabel(gpuDevice({ kind: 'vgpu', profile: 'grid_a100-4c', count: 2 }))).toBe('vGPU grid_a100-4c ×2');
    expect(gpuLabel(gpuDevice({ kind: 'vgpu' }))).toBe('vGPU without a profile');
    expect(gpuLabel(gpuDevice({ kind: 'passthrough', vendor_id: '10de', device_id: '20b5', count: 3 }))).toBe('Passthrough 10de:20b5 ×3');
    expect(gpuLabel(gpuDevice({ kind: 'passthrough', vendor_id: '10de' }))).toBe('Passthrough without device ids');
    const [request] = gpuRequests({ 'resources1:VGPU': '2', 'trait1:CUSTOM_A': 'required', 'trait1:CUSTOM_B': 'forbidden' });
    expect(gpuLabel(request!)).toBe('vGPU ×2 · VGPU, CUSTOM_A, !CUSTOM_B');
    // a declared profile stands after the kind, unless it is shown beside the request
    const [declared] = gpuRequests({ 'resources:VGPU': '1' }, 'A100-4C');
    expect(gpuLabel(declared!)).toBe('vGPU A100-4C · VGPU');
    expect(gpuLabel(declared!, { declared: false })).toBe('vGPU · VGPU');
    expect(gpuRequests({ 'pci_passthrough:alias': 'a100:2' }).map((g) => gpuLabel(g))).toEqual(['Passthrough ×2 · alias a100']);
    expect(gpuRequests({ 'pci_passthrough:alias': 'a100:1' }, 'A100-4C').map((g) => gpuLabel(g))).toEqual(['Passthrough A100-4C · alias a100']);
  });

  it("names a pool by its class and traits, with the flavors that ask of it and the profiles they give", () => {
    const a = flavor('g1.a', gpuRequests({ 'resources:VGPU': '1', 'trait:CUSTOM_A': 'required' }, 'A100-4C'));
    const b = flavor('g1.b', gpuRequests({ 'resources1:VGPU': '1', 'trait1:CUSTOM_A': 'required' }));
    const bare = flavor('g1.bare', gpuRequests({ 'resources:VGPU': '1' }));
    const alias = flavor('p1.a', gpuRequests({ 'pci_passthrough:alias': 'a100:1' }));
    const flavors = [a, b, bare, alias];
    expect(poolLabel(pool({ traits: ['CUSTOM_A'], forbidden_traits: ['CUSTOM_B'] }))).toBe('VGPU, CUSTOM_A, !CUSTOM_B');
    // a numbered group's pool is another than the unnumbered group's with the same traits (§22.3)
    expect(flavorsOfPool(pool({ traits: ['CUSTOM_A'] }), flavors).map((f) => f.name)).toEqual(['g1.a']);
    expect(flavorsOfPool(pool({ traits: ['CUSTOM_A'], same_provider: true }), flavors).map((f) => f.name)).toEqual(['g1.b']);
    // the class alone unites its pools: every vGPU flavor of the class asks of it, never an alias flavor (§22.5)
    expect(flavorsOfPool(pool({}), flavors).map((f) => f.name)).toEqual(['g1.a', 'g1.b', 'g1.bare']);
    expect(flavorsOfPool(pool({ resource_class: 'CUSTOM_OTHER' }), flavors)).toEqual([]);
    expect(declaredProfiles(a)).toEqual(['A100-4C']);
    expect(declaredProfiles(b)).toEqual([]);
  });

  it("reads a capacity's pools in the API's order: each request with traits, then each class alone (SDD §22.3)", () => {
    const requests = [
      ...gpuRequests({ 'resources:VGPU': '1', 'trait:CUSTOM_B': 'required', 'trait:CUSTOM_A': 'required' }),
      ...gpuRequests({ 'resources1:VGPU': '1', 'trait1:CUSTOM_A': 'required', 'trait1:CUSTOM_B': 'required' }),
      ...gpuRequests({ 'resources:CUSTOM_X': '1' }, 'A16-4Q'),
      ...gpuRequests({ 'resources:VGPU': '2', 'trait:CUSTOM_A': 'required', 'trait:CUSTOM_B': 'required' }),
      ...gpuRequests({ 'pci_passthrough:alias': 'a100:1' }),
      ...gpuRequests({ 'resources:VGPU': '1', 'trait:CUSTOM_C': 'forbidden' }),
    ];
    // a numbered group's pool is its own; a request without traits is its class's pool, where it comes; an alias has none
    expect(gpuPools(requests).map((p) => [poolLabel({ ...p, free: 0, providers: 0, largest_free: 0, detail: null }), p.same_provider])).toEqual([
      ['VGPU, CUSTOM_A, CUSTOM_B', false],
      ['VGPU, CUSTOM_A, CUSTOM_B', true],
      ['CUSTOM_X', false],
      ['VGPU, !CUSTOM_C', false],
      ['VGPU', false],
    ]);
    expect(samePool(gpuPools(requests)[0]!, { resource_class: 'VGPU', traits: ['CUSTOM_B', 'CUSTOM_A'], forbidden_traits: [], same_provider: false })).toBe(true);
    expect(gpuPools(gpuRequests({ 'pci_passthrough:alias': 'a100:1' }))).toEqual([]);
  });

  it('says what a pool has free, on how many GPUs, and why a refused pool counts nothing', () => {
    expect(poolFreeText(pool({ free: 4, providers: 1, largest_free: 4 }))).toBe('4 free on 1 GPU');
    expect(poolFreeText(pool({ free: 6, providers: 2, largest_free: 4 }))).toBe('6 free on 2 GPUs, at most 4 on one');
    expect(poolFreeText(pool({ free: 0 }))).toBe('None free');
    expect(poolFreeText(pool({ detail: 'HTTP 400 placement.query.bad_value' }))).toBe(
      'Refused by Placement (HTTP 400 placement.query.bad_value): no server is created with its flavors',
    );
  });

  it("says why a destination's GPU capacity was not read, with the failure's detail", () => {
    const unknown = (reason: GpuCapacity['reason'], detail: string | null): GpuCapacity => ({ status: 'unknown', reason, detail, read_at: null, pools: [] });
    expect(gpuCapacityReason(unknown('forbidden', 'HTTP 403'))).toBe('the destination credential lacks the role Placement asks for, admin by default (HTTP 403)');
    expect(gpuCapacityReason(unknown('no_placement', null))).toBe("the destination's catalog lists no Placement");
    expect(gpuCapacityReason(unknown('error', null))).toBe('the read failed');
    expect(gpuCapacityReason(unknown('error', 'ReadTimeout'))).toBe('the read failed (ReadTimeout)');
  });
});

describe("a destination card's GPU capacity and the provider form's vGPU profiles (SDD §16, §22.3; plan task G11)", () => {
  const row = (key: number, flavor: string, profile: string) => ({ key, flavor, profile });

  it("reads a destination's GPU capacity from its check report, and none from a report without it", () => {
    const capacity: GpuCapacity = { status: 'ok', reason: null, detail: null, read_at: '2026-10-08T11:57:00Z', pools: [] };
    expect(checkedGpuCapacity({ admin: true, gpu_capacity: capacity })).toBe(capacity);
    const unread = { ...capacity, status: 'unknown', reason: 'forbidden', detail: 'HTTP 403' };
    expect(checkedGpuCapacity({ gpu_capacity: unread })).toBe(unread);
    // a source's report, one from before 0.3.0 and one a PATCH reset hold none; a value it cannot read is none
    expect(checkedGpuCapacity({ admin: true, roles: ['admin'] })).toBeNull();
    expect(checkedGpuCapacity({})).toBeNull();
    expect(checkedGpuCapacity({ gpu_capacity: null })).toBeNull();
    expect(checkedGpuCapacity({ gpu_capacity: 'ok' })).toBeNull();
    expect(checkedGpuCapacity({ gpu_capacity: { status: 'ok', pools: 'none' } })).toBeNull();
    expect(checkedGpuCapacity({ gpu_capacity: { status: 'read', pools: [] } })).toBeNull();
  });

  it("lists a provider's GPU flavors as its inventory reads them: a destination's, and a source's VMs'", () => {
    const flavor = (name: string, extra_specs: Record<string, string>): DestinationFlavor => ({ name, vcpus: 8, ram_mb: 32768, disk_gb: 80, extra_specs });
    const flavors = [
      flavor('m1.large', {}),
      flavor('p1.a100', { 'pci_passthrough:alias': 'a100:1' }),
      flavor('g1.custom', { 'resources:CUSTOM_A100_4C': '1' }),
      flavor('g1.a100-4c', { 'resources:VGPU': '1', 'trait:CUSTOM_NVIDIA_A100_4C': 'required' }),
    ];
    const destination: DestinationInventory = { networks: {}, flavors, volume_types: [], quotas: {}, projects: [] };
    // a destination's gpu_flavors, by name in code-point order
    const gpuFlavors = (names: string[]): GpuFlavor[] => names.map((name) => ({ name, vcpus: 8, ram_mb: 32768, disk_gb: 80, gpus: [] }));
    expect(inventoryGpuFlavors({ ...destination, gpu_flavors: gpuFlavors(['p1.a100', 'g1.a100-4c']) })).toEqual(['g1.a100-4c', 'p1.a100']);
    expect(inventoryGpuFlavors({ ...destination, gpu_flavors: [] })).toEqual([]);
    // an answer without them: its flavors that ask for a GPU as §22.2 reads them, a CUSTOM_ class with a declared profile
    expect(inventoryGpuFlavors(destination)).toEqual(['g1.a100-4c', 'p1.a100']);
    expect(inventoryGpuFlavors(destination, { 'g1.custom': 'A100-4C' })).toEqual(['g1.a100-4c', 'g1.custom', 'p1.a100']);
    // a source's: the flavors of its VMs with a GPU, each once - a VM recorded before 0.3.0 by its flavor's requests
    const vgpu = gpuDevice({ kind: 'vgpu', resource_class: 'VGPU' });
    const vms = [
      vm({ flavor: 'g1.vgpu', gpus: [vgpu] }),
      vm({ flavor: 'm1.small' }),
      vm({ flavor: 'g1.vgpu', gpus: [vgpu] }),
      vm({ flavor: 'g1.old', flavor_extra_specs: { 'resources:VGPU': '1' } }),
      vm({ flavor: 'g1.custom', flavor_extra_specs: { 'resources:CUSTOM_A100_4C': '1' } }),
      vm({ flavor: null, gpus: [vgpu] }),
    ];
    expect(inventoryGpuFlavors(vms)).toEqual(['g1.old', 'g1.vgpu']);
    expect(inventoryGpuFlavors(vms, { 'g1.custom': 'A100-4C' })).toEqual(['g1.custom', 'g1.old', 'g1.vgpu']);
    expect(inventoryGpuFlavors([])).toEqual([]);
  });

  it('refuses what the API refuses of a declared vGPU profile, and a flavor declared twice, each name trimmed (SDD §4.2)', () => {
    expect(vgpuProfileErrors([row(0, 'g1.a100-4c', 'A100-4C'), row(1, ' p1.a100 ', ' A100D-80C ')])).toEqual([]);
    expect(vgpuProfileErrors([])).toEqual([]);
    // at most 255 and 64 characters, counted in code points as Python counts them: one astral character is two UTF-16 units
    const astral = '\u{1D7D8}';
    expect(vgpuProfileErrors([row(0, astral.repeat(255), astral.repeat(64))])).toEqual([]);
    expect(vgpuProfileErrors([row(0, 'g1.a100-4c', 'A100-4C'), row(7, 'f'.repeat(256), astral.repeat(65))])).toEqual([
      { key: 7, field: 'flavor', message: 'Shorten the flavor of vGPU profile 2: at most 255 characters.' },
      { key: 7, field: 'profile', message: 'Shorten the profile of vGPU profile 2: at most 64 characters.' },
    ]);
    // an empty name - one of spaces too - and a flavor declared again, which one map cannot hold
    expect(vgpuProfileErrors([row(0, ' ', ''), row(1, 'g1.a100-4c', 'A100-4C'), row(2, 'g1.a100-4c ', ' '), row(3, '', 'A100-3-20C'), row(4, 'g1.a100-4c', 'A100-4C')])).toEqual([
      { key: 0, field: 'flavor', message: 'Enter the flavor of vGPU profile 1, or remove it.' },
      { key: 0, field: 'profile', message: 'Enter the profile of vGPU profile 1, such as A100-4C.' },
      { key: 2, field: 'flavor', message: 'g1.a100-4c is declared twice: change or remove vGPU profile 3.' },
      { key: 2, field: 'profile', message: 'Enter the profile of vGPU profile 3, such as A100-4C.' },
      { key: 3, field: 'flavor', message: 'Enter the flavor of vGPU profile 4, or remove it.' },
      { key: 4, field: 'flavor', message: 'g1.a100-4c is declared twice: change or remove vGPU profile 5.' },
    ]);
  });

  it('sends the rows as one map of trimmed names, and tells the same map in any order of its pairs', () => {
    expect(vgpuProfileMap([row(0, ' g1.a100-4c ', ' A100-4C '), row(1, 'p1.a100', 'A100D-80C')])).toEqual({ 'g1.a100-4c': 'A100-4C', 'p1.a100': 'A100D-80C' });
    // own keys, never an assignment: a flavor named "__proto__" is a pair like any other (review RV23-01)
    const odd = vgpuProfileMap([row(0, '__proto__', 'A100-4C')]);
    expect(Object.getPrototypeOf(odd)).toBe(Object.prototype);
    expect(JSON.stringify(odd)).toBe('{"__proto__":"A100-4C"}');
    expect(vgpuProfileMap([])).toEqual({});
    expect(sameVgpuProfiles({ 'g1.a100-4c': 'A100-4C', 'p1.a100': 'A100D-80C' }, { 'p1.a100': 'A100D-80C', 'g1.a100-4c': 'A100-4C' })).toBe(true);
    expect(sameVgpuProfiles({}, {})).toBe(true);
    expect(sameVgpuProfiles({ 'g1.a100-4c': 'A100-4C' }, { 'g1.a100-4c': 'A100-3-20C' })).toBe(false);
    expect(sameVgpuProfiles({ 'g1.a100-4c': 'A100-4C' }, { 'g1.a100-4c': 'A100-4C', 'p1.a100': 'A100D-80C' })).toBe(false);
    expect(sameVgpuProfiles({ 'g1.a100-4c': 'A100-4C' }, {})).toBe(false);
  });
});
