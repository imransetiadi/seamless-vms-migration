import type { DestinationInventory, DestinationNetwork, DestinationSubnet } from '../api/types';

/**
 * The network a NIC's RHOSO network name resolves to, as the API's resolve_network (SDD §7.3 step 0, §9.3): the destination
 * credential's own network of that name, else the only one it sees; else none and why ({ network: null, why: null } when
 * no network carries the name).
 */
export function resolveNetwork(networks: DestinationNetwork[], name: string): { network: DestinationNetwork | null; why: string | null } {
  const own = networks.filter((n) => n.name === name && n.own);
  if (own.length === 1) return { network: own[0]!, why: null };
  if (own.length) return { network: null, why: `${own.length} networks of the credential's project are named '${name}'` };
  const seen = networks.filter((n) => n.name === name);
  if (seen.length === 1) return { network: seen[0]!, why: null };
  if (!seen.length) return { network: null, why: null };
  const projects = [...new Set(seen.map((n) => String(n.project_id)))].sort().join(', ');
  return { network: null, why: `${seen.length} networks of other projects are named '${name}' (${projects}), none of the credential's project` };
}

/** The networks an inventory lists with their owners; one from before `network_details` lists each name once, as the credential's own (SDD §11). */
export function networkList(inventory: Pick<DestinationInventory, 'networks' | 'network_details'>): DestinationNetwork[] {
  if (inventory.network_details?.length) return inventory.network_details;
  return Object.entries(inventory.networks).map(([name, mtu]) => ({ id: null, name, mtu, project_id: null, own: true, shared: false }));
}

/** A resolved network's subnets: by its id, else (an inventory from before it) by name. */
export function subnetsOf(subnets: DestinationSubnet[], network: DestinationNetwork): DestinationSubnet[] {
  if (network.id !== null) return subnets.filter((s) => s.network_id === network.id);
  return subnets.filter((s) => s.network === network.name);
}
