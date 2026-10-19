import { describe, expect, it } from 'vitest';
import { makeVm } from '../api/mockData';
import type { VMRef } from '../api/types';
import { readinessFlags, readinessLevel } from './readiness';

describe('readinessFlags', () => {
  it('flags OpenStack ports without exactly one fixed IP, naming each with its addresses (SDD §9.3 NET_PORT_IPS)', () => {
    const vm = makeVm({
      id: 'os-r1',
      name: 'mixed-01',
      disks: [{ size_gb: 20 }],
      nics: [
        { network: 'app-net', fixed_ips: ['10.0.0.5', 'fd00::5'] },
        { network: 'l2-net', fixed_ips: [] },
        { network: 'app-net', fixed_ips: ['10.0.0.6'] },
      ],
    });
    const flag = readinessFlags(vm, 'openstack').find((f) => f.code === 'NET_PORT_IPS');
    expect(flag).toMatchObject({ label: 'Port IPs', severity: 'warning' });
    expect(flag!.detail).toContain('app-net (10.0.0.5, fd00::5), l2-net (no fixed IP)');
    expect(flag!.detail).not.toContain('10.0.0.6');
    // the way out the plan offers (SDD §16 port_creation_mode), and what happens without it
    expect(flag!.detail).toMatch(/keep mac addresses and every fixed ip/i);
    expect(flag!.detail).toMatch(/without it, cold and warm cannot recreate them and storage handover keeps one address per port/i);
    expect(readinessLevel(readinessFlags(vm, 'openstack'))).toBe('attention');
    // one port: "Port … keeps"
    const dual = makeVm({ id: 'os-r4', name: 'dual-01', disks: [{ size_gb: 20 }], nics: [{ network: 'app-net', fixed_ips: ['10.0.0.5', 'fd00::5'] }] });
    expect(readinessFlags(dual, 'openstack').find((f) => f.code === 'NET_PORT_IPS')!.detail).toMatch(/^Port app-net \(10\.0\.0\.5, fd00::5\) keeps every address/);
  });

  it('says nothing about a port with one fixed IP, or about VMware NICs, which the migration kit recreates', () => {
    const one = makeVm({ id: 'os-r2', name: 'one-01', disks: [{ size_gb: 20 }], nics: [{ network: 'app-net', fixed_ips: ['10.0.0.6'] }] });
    expect(readinessFlags(one, 'openstack').map((f) => f.code)).not.toContain('NET_PORT_IPS');
    const vmw = makeVm({ id: 'vm-r3', name: 'vmw-01', disks: [{ size_gb: 20 }], nics: [{ network: 'VM Network', fixed_ips: ['10.0.0.7', 'fd00::7'] }] });
    expect(readinessFlags(vmw, 'vmware').map((f) => f.code)).not.toContain('NET_PORT_IPS');
  });

  it('blocks a VMware VM whose name the migration kit keeps nothing of, as validation does (SDD §9.3 SRC_VM_NAME_UNUSABLE, review RV28-03)', () => {
    const vm = (name: string) => makeVm({ id: 'vm-r5', name, disks: [{ size_gb: 20 }], nics: [{ network: 'VM Network', fixed_ips: ['10.0.0.7'] }] });
    // Japanese "server": no Latin letter or digit survives the kit's naming
    const flag = readinessFlags(vm('\u30b5\u30fc\u30d0\u30fc'), 'vmware').find((f) => f.code === 'SRC_VM_NAME_UNUSABLE');
    expect(flag).toMatchObject({ label: 'Unusable name', severity: 'blocker' });
    expect(flag!.detail).toMatch(/migration kit keeps no character of the name/i);
    expect(readinessLevel(readinessFlags(vm('\u30b5\u30fc\u30d0\u30fc'), 'vmware'))).toBe('blocker');
    // a name the kit transliterates keeps its VM ready, and an OpenStack VM is never named by the kit
    expect(readinessFlags(vm('caf\u00e9-01'), 'vmware').map((f) => f.code)).not.toContain('SRC_VM_NAME_UNUSABLE');
    expect(readinessFlags(vm('\u30b5\u30fc\u30d0\u30fc'), 'openstack').map((f) => f.code)).not.toContain('SRC_VM_NAME_UNUSABLE');
  });

  it('blocks a VM with a Nova task in flight whatever its power state, naming the task, as validation does (SDD §9.3 SRC_VM_TRANSITIONAL_STATE, review PV-05)', () => {
    const vm = (power_state: VMRef['power_state'], task_state: string | null) => ({
      ...makeVm({ id: 'os-r7', name: 'backup-01', disks: [{ size_gb: 20 }], nics: [{ network: 'app-net', fixed_ips: ['10.0.0.9'] }] }),
      power_state,
      task_state,
    });
    // a backup's image upload under ACTIVE: the guest runs, and a stop or snapshot would fail
    const flag = readinessFlags(vm('running', 'image_uploading'), 'openstack').find((f) => f.code === 'SRC_VM_TRANSITIONAL_STATE');
    expect(flag).toMatchObject({ label: 'Task in flight', severity: 'blocker' });
    expect(flag!.detail).toBe('A Nova task (image_uploading) is in flight.');
    expect(readinessLevel(readinessFlags(vm('stopped', 'image_snapshot'), 'openstack'))).toBe('blocker');
    // no task, no blocker; a resize names the tasks a transitional status stands for
    expect(readinessFlags(vm('running', null), 'openstack').map((f) => f.code)).not.toContain('SRC_VM_TRANSITIONAL_STATE');
    expect(readinessFlags(vm('transitioning', null), 'openstack').find((f) => f.code === 'SRC_VM_TRANSITIONAL_STATE')!.detail).toBe(
      'A Nova task (resize, migration, rescue, rebuild, reboot) is in flight.',
    );
    // a server in error with a task meets both conditions, as the API's pre-flight finds
    expect(readinessFlags(vm('error', 'deleting'), 'openstack').map((f) => f.code)).toEqual(
      expect.arrayContaining(['SRC_VM_ERROR_STATE', 'SRC_VM_TRANSITIONAL_STATE']),
    );
  });

  it('blocks a vGPU requested bare or in a request group, as validation does (SDD §9.3 VM_VGPU, review PL-08)', () => {
    const vm = (specs: Record<string, string>) =>
      makeVm({ id: 'os-r6', name: 'gpu-02', disks: [{ size_gb: 20 }], nics: [{ network: 'app-net', fixed_ips: ['10.0.0.8'] }], flavor_extra_specs: specs });
    for (const key of ['resources:VGPU', 'resources1:VGPU', 'resources_GPU-a:VGPU'])
      expect(readinessFlags(vm({ [key]: '1' }), 'openstack').find((f) => f.code === 'VM_VGPU')).toMatchObject({ label: 'vGPU', severity: 'blocker' });
    // another resource class, or a trait, in a request group is no vGPU
    for (const key of ['resources1:VCPU', 'resources:CUSTOM_VGPU_X', 'resources1:VGPU_DISPLAY_HEAD', 'trait1:CUSTOM_VGPU'])
      expect(readinessFlags(vm({ [key]: '1' }), 'openstack').map((f) => f.code)).not.toContain('VM_VGPU');
  });
});
