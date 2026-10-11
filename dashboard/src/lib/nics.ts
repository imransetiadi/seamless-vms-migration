import type { Nic, NicOverride, Plan } from '../api/types';

/** The plan's override of a VM's NIC, by its MAC (SDD §4.2). */
export function nicOverride(plan: Plan, vmId: string, mac: string | null): NicOverride | null {
  if (!mac) return null;
  return plan.nic_overrides?.[vmId]?.[mac.toLowerCase()] ?? null;
}

/**
 * Where a VM's NIC goes at RHOSO (SDD §9.2, §9.3): its override's network, else the plan's mapping of its network;
 * its override's addresses, else its own.
 */
export function nicTarget(plan: Plan, vmId: string, nic: Nic): { network: string; addresses: string[]; overridden: boolean } {
  const entry = nicOverride(plan, vmId, nic.mac);
  return {
    network: entry?.network || (plan.mappings.networks[nic.network] ?? nic.network),
    addresses: entry?.fixed_ips ?? nic.fixed_ips,
    overridden: entry !== null,
  };
}

/** The plan's `nic_overrides` with this VM's entries replaced; with none, the VM's key goes. */
export function withVmNicOverrides(plan: Plan, vmId: string, entries: Record<string, NicOverride>): Plan['nic_overrides'] {
  const { [vmId]: _previous, ...others } = plan.nic_overrides ?? {};
  return Object.keys(entries).length ? { ...others, [vmId]: entries } : others;
}

/** One row of the NIC editor: '' keeps the plan's mapping, or the NIC's own addresses. */
export interface NicDraft {
  mac: string;
  network: string;
  addresses: string;
}

export function toNicDraft(plan: Plan, vmId: string, nic: Nic): NicDraft {
  const entry = nicOverride(plan, vmId, nic.mac);
  return { mac: (nic.mac ?? '').toLowerCase(), network: entry?.network ?? '', addresses: (entry?.fixed_ips ?? []).join(', ') };
}

const IPV4 = /^(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(\.(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){3}$/;

/** An IPv4 or IPv6 address, as the API's `ipaddress.ip_address` takes it (no prefix length). */
export function isIpAddress(value: string): boolean {
  if (IPV4.test(value)) return true;
  if (!value.includes(':') || !/^[0-9a-fA-F:.]+$/.test(value)) return false;
  try {
    new URL(`http://[${value}]/`);
    return true;
  } catch {
    return false;
  }
}

/** The editor's rows as `nic_overrides` entries, and what the API would refuse (SDD §12), named by MAC. */
export function parseNicDrafts(drafts: NicDraft[]): { entries: Record<string, NicOverride>; problems: string[] } {
  const entries: Record<string, NicOverride> = {};
  const problems: string[] = [];
  for (const draft of drafts) {
    const addresses = draft.addresses.split(/[\s,]+/).filter(Boolean);
    const bad = addresses.filter((a) => !isIpAddress(a));
    if (bad.length) problems.push(`${draft.mac}: not an IP address: ${bad.join(', ')}`);
    const twice = [...new Set(addresses.filter((a, i) => addresses.indexOf(a) !== i))].sort();
    if (twice.length) problems.push(`${draft.mac}: ${twice.join(', ')} given twice`);
    if (draft.network || addresses.length) {
      entries[draft.mac] = { network: draft.network || null, fixed_ips: addresses.length ? addresses : null };
    }
  }
  return { entries, problems };
}

/** The address as a number and its family, or null: IPv4 in 32 bits, IPv6 in 128. */
function toBits(address: string): { value: bigint; bits: 32 | 128 } | null {
  if (IPV4.test(address)) {
    return { value: address.split('.').reduce((n, part) => (n << 8n) + BigInt(Number(part)), 0n), bits: 32 };
  }
  if (!isIpAddress(address) || address.includes('.')) return null;
  const [head, tail = ''] = address.split('::');
  const left = head ? head.split(':') : [];
  const right = address.includes('::') ? (tail ? tail.split(':') : []) : [];
  const groups = address.includes('::') ? [...left, ...Array(8 - left.length - right.length).fill('0'), ...right] : left;
  if (groups.length !== 8) return null;
  return { value: groups.reduce((n, g) => (n << 16n) + BigInt(parseInt(g, 16)), 0n), bits: 128 };
}

/** Whether `address` lies in `cidr` ("10.20.0.0/16", "fd00:10::/64"), like the API's ipaddress check. */
export function ipInCidr(address: string, cidr: string): boolean {
  const [network, prefix] = cidr.split('/');
  const ip = toBits(address);
  const net = network ? toBits(network) : null;
  const length = Number(prefix);
  if (!ip || !net || ip.bits !== net.bits || !Number.isInteger(length) || length < 0 || length > ip.bits) return false;
  const shift = BigInt(ip.bits - length);
  return ip.value >> shift === net.value >> shift;
}
