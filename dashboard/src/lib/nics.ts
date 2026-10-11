import type { DestinationSubnet, Nic, NicOverride, Plan } from '../api/types';

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

/**
 * An address as the API's `ipaddress` writes it ("FD00:0::0005" is "fd00::5", SDD §4.2); anything else as given. The
 * URL standard writes IPv6 as RFC 5952 does, as Python does; an address with an embedded IPv4 part stays as given.
 */
export function canonicalIp(address: string): string {
  if (IPV4.test(address) || !isIpAddress(address) || address.includes('.')) return address;
  return new URL(`http://[${address}]/`).hostname.slice(1, -1);
}

/**
 * The editor's rows as `nic_overrides` entries, their addresses written as the API stores them, and what the API would
 * refuse (SDD §12) by MAC: each address field carries its own problems (SDD §16).
 */
export function parseNicDrafts(drafts: NicDraft[]): { entries: Record<string, NicOverride>; problems: Record<string, string[]> } {
  const entries: Record<string, NicOverride> = {};
  const problems: Record<string, string[]> = {};
  for (const draft of drafts) {
    const addresses = draft.addresses.split(/[\s,]+/).filter(Boolean).map(canonicalIp);
    const own: string[] = [];
    const bad = addresses.filter((a) => !isIpAddress(a));
    if (bad.length) own.push(`not an IP address: ${bad.join(', ')}`);
    const twice = [...new Set(addresses.filter((a, i) => addresses.indexOf(a) !== i))].sort();
    if (twice.length) own.push(`${twice.join(', ')} given twice`);
    if (own.length) problems[draft.mac] = own;
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

/** An IPv6 address whose last 32 bits are written as an IPv4 address, with them as two hex groups instead. */
function withHexTail(address: string): string {
  const cut = address.lastIndexOf(':');
  const tail = address.slice(cut + 1);
  if (cut < 0 || !IPV4.test(tail)) return address;
  const [a, b, c, d] = tail.split('.').map(Number) as [number, number, number, number];
  return `${address.slice(0, cut + 1)}${((a << 8) | b).toString(16)}:${((c << 8) | d).toString(16)}`;
}

/**
 * The guest addresses the VMware migration kit sends Neutron, as its FixedIPsForNeutron and the API's kit_ipv4 (SDD §7.2):
 * IPv4 ones only, an IPv4-mapped IPv6 address as its IPv4.
 */
export function kitIpv4(addresses: string[]): string[] {
  const out: string[] = [];
  for (const address of addresses) {
    if (IPV4.test(address)) {
      out.push(address);
      continue;
    }
    const ip = toBits(withHexTail(address));
    if (ip && ip.bits === 128 && ip.value >> 32n === 0xffffn) out.push(fromBits(ip.value & 0xffffffffn, 32));
  }
  return out;
}

/** A 32- or 128-bit value written as an address, as the API writes it. */
function fromBits(value: bigint, bits: 32 | 128): string {
  if (bits === 32) return [24n, 16n, 8n, 0n].map((shift) => String((value >> shift) & 255n)).join('.');
  return canonicalIp(Array.from({ length: 8 }, (_, i) => ((value >> BigInt(112 - 16 * i)) & 0xffffn).toString(16)).join(':'));
}

/**
 * What makes `address` no host address of the subnets of `cidrs` that hold it, in the API's words (SDD §9.3, as Neutron's
 * check_subnet_ip for a port created with no owner): an IPv4 subnet's network address at any prefix length, its broadcast
 * address below a /31; an IPv6 subnet's first address is taken. Null for a host address of one of them, or an address none
 * of them holds.
 */
export function noHostAddress(address: string, cidrs: string[]): string | null {
  const ip = toBits(address);
  if (!ip) return null;
  let why: string | null = null;
  for (const cidr of cidrs) {
    const [base, prefix] = cidr.split('/');
    const net = base ? toBits(base) : null;
    const length = Number(prefix);
    if (!net || net.bits !== ip.bits || !Number.isInteger(length) || length < 0 || length > ip.bits) continue;
    const host = BigInt(ip.bits - length);
    const first = (net.value >> host) << host;
    if (ip.value >> host !== first >> host) continue;
    // the subnet as the API's ip_network(cidr, strict=False) writes it
    const subnet = `${fromBits(first, ip.bits)}/${length}`;
    if (ip.bits === 32 && ip.value === first) why = why ?? `the network address of ${subnet}`;
    else if (ip.bits === 32 && length < 31 && ip.value === (first | ((1n << host) - 1n))) why = why ?? `the broadcast address of ${subnet}`;
    else return null;
  }
  return why;
}

/** Subnets whose addresses Neutron derives from the port's MAC. */
const AUTO_ADDRESS_MODES = ['slaac', 'dhcpv6-stateless'];

/**
 * Neutron's check_subnet_ip (2023.1) for a port created with no owner, as Nova and Seamless create theirs, as the API's
 * ipam.host_address: an IPv4 subnet's network address is never a port's, nor its broadcast address below a /31; an IPv6
 * subnet's first address is taken.
 */
function hostAddress(ip: { value: bigint; bits: 32 | 128 }, cidr: string): boolean {
  const [base, prefix] = cidr.split('/');
  const net = base ? toBits(base) : null;
  const length = Number(prefix);
  if (!net || net.bits !== ip.bits || !Number.isInteger(length) || length < 0 || length > ip.bits) return false;
  const host = BigInt(ip.bits - length);
  const first = (net.value >> host) << host;
  if (ip.value >> host !== first >> host) return false;
  if (ip.bits === 32) return ip.value !== first && (length >= 31 || ip.value !== (first | ((1n << host) - 1n)));
  return true;
}

/** The address SLAAC gives `mac` on `cidr` (oslo's get_ipv6_addr_by_EUI64), written as the API writes it. */
export function eui64(cidr: string, mac: string): string {
  const octets = mac.split(':').map((part) => parseInt(part, 16));
  const iface = [...octets.slice(0, 3), 0xff, 0xfe, ...octets.slice(3)];
  iface[0] = iface[0]! ^ 0x02;
  const [base, prefix] = cidr.split('/');
  const host = BigInt(128 - Number(prefix));
  const first = (toBits(base!)!.value >> host) << host;
  return fromBits(first + iface.reduce((n, byte) => (n << 8n) + BigInt(byte), 0n), 128);
}

/**
 * Why Neutron refuses `address` alone on a port of the network whose subnets are `subnets`, in the API's words (SDD §6.6,
 * ipam.address_refusal), or null. The port is created through Neutron with `mac` when `neutron`, else by Nova with a new MAC.
 */
export function addressRefusal(
  address: string,
  subnets: Array<Pick<DestinationSubnet, 'cidr' | 'ipv6_address_mode' | 'ipv6_ra_mode'>>,
  { neutron, mac }: { neutron: boolean; mac: string | null },
): string | null {
  const ip = toBits(address);
  if (!ip) return 'is not an IP address';
  // an address alone: Neutron takes the first subnet of the network that holds it
  const subnet = subnets.find((s) => hostAddress(ip, s.cidr));
  if (!subnet) return "is no host address of any subnet of the port's network";
  if (AUTO_ADDRESS_MODES.includes(subnet.ipv6_address_mode ?? '') || AUTO_ADDRESS_MODES.includes(subnet.ipv6_ra_mode ?? '')) {
    // Neutron takes an explicit address there only if it derives the same one
    if (!neutron || !mac) return `lies on ${subnet.cidr}, a SLAAC or DHCPv6-stateless subnet: through Nova the port gets a new MAC, and Neutron takes no explicit address there`;
    const expected = eui64(subnet.cidr, mac);
    if (canonicalIp(address) !== expected) return `is not ${expected}, the address the SLAAC or DHCPv6-stateless subnet ${subnet.cidr} gives MAC ${mac}`;
  }
  return null;
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
