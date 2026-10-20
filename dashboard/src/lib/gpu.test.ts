import { describe, expect, it } from 'vitest';
// One case table for the control plane and the dashboard's mock (SDD §22.2, PR #64 review).
import raw from '../../../seamless/tests/fixtures/gpu_request_cases.json?raw';
import type { GpuDevice, VMRef } from '../api/types';
import { gpuRequests, vmGpus } from './gpu';

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
