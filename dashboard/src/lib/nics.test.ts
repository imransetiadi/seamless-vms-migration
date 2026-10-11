import { describe, expect, it } from 'vitest';
import { makeVm, buildFixtures } from '../api/mockData';
import type { Plan } from '../api/types';
import { canonicalIp, isIpAddress, nicTarget, noHostAddress, parseNicDrafts, toNicDraft, withVmNicOverrides } from './nics';

const now = Date.parse('2026-10-09T12:00:00Z');
const base: Plan = { ...buildFixtures(now).plans[0]!, mappings: { networks: { 'tenant-app': 'tenant-web' }, flavors: {}, volume_types: {}, projects: {} }, nic_overrides: {} };
const vm = makeVm({
  id: 'os-n1',
  name: 'nic-01',
  disks: [{ size_gb: 20 }],
  nics: [
    { network: 'tenant-app', mac: 'fa:16:3e:00:00:01', fixed_ips: ['10.20.1.5'] },
    { network: 'tenant-db', mac: 'fa:16:3e:00:00:02', fixed_ips: ['10.20.2.5', 'fd00::5'] },
  ],
});

describe('NIC overrides (SDD §4.2, §9.2)', () => {
  it('sends a NIC where its override says, else where the plan maps it, with its own addresses unless the override names them', () => {
    const plan = { ...base, nic_overrides: { 'os-n1': { 'fa:16:3e:00:00:02': { network: 'tenant-web', fixed_ips: null } } } };
    expect(nicTarget(plan, vm.source_id, vm.nics[0]!)).toEqual({ network: 'tenant-web', addresses: ['10.20.1.5'], overridden: false });
    expect(nicTarget(plan, vm.source_id, vm.nics[1]!)).toEqual({ network: 'tenant-web', addresses: ['10.20.2.5', 'fd00::5'], overridden: true });
    const upper = { ...base, nic_overrides: { 'os-n1': { 'fa:16:3e:00:00:01': { network: null, fixed_ips: ['10.20.1.9'] } } } };
    expect(nicTarget(upper, vm.source_id, { ...vm.nics[0]!, mac: 'FA:16:3E:00:00:01' }).addresses).toEqual(['10.20.1.9']);
  });

  it('turns the editor rows into entries, leaving out a row that keeps both, and names a bad address', () => {
    const drafts = vm.nics.map((nic) => toNicDraft(base, vm.source_id, nic));
    expect(drafts).toEqual([
      { mac: 'fa:16:3e:00:00:01', network: '', addresses: '' },
      { mac: 'fa:16:3e:00:00:02', network: '', addresses: '' },
    ]);
    expect(parseNicDrafts(drafts)).toEqual({ entries: {}, problems: {} });
    const edited = [
      { mac: 'fa:16:3e:00:00:01', network: 'tenant-web', addresses: '' },
      { mac: 'fa:16:3e:00:00:02', network: '', addresses: '10.20.2.9, fd00::9' },
    ];
    expect(parseNicDrafts(edited)).toEqual({
      entries: {
        'fa:16:3e:00:00:01': { network: 'tenant-web', fixed_ips: null },
        'fa:16:3e:00:00:02': { network: null, fixed_ips: ['10.20.2.9', 'fd00::9'] },
      },
      problems: {},
    });
    // by MAC, so each address field carries its own problems (SDD §16)
    expect(parseNicDrafts([{ mac: 'fa:16:3e:00:00:01', network: '', addresses: '10.20.1.300 10.20.1.7 10.20.1.7' }]).problems).toEqual({
      'fa:16:3e:00:00:01': ['not an IP address: 10.20.1.300', '10.20.1.7 given twice'],
    });
  });

  it("stores and compares addresses in the API's canonical form (SDD §4.2)", () => {
    const parsed = parseNicDrafts([{ mac: 'fa:16:3e:00:00:02', network: '', addresses: 'FD00:0::0009, 10.20.2.9' }]);
    expect(parsed.entries['fa:16:3e:00:00:02']?.fixed_ips).toEqual(['fd00::9', '10.20.2.9']);
    expect(parseNicDrafts([{ mac: 'fa:16:3e:00:00:02', network: '', addresses: 'fd00::5, FD00:0::0005' }]).problems).toEqual({
      'fa:16:3e:00:00:02': ['fd00::5 given twice'],
    });
  });

  it('replaces only this VM in the plan, and drops the VM when none of its NICs is overridden', () => {
    const plan = { ...base, nic_overrides: { other: { 'fa:16:3e:00:00:09': { network: 'tenant-web', fixed_ips: null } } } };
    const next = withVmNicOverrides(plan, 'os-n1', { 'fa:16:3e:00:00:01': { network: 'tenant-web', fixed_ips: null } });
    expect(Object.keys(next).sort()).toEqual(['os-n1', 'other']);
    expect(withVmNicOverrides({ ...plan, nic_overrides: next }, 'os-n1', {})).toEqual(plan.nic_overrides);
  });

  it('tells IP addresses apart', () => {
    for (const ok of ['10.0.0.5', '192.168.10.255', 'fd00::5', '::1', 'fe80::1:2']) expect(isIpAddress(ok), ok).toBe(true);
    for (const bad of ['10.0.0.256', '10.0.0', 'fd00:::5', 'host', '', '10.0.0.5/24']) expect(isIpAddress(bad), bad).toBe(false);
  });
});

describe('canonicalIp', () => {
  it("writes an address as the API's ipaddress does, and leaves anything else as given", () => {
    expect(canonicalIp('FD00:0::0005')).toBe('fd00::5');
    expect(canonicalIp('fd00:0:0:1:0:0:0:5')).toBe('fd00:0:0:1::5');
    expect(canonicalIp('1:0:0:2:0:0:3:4')).toBe('1::2:0:0:3:4');
    expect(canonicalIp('1:0:2:3:4:5:6:7')).toBe('1:0:2:3:4:5:6:7');
    expect(canonicalIp('0:0:0:0:0:0:0:1')).toBe('::1');
    expect(canonicalIp('10.20.1.5')).toBe('10.20.1.5');
    expect(canonicalIp('10.20.1.300')).toBe('10.20.1.300');
    expect(canonicalIp('host')).toBe('host');
  });
});

describe('noHostAddress', () => {
  it('names what Neutron gives no VM, as the API does (SDD §9.3)', () => {
    expect(noHostAddress('192.168.10.0', ['192.168.10.0/24'])).toBe('the network address of 192.168.10.0/24');
    expect(noHostAddress('192.168.10.255', ['192.168.10.0/24'])).toBe('the broadcast address of 192.168.10.0/24');
    expect(noHostAddress('fd00:10::', ['fd00:10::/64'])).toBe('the network address of fd00:10::/64');
    expect(noHostAddress('10.255.0.0', ['10.255.0.0/31'])).toBe('the network address of 10.255.0.0/31');
    expect(noHostAddress('10.9.9.9', ['10.9.9.9/32'])).toBe('the network address of 10.9.9.9/32');
    // the subnet as the API writes it
    expect(noHostAddress('192.168.10.0', ['192.168.10.7/24'])).toBe('the network address of 192.168.10.0/24');
    expect(noHostAddress('fd00::', ['FD00:0::/64'])).toBe('the network address of fd00::/64');
    const cidrs = ['192.168.10.0/24', '10.255.0.0/31', 'fd00:10::/64', 'fd99::/16'];
    for (const host of ['192.168.10.1', '192.168.10.254', '10.255.0.1', 'fd00:10::ffff:ffff:ffff:ffff', 'fd99:ffff:ffff:ffff:ffff:ffff:ffff:ffff', '10.0.0.5', 'nonsense'])
      expect(noHostAddress(host, cidrs), host).toBeNull();
  });
});

describe('ipInCidr', () => {
  it('places IPv4 and IPv6 addresses in their subnets', async () => {
    const { ipInCidr } = await import('./nics');
    expect(ipInCidr('10.20.1.5', '10.20.0.0/16')).toBe(true);
    expect(ipInCidr('10.21.1.5', '10.20.0.0/16')).toBe(false);
    expect(ipInCidr('192.168.10.5', '192.168.10.0/24')).toBe(true);
    expect(ipInCidr('fd00:10::5', 'fd00:10::/64')).toBe(true);
    expect(ipInCidr('fd00:11::5', 'fd00:10::/64')).toBe(false);
    expect(ipInCidr('10.20.1.5', 'fd00:10::/64')).toBe(false);
    expect(ipInCidr('nonsense', '10.0.0.0/8')).toBe(false);
  });
});
