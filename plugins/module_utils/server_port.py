from __future__ import absolute_import, division, print_function

__metaclass__ = type

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

        sdk_port = self._earlier_port(conn, sdk_params)
        if sdk_port is None:
            sdk_port = conn.network.create_port(**sdk_params)
        return {"port": sdk_port["id"]}

    @staticmethod
    def _earlier_port(conn, sdk_params):
        """The unbound port an earlier attempt created with these addresses, or
        None. Any other port with the MAC on the network is refused before Nova
        is asked for the server."""
        mac = sdk_params.get("mac_address")
        if not mac:
            return None
        wanted = sorted(ip["ip_address"] for ip in sdk_params.get("fixed_ips") or [])
        for sdk_port in conn.network.ports(network_id=sdk_params["network_id"], mac_address=mac):
            if sdk_port["mac_address"] != mac or sdk_port["network_id"] != sdk_params["network_id"]:
                continue
            have = sorted(ip.get("ip_address") for ip in sdk_port["fixed_ips"] or [])
            if (
                sdk_port["description"] == sdk_params["description"]
                and not sdk_port["device_id"]
                and have == wanted
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
