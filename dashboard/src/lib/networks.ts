import type { DestinationInventory, DestinationNetwork, DestinationSubnet, Nic, Provider } from '../api/types';
import { pyRepr } from './format';
import { addressRefusal, kitIpv4 } from './nics';
import { destinationRelease, type PolicyProfile } from './platforms';

/**
 * The network a NIC's RHOSO network name resolves to, as the API's resolve_network (SDD §7.3 step 0, §9.3): the destination
 * credential's own network of that name, else the only one it sees; else none and why ({ network: null, why: null } when
 * no network carries the name).
 */
export function resolveNetwork(networks: DestinationNetwork[], name: string): { network: DestinationNetwork | null; why: string | null } {
  const own = networks.filter((n) => n.name === name && n.own);
  if (own.length === 1) return { network: own[0]!, why: null };
  if (own.length) return { network: null, why: `${own.length} networks of the credential's project are named ${pyRepr(name)}` };
  const seen = networks.filter((n) => n.name === name);
  if (seen.length === 1) return { network: seen[0]!, why: null };
  if (!seen.length) return { network: null, why: null };
  // a network without an owner: the API writes Python's None
  const projects = [...new Set(seen.map((n) => String(n.project_id ?? 'None')))].sort().join(', ');
  return { network: null, why: `${seen.length} networks of other projects are named ${pyRepr(name)} (${projects}), none of the credential's project` };
}

/** The networks an inventory lists with their owners; one from before `network_details` lists each name once, as the credential's own (SDD §10). */
export function networkList(inventory: Pick<DestinationInventory, 'networks' | 'network_details'>): DestinationNetwork[] {
  if (inventory.network_details?.length) return inventory.network_details;
  return Object.entries(inventory.networks).map(([name, mtu]) => ({ id: null, name, mtu, project_id: null, own: true, shared: false, external: false }));
}

/**
 * The roles Neutron's policy lets choose a port's address, MAC or port security on another project's network, by the
 * destination's policy profile: the API's PORT_ROLES_BY_PROFILE (SDD §6.6, §21.5). An unknown profile counts admin
 * alone, the role both count.
 */
export const PORT_ROLES_BY_PROFILE: Record<PolicyProfile, readonly string[]> = {
  legacy: ['admin', 'advsvc'],
  secure_rbac: ['admin', 'service'],
  unknown: ['admin'],
};

/** Why another project's network refuses the token's roles, by the profile: the API's _ROLE_RULE, naming the destination provider `where`. */
const ROLE_RULE: Record<PolicyProfile, (where: string) => string> = {
  legacy: () => "Neutron lets only the admin or advsvc role, or that project, choose a port's address or MAC there, and the credential's token has neither",
  secure_rbac: () =>
    "Neutron's secure-RBAC defaults let only the admin or service role, or that project, choose a port's address or MAC there, and the credential's token has neither",
  unknown: (where) =>
    `the policy profile of ${where} is unknown, so only the admin role, or that project, may choose a port's address or MAC there, and the credential's token lacks it (check ${where} so its series is detected, or declare its policy profile or series)`,
};

const knownProfile = (profile: string): PolicyProfile => (profile in PORT_ROLES_BY_PROFILE ? (profile as PolicyProfile) : 'unknown');

/**
 * Why the destination credential may not put a port on `network`, or null, as the API's port_refusal (SDD §6.6): another
 * project's network takes it only when shared with the credential's project or external (an admin sees every project's
 * networks), and only from a token with a role the destination's policy `profile` counts, since every recreated port asks
 * for its address or MAC. An external network that is not shared, the credential's own included, also needs the admin
 * role: Nova attaches no other to a server. `roles` undefined (not reported) leaves the role halves unjudged. `where` is the
 * destination provider's label, which a text built for a plan names, as the API's (SDD §1 naming, review RV23-07); the
 * dashboard's own copy may say "the destination".
 */
export function portRefusal(network: DestinationNetwork, roles: readonly string[] | undefined, profile: string = 'legacy', where = 'the destination'): string | null {
  const counted = knownProfile(profile);
  if (!network.own) {
    if (!(network.shared || network.external)) return "it is neither shared with the credential's project nor external";
    if (roles !== undefined && !roles.some((role) => PORT_ROLES_BY_PROFILE[counted].includes(role))) return ROLE_RULE[counted](where);
  }
  // Nova's network:attach_external_network, admin only by default (2023.1): the credential's own network included
  if (network.external && !network.shared && roles !== undefined && !roles.includes('admin'))
    return "it is external and not shared, and Nova lets only the admin role attach such a network to a server; the credential's token lacks it";
  return null;
}

/**
 * The destination's policy profile as its check report gives it (SDD §21.5), as the API's policy_profile_of: a report that
 * names none - reset by new credentials or a provider change, or from before 0.2.0 - reads as the provider's declarations
 * alone, never legacy by default (RV18-04); a value no rule knows reads unknown.
 */
export function policyProfileOf(
  capabilities: Record<string, unknown> | undefined,
  provider: Pick<Provider, 'kind' | 'release' | 'policy_profile'> | undefined,
): PolicyProfile {
  const reported = capabilities?.policy_profile;
  if (reported !== undefined && reported !== null) return knownProfile(String(reported));
  if (!provider) return 'unknown';
  return destinationRelease(provider.kind, provider.release, provider.policy_profile, '', '').policy_profile;
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
