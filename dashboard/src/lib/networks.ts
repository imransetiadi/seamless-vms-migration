import type { DestinationInventory, DestinationNetwork, DestinationSubnet, Nic } from '../api/types';
import { addressRefusal, kitIpv4 } from './nics';

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

/** The networks an inventory lists with their owners; one from before `network_details` lists each name once, as the credential's own (SDD §10). */
export function networkList(inventory: Pick<DestinationInventory, 'networks' | 'network_details'>): DestinationNetwork[] {
  if (inventory.network_details?.length) return inventory.network_details;
  return Object.entries(inventory.networks).map(([name, mtu]) => ({ id: null, name, mtu, project_id: null, own: true, shared: false, external: false }));
}

/** The roles Neutron's policy lets choose a port's address, MAC or port security on another project's network: the API's PORT_ROLES (SDD §6.6). */
export const PORT_ROLES = ['admin', 'advsvc'];

/**
 * Why the destination credential may not put a port on `network`, or null, as the API's port_refusal (SDD §6.6): another
 * project's network takes it only when shared with the credential's project or external (an admin sees every project's
 * networks), and only from a token with the admin or advsvc role, since every recreated port asks for its address or MAC.
 * `roles` undefined (not reported) leaves that half unjudged.
 */
export function portRefusal(network: DestinationNetwork, roles: readonly string[] | undefined): string | null {
  if (network.own) return null;
  if (!(network.shared || network.external)) return "it is neither shared with the credential's project nor external";
  if (roles !== undefined && !roles.some((role) => PORT_ROLES.includes(role)))
    return "Neutron lets only the admin or advsvc role, or that project, choose a port's address or MAC there, and the credential's token has neither";
  return null;
}

/** The role names of the destination's token, as its check reported them (SDD §10); undefined when it reported none. */
export function capabilityRoles(capabilities: Record<string, unknown> | undefined): string[] | undefined {
  const roles = capabilities?.roles;
  return Array.isArray(roles) ? roles.map(String) : undefined;
}

/** A resolved network's subnets: by its id, else (an inventory from before it) by name. */
export function subnetsOf(subnets: DestinationSubnet[], network: DestinationNetwork): DestinationSubnet[] {
  if (network.id !== null) return subnets.filter((s) => s.network_id === network.id);
  return subnets.filter((s) => s.network === network.name);
}

/**
 * The subnet of its network the VMware migration kit gives `nic`'s port, as the API's kit_subnet (SDD §7.2): the one
 * that holds every IPv4 guest address of the NIC as a host address (the kit sends them all with one subnet), any of them
 * (an IPv4 one first) when it has none, none when the network has no subnet; else why the kit's port could not take them.
 */
export function kitSubnet(nic: Nic, subnets: DestinationSubnet[]): { subnet: DestinationSubnet | null; why: string | null } {
  const addresses = kitIpv4(nic.fixed_ips);
  const v4 = subnets.filter((s) => s.cidr.includes('.'));
  if (!addresses.length) return { subnet: (v4.length ? v4 : subnets)[0] ?? null, why: null };
  const holder = v4.find((s) => addresses.every((a) => addressRefusal(a, [s], { neutron: true, mac: nic.mac }) === null));
  if (holder) return { subnet: holder, why: null };
  for (const address of addresses) {
    const why = addressRefusal(address, subnets, { neutron: true, mac: nic.mac });
    if (why) return { subnet: null, why: `${address} ${why}` };
  }
  return { subnet: null, why: `${addresses.join(', ')} lie in different subnets of the port's network: the migration kit gives a NIC's IPv4 addresses one subnet` };
}
