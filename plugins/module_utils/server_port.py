from __future__ import absolute_import, division, print_function

__metaclass__ = type

import ipaddress

try:
    import openstack
    HAS_OPENSTACK = True
    OPENSTACK_SDK_PORT = openstack.network.v2.port.Port
except ImportError:
    HAS_OPENSTACK = False
    OPENSTACK_SDK_PORT = None

from ansible_collections.os_migrate.os_migrate.plugins.module_utils import (
    exc,
    const,
    reference,
    osm_resource,
)

SERVER_PORT_ORDER_MAX = 1000

# what makes Neutron derive a port's address on a subnet from its MAC (SDD 6.6)
AUTO_ADDRESS_MODES = ("slaac", "dhcpv6-stateless")

# the roles Neutron's policy (2023.1 create_port:fixed_ips:ip_address, :mac_address,
# :port_security_enabled) lets choose a port's address, MAC or port security on another
# project's network (SDD 6.6)
PORT_ROLES = ("admin", "advsvc")


def credential_roles(conn):
    """The role names of the token the connection `conn` holds (SDD 6.6)."""
    return set(conn.session.auth.get_access(conn.session).role_names or [])


def network_refusal(conn, network_id):
    """Why the credential of `conn` may not put a port on the network `network_id`, or
    None (SDD 6.6). Another project's network takes it only when shared with the
    credential's project or external - an admin sees every project's networks - and the
    import chooses every port's address or MAC, which Neutron lets only PORT_ROLES, or
    the network's own project, choose. An external network that is not shared, the
    credential's own included, Nova attaches to a server for the admin role only."""
    network = conn.network.get_network(network_id)
    project = network["project_id"]
    roles = None
    if project != conn.current_project_id:
        if not (network["is_shared"] or network["is_router_external"]):
            return (
                f"the network belongs to project {project} and is neither shared with the "
                "credential's project nor external"
            )
        roles = credential_roles(conn)
        if not roles & set(PORT_ROLES):
            return (
                f"the network belongs to project {project}, and Neutron lets only the admin or "
                "advsvc role, or that project, choose a port's address or MAC there; the "
                "destination credential's token has neither role"
            )
    if network["is_router_external"] and not network["is_shared"]:
        # Nova's network:attach_external_network, admin only by default (2023.1)
        if "admin" not in (roles if roles is not None else credential_roles(conn)):
            return (
                "the network is external and not shared, and Nova lets only the admin role "
                "attach such a network to a server; the destination credential's token lacks it"
            )
    return None


def port_description(source_port_id):
    """The description of a destination port created through Neutron for the
    source port `source_port_id`: how a resumed cutover finds it again and a
    rollback deletes it (SDD 6.6)."""
    return "os-migrate:%s" % source_port_id


def server_ports(conn, sdk_ser):
    # the device_owner part after the colon varies - it is the availability zone
    sdk_ports = filter(
        lambda p: p.get("device_owner", "").startswith("compute:"),
        conn.network.ports(device_id=sdk_ser["id"]),
    )
    return _ports_sorted_by_nova_order(sdk_ser, sdk_ports)


# This function is O(n^3) but we're dealing with very small numbers.
# E.g. even if we had a VM with 20 NICs and each NIC had 20 IPs (which
# is unlikely to ever happen), we'd be looking at 8000 loops max.
def _ports_sorted_by_nova_order(sdk_ser, sdk_ports):
    ordered_ports = list(map(lambda p: [SERVER_PORT_ORDER_MAX, p], sdk_ports))
    nova_ips_sorted = _nova_fixed_ips_sorted(sdk_ser)
    for idx, ip in enumerate(nova_ips_sorted):
        for ordered_port in ordered_ports:
            if _neutron_port_has_fixed_ip(ordered_port[1], ip):
                ordered_port[0] = idx
                continue
    sorted_ports = sorted(ordered_ports, key=lambda ordered_port: ordered_port[0])
    return map(lambda ordered_port: ordered_port[1], sorted_ports)


def _nova_fixed_ips_sorted(sdk_ser):
    ips_sorted = []
    for net in sdk_ser["addresses"]:
        for addr in sdk_ser["addresses"][net]:
            if addr.get("OS-EXT-IPS:type") != "fixed":
                continue
            ips_sorted.append(addr["addr"])
    return ips_sorted


def _neutron_port_has_fixed_ip(sdk_port, ip):
    for addr in sdk_port["fixed_ips"]:
        if addr.get("ip_address") == ip:
            return True
    return False


class ServerPort(osm_resource.Resource):

    resource_type = const.RES_TYPE_SERVER_PORT
    sdk_class = OPENSTACK_SDK_PORT

    info_from_sdk = [
        "id",
        "device_owner",
        "device_id",
    ]
    params_from_refs = [
        "fixed_ips_refs",
        "network_ref",
    ]
    params_from_sdk = ["binding_profile", "binding_vnic_type", "mac_address"]
    # never binding_profile: Neutron's policy reserves it for admins, and it is
    # host-specific (SDD 6.6)
    sdk_params_from_params = ["binding_vnic_type", "mac_address"]
    sdk_params_from_refs = [
        "fixed_ips",
        "network_id",
    ]

    @classmethod
    def from_sdk(cls, conn, sdk_resource):
        obj = super(ServerPort, cls).from_sdk(conn, sdk_resource)
        # the part after the colon varies - it is the availability zone
        if not sdk_resource["device_owner"].startswith("compute:"):
            raise exc.UnexpectedValue(
                "device_owner", "compute:*", sdk_resource["device_owner"]
            )
        return obj

    def _data_from_sdk_and_refs(self, sdk_res, refs):
        super(ServerPort, self)._data_from_sdk_and_refs(sdk_res, refs)
        # For ports recreated through Neutron (SDD 6.6). Not in params_from_*,
        # whose names every export must hold: an export made before this rule
        # stays valid for Nova port creation.
        params = self.params()
        params["security_group_refs"] = refs["security_group_refs"]
        params["is_port_security_enabled"] = sdk_res["is_port_security_enabled"]

    def create_or_update(self, conn, filters=None):
        """Create the port through Neutron with its MAC, every fixed IP and its
        security groups, or use again the unbound port an earlier attempt of the
        cutover created for it (SDD 6.6)."""
        params, info = self.params_and_info()
        if params.get("security_group_refs") is None:
            # Nova applies a server's security groups only to the ports it
            # creates itself: this port would get the project's default group
            raise exc.InconsistentState(
                f"Port {info.get('id')} was exported without its security groups; "
                "export the workload again before recreating its ports through Neutron."
            )
        refs = self._refs_from_ser(conn)
        sdk_params = self._to_sdk_params(refs)
        sdk_params["security_group_ids"] = [
            reference.security_group_id(conn, ref) for ref in params["security_group_refs"]
        ]
        if params.get("is_port_security_enabled") is not None:
            sdk_params["is_port_security_enabled"] = params["is_port_security_enabled"]
        sdk_params["description"] = port_description(info["id"])
        # Neutron allocates a SLAAC or DHCPv6-stateless subnet's EUI-64 address of the MAC on
        # every port it creates, also when it is sent, and a second allocation of one address
        # fails: the address is left out, and an explicit list (maybe empty) keeps Neutron from
        # adding an address of any other subnet (SDD 6.6)
        subnets = list(conn.network.subnets(network_id=sdk_params["network_id"]))
        sdk_params["fixed_ips"] = _requested_ips(sdk_params.get("fixed_ips") or [], subnets)

        sdk_port = self._earlier_port(conn, sdk_params)
        if sdk_port is None:
            sdk_port = conn.network.create_port(**sdk_params)
        return {"port": sdk_port["id"]}

    @staticmethod
    def _earlier_port(conn, sdk_params):
        """The unbound port an earlier attempt created with these addresses, in the
        credential's project, or None. Any other port with the MAC on the network is
        refused before Nova is asked for the server - another project's too, which an
        admin credential sees and Nova never attaches (PortNotUsable, SDD 6.6)."""
        mac = sdk_params.get("mac_address")
        if not mac:
            return None
        # not counting the addresses Neutron derives itself on SLAAC or DHCPv6-stateless
        # subnets: it adds them to every port it creates (SDD 6.6)
        subnets = list(conn.network.subnets(network_id=sdk_params["network_id"]))
        wanted = sorted(
            ip["ip_address"] for ip in _requested_ips(sdk_params.get("fixed_ips") or [], subnets)
        )
        for sdk_port in conn.network.ports(network_id=sdk_params["network_id"], mac_address=mac):
            if sdk_port["mac_address"] != mac or sdk_port["network_id"] != sdk_params["network_id"]:
                continue
            have = sorted(
                ip.get("ip_address") for ip in _requested_ips(sdk_port["fixed_ips"] or [], subnets)
            )
            if (
                sdk_port["description"] == sdk_params["description"]
                and not sdk_port["device_id"]
                and have == wanted
                and sdk_port["project_id"] == conn.current_project_id
            ):
                return sdk_port
            raise exc.InconsistentState(
                f"Port {sdk_port['id']} of the destination network already has MAC {mac} and "
                "is not the unbound port an earlier attempt created for this one; delete it, "
                "or recreate the ports through Nova."
            )
        return None

    def nova_sdk_params(self, conn):
        refs = self._refs_from_ser(conn)

        if len(refs["fixed_ips"]) != 1:
            message = (
                "Using simple port creation via Nova is only supported for a single "
                f"IP address per port, but on network '{refs['network_ref'].get('name', '')}' "
                f"it has these addresses: {refs['fixed_ips_refs']}."
            )
            raise exc.InconsistentState(message)

        return {
            "uuid": refs["network_id"],
            "fixed_ip": refs["fixed_ips"][0]["ip_address"],
        }

    def dst_port_errors(self, conn, port_creation_mode):
        """What would make the import fail to create this port after the source stops,
        looked up now as the import will look it up (SDD 6.6): its network and subnet
        references, whether the credential may put a port on that network, and every fixed
        IP as Neutron's IPAM takes it on the port.

        Returns: list of strings (error messages), empty when the port can be created
        """
        params, info = self.params_and_info()
        where = "port {0} on network '{1}'".format(
            info.get("id"), (params.get("network_ref") or {}).get("name")
        )
        try:
            refs = self._refs_from_ser(conn)
            refused = network_refusal(conn, refs["network_id"])
        except (
            osm_resource.OPENSTACK_EXC.ResourceFailure,
            osm_resource.OPENSTACK_EXC.ResourceNotFound,
            osm_resource.OPENSTACK_EXC.DuplicateResource,
        ) as e:
            return [f"Destination prerequisites not met: {where}: {e}"]
        errors = []
        if refused:
            errors.append(f"Destination prerequisites not met: {where}: {refused}")
        subnets = list(conn.network.subnets(network_id=refs["network_id"]))
        # Nova creates its ports with a new MAC; a port created through Neutron keeps the
        # source MAC
        neutron = port_creation_mode == "neutron"
        if neutron and params.get("security_group_refs") is None:
            # an export from before the rule, which create_or_update refuses (SDD 6.6)
            errors.append(
                f"Destination prerequisites not met: {where}: it was exported without its "
                "security groups; export the workload again before recreating its ports "
                "through Neutron"
            )
        elif neutron:
            # create_or_update looks the port's groups up for the port it creates (SDD 6.6)
            for ref in params["security_group_refs"]:
                try:
                    reference.security_group_id(conn, ref)
                except (
                    osm_resource.OPENSTACK_EXC.ResourceFailure,
                    osm_resource.OPENSTACK_EXC.ResourceNotFound,
                    osm_resource.OPENSTACK_EXC.DuplicateResource,
                ) as e:
                    errors.append(f"Destination prerequisites not met: {where}: {e}")
        if not neutron and len(refs["fixed_ips"]) != 1:
            # nova_sdk_params refuses any other count, after the stop (SDD 6.6)
            addresses = ", ".join(fixed_ip["ip_address"] for fixed_ip in refs["fixed_ips"])
            count = f"{len(refs['fixed_ips'])}: {addresses}" if addresses else "none"
            errors.append(
                f"Destination prerequisites not met: {where}: Nova creates a port with exactly "
                f"one fixed IP, and this one has {count}; keep one address, or recreate the "
                "ports through Neutron (port_creation_mode: neutron)"
            )
        for fixed_ip in refs["fixed_ips"]:
            problem = _address_refusal(fixed_ip, subnets, neutron, params.get("mac_address"))
            if problem:
                errors.append(
                    f"Destination prerequisites not met: {where}: "
                    f"{fixed_ip['ip_address']} {problem}"
                )
        # what another port of the network holds: Neutron refuses its address, and with the MAC
        # kept the MAC - all but the unbound port an earlier attempt created (SDD 6.6)
        mine = None
        if neutron and params.get("mac_address"):
            try:
                mine = self._earlier_port(
                    conn,
                    {
                        "network_id": refs["network_id"],
                        "mac_address": params["mac_address"],
                        "fixed_ips": refs["fixed_ips"],
                        "description": port_description(info["id"]),
                    },
                )
            except exc.InconsistentState as e:
                errors.append(f"Destination prerequisites not met: {where}: {e}")
        for fixed_ip in refs["fixed_ips"]:
            address = fixed_ip["ip_address"]
            holders = [
                sdk_port["id"]
                for sdk_port in conn.network.ports(
                    network_id=refs["network_id"], fixed_ips=[f"ip_address={address}"]
                )
                if any(ip.get("ip_address") == address for ip in sdk_port["fixed_ips"] or [])
                and (mine is None or sdk_port["id"] != mine["id"])
            ]
            if holders:
                errors.append(
                    f"Destination prerequisites not met: {where}: {address} is held by port "
                    f"{holders[0]}"
                )
        return errors

    @staticmethod
    def _refs_from_sdk(conn, sdk_res):
        refs = {}

        refs["fixed_ips"] = sdk_res["fixed_ips"]
        refs["fixed_ips_refs"] = []
        for fixed_ip in sdk_res["fixed_ips"]:
            refs["fixed_ips_refs"].append(
                {
                    "ip_address": fixed_ip["ip_address"],
                    "subnet_ref": reference.subnet_ref(conn, fixed_ip["subnet_id"]),
                }
            )
        refs["network_id"] = sdk_res["network_id"]
        refs["network_ref"] = reference.network_ref(conn, sdk_res["network_id"])
        refs["security_group_refs"] = [
            reference.security_group_ref(conn, security_group_id)
            for security_group_id in sdk_res["security_group_ids"] or []
        ]

        return refs

    def _refs_from_ser(self, conn):
        refs = {}
        params = self.params()

        refs["fixed_ips_refs"] = params["fixed_ips_refs"]
        refs["fixed_ips"] = []
        for fixed_ip in params["fixed_ips_refs"]:
            entry = {"ip_address": fixed_ip["ip_address"]}
            # without subnet_ref only the address is named (a NIC override, SDD 6.6):
            # Neutron picks the subnet of the port's network that holds it
            if fixed_ip.get("subnet_ref"):
                entry["subnet_id"] = reference.subnet_id(conn, fixed_ip["subnet_ref"])
            refs["fixed_ips"].append(entry)
        refs["network_ref"] = params["network_ref"]
        refs["network_id"] = reference.network_id(conn, params["network_ref"])

        return refs


def _host_address(ip, cidr):
    """Neutron's check_subnet_ip (2023.1) for a port created with no owner, as Nova and the
    import create theirs: an IPv4 subnet's network address is never a port's, nor its
    broadcast address below a /31 (netaddr gives a /31 or /32 none); an IPv6 subnet's first
    address is taken from a port without an owner (SDD 6.6)."""
    net = ipaddress.ip_network(cidr, strict=False)
    if ip.version != net.version or ip not in net:
        return False
    if net.version == 4:
        return ip != net.network_address and (
            net.prefixlen >= 31 or ip != net.broadcast_address
        )
    return True


def _auto_address(subnet):
    """Whether Neutron derives a port's address on `subnet` from its MAC (SLAAC or
    DHCPv6-stateless, by address or RA mode)."""
    return (
        subnet.get("ipv6_address_mode") in AUTO_ADDRESS_MODES
        or subnet.get("ipv6_ra_mode") in AUTO_ADDRESS_MODES
    )


def _subnet_of(fixed_ip, subnets):
    """The subnet of the port's network that `fixed_ip` goes to: the one it names, else the
    first that holds its address, as Neutron picks it; None when none does."""
    if fixed_ip.get("subnet_id"):
        return next((s for s in subnets if s["id"] == fixed_ip["subnet_id"]), None)
    try:
        ip = ipaddress.ip_address(fixed_ip["ip_address"])
    except (KeyError, ValueError):
        return None
    return next((s for s in subnets if _host_address(ip, s["cidr"])), None)


def _requested_ips(fixed_ips, subnets):
    """The fixed IPs a port is created with through Neutron: all but those on a SLAAC or
    DHCPv6-stateless subnet, whose EUI-64 address of the MAC Neutron adds itself (SDD 6.6)."""
    out = []
    for fixed_ip in fixed_ips:
        subnet = _subnet_of(fixed_ip, subnets)
        if subnet is None or not _auto_address(subnet):
            out.append(fixed_ip)
    return out


def _eui64(cidr, mac):
    """The address SLAAC gives `mac` on `cidr` (oslo's get_ipv6_addr_by_EUI64): the
    prefix with the MAC's modified EUI-64 interface identifier."""
    octets = [int(part, 16) for part in mac.split(":")]
    interface = octets[:3] + [0xFF, 0xFE] + octets[3:]
    interface[0] ^= 0x02
    prefix = ipaddress.ip_network(cidr, strict=False).network_address
    return ipaddress.IPv6Address(int(prefix) + int.from_bytes(bytes(interface), "big"))


def _address_refusal(fixed_ip, subnets, neutron, mac):
    """Why Neutron's IPAM refuses `fixed_ip` on a port of the network whose subnets are
    `subnets` (2023.1: _get_subnet_for_fixed_ip, check_subnet_ip and
    _test_fixed_ips_for_port), or None (SDD 6.6). The port is created through Neutron
    with `mac` when `neutron`, else by Nova with a new MAC."""
    try:
        ip = ipaddress.ip_address(fixed_ip["ip_address"])
    except ValueError:
        return "is not an IP address"
    # an address alone: Neutron takes the first subnet of the network that holds it
    subnet = _subnet_of(fixed_ip, subnets)
    if fixed_ip.get("subnet_id"):
        if subnet is None:
            return f"names subnet {fixed_ip['subnet_id']}, which is not on the port's network"
        if not _host_address(ip, subnet["cidr"]):
            return f"is no host address of subnet {subnet['cidr']}"
    elif subnet is None:
        return "is no host address of any subnet of the port's network"
    if _auto_address(subnet):
        # Neutron takes an explicit address there only if it derives the same one
        if not neutron or not mac:
            return (
                f"lies on {subnet['cidr']}, a SLAAC or DHCPv6-stateless subnet: through Nova "
                "the port gets a new MAC, and Neutron takes no explicit address there"
            )
        expected = _eui64(subnet["cidr"], mac)
        if ip != expected:
            return (
                f"is not {expected}, the address the SLAAC or DHCPv6-stateless subnet "
                f"{subnet['cidr']} gives MAC {mac}"
            )
    return None
