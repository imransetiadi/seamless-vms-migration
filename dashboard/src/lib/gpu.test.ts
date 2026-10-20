import { describe, expect, it } from 'vitest';
// One case table for the control plane and the dashboard's mock (SDD §22.2, PR #64 review).
import raw from '../../../seamless/tests/fixtures/gpu_request_cases.json?raw';
import type { GpuCapacity, GpuDevice, GpuFlavor, GpuPool, VMRef } from '../api/types';
import { declaredProfiles, flavorsOfPool, gpuCapacityReason, gpuDevice, gpuLabel, gpuRequests, poolFreeText, poolLabel, vmGpus } from './gpu';

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
