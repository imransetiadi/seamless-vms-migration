import { describe, expect, it } from 'vitest';
import type { Finding } from '../api/types';
import { groupFindings } from './findings';

const mtu = (vm: string, mtuValue: number): Finding & { vmName: string; migrationId: string } => ({
  code: 'NET_MTU_SHRINK',
  severity: 'warning',
  message: `Destination MTU 1442 is smaller than the source NIC MTU ${mtuValue}.`,
  remediation: 'Lower the guest MTU.',
  strategies: [],
  vmName: vm,
  migrationId: `mig-${vm}`,
});

describe('groupFindings', () => {
  it('groups by code, most severe first, keeping per-VM messages', () => {
    const groups = groupFindings([
      mtu('web-02', 1450),
      { code: 'VM_VGPU', severity: 'blocker', message: 'vGPU', remediation: null, strategies: [], vmName: 'gpu-01', migrationId: 'mig-gpu' },
      mtu('web-01', 1450),
      mtu('ad-dc-01', 1500),
    ]);
    expect(groups.map((g) => [g.code, g.items.length])).toEqual([
      ['VM_VGPU', 1],
      ['NET_MTU_SHRINK', 3],
    ]);
    const net = groups[1];
    expect(net?.items.map((i) => i.vmName)).toEqual(['ad-dc-01', 'web-01', 'web-02']);
    expect(net?.sameMessage).toBe(false);
  });
});
