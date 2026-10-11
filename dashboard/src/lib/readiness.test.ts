import { describe, expect, it } from 'vitest';
import { makeVm } from '../api/mockData';
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
    // the way out the plan offers (SDD §16 port_creation_mode)
    expect(flag!.detail).toMatch(/keep mac addresses and every fixed ip/i);
    expect(readinessLevel(readinessFlags(vm, 'openstack'))).toBe('attention');
  });

  it('says nothing about a port with one fixed IP, or about VMware NICs, which the migration kit recreates', () => {
    const one = makeVm({ id: 'os-r2', name: 'one-01', disks: [{ size_gb: 20 }], nics: [{ network: 'app-net', fixed_ips: ['10.0.0.6'] }] });
    expect(readinessFlags(one, 'openstack').map((f) => f.code)).not.toContain('NET_PORT_IPS');
    const vmw = makeVm({ id: 'vm-r3', name: 'vmw-01', disks: [{ size_gb: 20 }], nics: [{ network: 'VM Network', fixed_ips: ['10.0.0.7', 'fd00::7'] }] });
    expect(readinessFlags(vmw, 'vmware').map((f) => f.code)).not.toContain('NET_PORT_IPS');
  });
});
