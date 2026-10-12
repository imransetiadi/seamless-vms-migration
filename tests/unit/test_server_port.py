from __future__ import absolute_import, division, print_function

__metaclass__ = type

import openstack
import unittest
from unittest import mock


from ansible_collections.os_migrate.os_migrate.plugins.module_utils import (
    exc,
    reference,
    server_port,
)
from ansible_collections.os_migrate.os_migrate.tests.unit import fake_openstack as fake


def sdk_server_port(**kwargs):
    sdk_params = {
        "allowed_address_pairs": [],
        "binding_host_id": None,
        "binding_profile": None,
        "binding_vif_details": None,
        "binding_vif_type": None,
        "binding_vnic_type": "normal",
        "created_at": "2020-02-24T17:01:49Z",
        "data_plane_status": None,
        "description": "",
        "device_id": "uuid-test-server",
        "device_owner": "compute:avail_zone",
        "dns_assignment": None,
        "dns_domain": None,
        "dns_name": None,
        "extra_dhcp_opts": [],
        "fixed_ips": [
            {
                "subnet_id": "uuid-test-subnet",
                "ip_address": "192.168.0.11",
            },
        ],
        "is_admin_state_up": True,
        "is_port_security_enabled": True,
        "mac_address": "fa:16:3e:ab:cd:ef",
        "name": "",
        "network_id": "uuid-test-net",
        "project_id": "uuid-test-project",
        "propagate_uplink_status": None,
        "qos_network_policy_id": None,
        "qos_policy_id": None,
        "resource_request": None,
        "revision_number": 6,
        "security_group_ids": ["uuid-test-security-group"],
        "status": "ACTIVE",
        "trunk_details": None,
        "updated_at": "2020-02-24T17:02:05Z",
        "id": "uuid-test-server-port",
        "tags": [],
    }
    if kwargs.get("too_many_ips"):
        sdk_params["fixed_ips"].append(
            {
                "subnet_id": "uuid-test-subnet",
                "ip_address": "192.168.0.12",
            }
        )
    if "binding_profile" in kwargs:
        sdk_params["binding_profile"] = kwargs["binding_profile"]
    return openstack.network.v2.port.Port(**sdk_params)


def server_port_refs(**kwargs):
    refs = {
        "fixed_ips": [
            {
                "subnet_id": "uuid-test-subnet",
                "ip_address": "192.168.0.11",
            },
        ],
        "fixed_ips_refs": [
            {
                "subnet_ref": {
                    "name": "test-subnet",
                    "project_name": "test-project",
                    "domain_name": "Default",
                },
                "ip_address": "192.168.0.11",
            },
        ],
        "network_id": "uuid-test-net",
        "network_ref": {
            "name": "test-net",
            "project_name": "test-project",
            "domain_name": "Default",
        },
        "security_group_refs": [
            {
                "name": "web",
                "project_name": "test-project",
                "domain_name": "Default",
            },
        ],
    }
    if kwargs.get("too_many_ips"):
        refs["fixed_ips"].append(
            {
                "subnet_id": "uuid-test-subnet",
                "ip_address": "192.168.0.12",
            }
        )
        refs["fixed_ips_refs"].append(
            {
                "subnet_ref": {
                    "name": "test-subnet",
                    "project_name": "test-project",
                    "domain_name": "Default",
                },
                "ip_address": "192.168.0.12",
            }
        )
    return refs


# "Disconnected" variant of Network resource where we make sure not to
# make requests using `conn`.
class ServerPort(server_port.ServerPort):

    def _refs_from_ser(self, conn):
        too_many_ips = len(self.params()["fixed_ips_refs"]) > 1
        return server_port_refs(too_many_ips=too_many_ips)

    @staticmethod
    def _refs_from_sdk(conn, sdk_res):
        too_many_ips = len(sdk_res["fixed_ips"]) > 1
        return server_port_refs(too_many_ips=too_many_ips)


class TestServerPort(unittest.TestCase):

    def test_serialize_server_port(self):
        sp = ServerPort.from_sdk(None, sdk_server_port())
        params, info = sp.params_and_info()

        self.assertEqual(sp.type(), "openstack.network.ServerPort")
        self.assertEqual(
            params["fixed_ips_refs"],
            [
                {
                    "subnet_ref": {
                        "name": "test-subnet",
                        "project_name": "test-project",
                        "domain_name": "Default",
                    },
                    "ip_address": "192.168.0.11",
                },
            ],
        )
        self.assertEqual(params["network_ref"]["name"], "test-net")
        self.assertEqual(info["id"], "uuid-test-server-port")

    def test_nova_sdk_params(self):
        sp = ServerPort.from_sdk(None, sdk_server_port())
        self.assertEqual(
            sp.nova_sdk_params(None),
            {
                "uuid": "uuid-test-net",
                "fixed_ip": "192.168.0.11",
            },
        )

    def test_nova_sdk_params_too_many_ips(self):
        sp = ServerPort.from_sdk(None, sdk_server_port(too_many_ips=True))
        with self.assertRaises(exc.InconsistentState):
            sp.nova_sdk_params(None)

    def test_ports_sorted_by_nova_order(self):
        sdk_port_stubs = [
            {
                "fixed_ips": [{"ip_address": "192.168.22.11"}],
                "id": "uuid-test-server-port-2",
            },
            {
                "fixed_ips": [{"ip_address": "192.168.3.11"}],
                "id": "uuid-test-server-port-3",
            },
            {
                "fixed_ips": [{"ip_address": "192.168.111.11"}],
                "id": "uuid-test-server-port-1",
            },
        ]
        sdk_server_stub = {
            "addresses": {
                "net-1": [
                    {
                        "OS-EXT-IPS:type": "fixed",
                        "addr": "192.168.111.11",
                        "version": 4,
                    },
                    {"OS-EXT-IPS:type": "floating", "addr": "10.19.2.50", "version": 4},
                ],
                "net-2": [
                    {"OS-EXT-IPS:type": "fixed", "addr": "192.168.22.11", "version": 4},
                ],
                "net-3": [
                    {"OS-EXT-IPS:type": "fixed", "addr": "192.168.3.11", "version": 4},
                    {"OS-EXT-IPS:type": "floating", "addr": "10.19.2.51", "version": 4},
                ],
            },
        }
        sorted_ports = server_port._ports_sorted_by_nova_order(
            sdk_server_stub, sdk_port_stubs
        )
        sorted_ids = list(map(lambda p: p["id"], sorted_ports))
        self.assertEqual(
            sorted_ids,
            [
                "uuid-test-server-port-1",
                "uuid-test-server-port-2",
                "uuid-test-server-port-3",
            ],
        )


MARKER = "os-migrate:uuid-test-server-port"
# what SLAAC gives MAC fa:16:3e:ab:cd:ef on fd00:20::/64
SLAAC_EUI64 = "fd00:20::f816:3eff:feab:cdef"
TWO_IPS = [
    {"subnet_id": "uuid-test-subnet", "ip_address": "192.168.0.11"},
    {"subnet_id": "uuid-test-subnet", "ip_address": "192.168.0.12"},
]


class TestServerPortNeutron(unittest.TestCase):
    """Ports recreated through Neutron keep their MAC, every fixed IP and their
    security groups, and a resumed cutover never creates them twice (SDD 6.6)."""

    def setUp(self):
        self.dst = fake.FakeCloud("dst")
        patcher = mock.patch.object(
            reference,
            "security_group_id",
            side_effect=lambda conn, ref, required=True: "dst-sg-" + ref["name"],
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_serialize_records_the_security_groups_and_port_security(self):
        params = ServerPort.from_sdk(None, sdk_server_port()).params()
        self.assertEqual(
            params["security_group_refs"],
            [{"name": "web", "project_name": "test-project", "domain_name": "Default"}],
        )
        self.assertIs(params["is_port_security_enabled"], True)

    def test_an_export_without_the_neutron_params_stays_valid(self):
        # exported before SDD 6.6's rule: still valid, so Nova port mode is unaffected
        sp = ServerPort.from_sdk(None, sdk_server_port())
        del sp.params()["security_group_refs"]
        del sp.params()["is_port_security_enabled"]
        self.assertEqual(sp.data_errors(), [])

    def test_creates_the_port_with_its_mac_every_ip_and_its_security_groups(self):
        sp = ServerPort.from_sdk(
            None,
            sdk_server_port(too_many_ips=True, binding_profile={"pci_slot": "0000:81:00.1"}),
        )

        result = sp.create_or_update(self.dst)

        [(_, created)] = self.dst.calls_to("create_port")
        # never binding_profile: admin-only in Neutron's policy and host-specific
        self.assertEqual(
            created,
            {
                "network_id": "uuid-test-net",
                "mac_address": "fa:16:3e:ab:cd:ef",
                "fixed_ips": TWO_IPS,
                "security_group_ids": ["dst-sg-web"],
                "is_port_security_enabled": True,
                "binding_vnic_type": "normal",
                "description": MARKER,
            },
        )
        self.assertEqual(result, {"port": list(self.dst.ports)[0]})

    def test_uses_again_the_unbound_port_an_earlier_attempt_created(self):
        self.dst.add_port(
            "p-earlier",
            "fa:16:3e:ab:cd:ef",
            network_id="uuid-test-net",
            description=MARKER,
            fixed_ips=list(reversed(TWO_IPS)),
        )
        sp = ServerPort.from_sdk(None, sdk_server_port(too_many_ips=True))

        self.assertEqual(sp.create_or_update(self.dst), {"port": "p-earlier"})
        self.assertEqual(self.dst.calls_to("create_port"), [])

    def test_refuses_any_other_port_with_the_mac(self):
        others = {
            "p-bound": {"description": MARKER, "device_id": "another-server"},
            "p-foreign": {"description": ""},
            "p-other-ips": {
                "description": MARKER,
                "fixed_ips": [{"subnet_id": "uuid-test-subnet", "ip_address": "192.168.0.99"}],
            },
            # an earlier attempt's port in every way but its project, which an admin credential
            # sees: Nova attaches no port of another project (PortNotUsable, review RT-04)
            "p-other-project": {
                "description": MARKER,
                "fixed_ips": [{"subnet_id": "uuid-test-subnet", "ip_address": "192.168.0.11"}],
                "project_id": "project-elsewhere",
            },
        }
        for port_id, extra in others.items():
            dst = fake.FakeCloud("dst")
            dst.add_port(port_id, "fa:16:3e:ab:cd:ef", network_id="uuid-test-net", **extra)
            sp = ServerPort.from_sdk(None, sdk_server_port())
            with self.assertRaisesRegex(exc.InconsistentState, port_id):
                sp.create_or_update(dst)
            self.assertEqual(dst.calls_to("create_port"), [], port_id)

    def test_looks_only_at_ports_with_the_mac_even_if_the_filter_is_ignored(self):
        self.dst.add_port("p-elsewhere", "fa:16:3e:00:00:99", network_id="uuid-test-net")
        self.dst.network.ports = lambda **query: list(self.dst.ports.values())
        sp = ServerPort.from_sdk(None, sdk_server_port())

        result = sp.create_or_update(self.dst)

        self.assertEqual(len(self.dst.calls_to("create_port")), 1)
        self.assertNotEqual(result, {"port": "p-elsewhere"})

    def test_the_export_looks_up_each_security_group_of_the_port(self):
        # the real lookup, not the disconnected test double's
        def ref(kind):
            return lambda conn, id_, required=True: {"name": kind + ":" + id_}

        with mock.patch.object(reference, "subnet_ref", ref("subnet")), mock.patch.object(
            reference, "network_ref", ref("net")
        ), mock.patch.object(reference, "security_group_ref", ref("sg")):
            refs = server_port.ServerPort._refs_from_sdk(None, sdk_server_port())

        self.assertEqual(refs["security_group_refs"], [{"name": "sg:uuid-test-security-group"}])

    def test_keeps_port_security_off_and_no_security_group(self):
        sp = ServerPort.from_sdk(None, sdk_server_port())
        sp.params()["is_port_security_enabled"] = False
        sp.params()["security_group_refs"] = []

        sp.create_or_update(self.dst)

        [(_, created)] = self.dst.calls_to("create_port")
        self.assertIs(created["is_port_security_enabled"], False)
        self.assertEqual(created["security_group_ids"], [])

    def _auto_address_network(self):
        """The port's network: an IPv4, a SLAAC, a DHCPv6-stateless and a stateful DHCPv6 subnet."""
        for subnet in (
            {"id": "uuid-test-subnet", "cidr": "192.168.0.0/24", "ipv6_address_mode": None, "ipv6_ra_mode": None},
            {"id": "sub-slaac", "cidr": "fd00:20::/64", "ipv6_address_mode": "slaac", "ipv6_ra_mode": "slaac"},
            {"id": "sub-stateless", "cidr": "fd00:30::/64", "ipv6_address_mode": None,
             "ipv6_ra_mode": "dhcpv6-stateless"},
            {"id": "sub-stateful", "cidr": "fd00:40::/64", "ipv6_address_mode": "dhcpv6-stateful",
             "ipv6_ra_mode": "dhcpv6-stateful"},
        ):
            self.dst.subnets[subnet["id"]] = fake.Obj(network_id="uuid-test-net", **subnet)

    def test_never_sends_an_address_neutron_derives_on_a_slaac_or_dhcpv6_stateless_subnet(self):
        """SDD 6.6, review NC-01: Neutron 2023.1 allocates such a subnet's EUI-64 address of the
        MAC on every port it creates, also when the address is sent, and the second allocation
        fails (IpAddressAlreadyAllocated); with the MAC kept it derives the same address."""
        self._auto_address_network()
        ipv4 = {"subnet_id": "uuid-test-subnet", "ip_address": "192.168.0.11"}
        stateful = {"ip_address": "fd00:40::5"}
        sent = [
            ipv4,
            {"subnet_id": "sub-slaac", "ip_address": SLAAC_EUI64},
            # address-only, on the DHCPv6-stateless subnet
            {"ip_address": "fd00:30::f816:3eff:feab:cdef"},
            stateful,
        ]
        sp = ServerPort.from_sdk(None, sdk_server_port())
        sp._refs_from_ser = lambda conn: dict(server_port_refs(), fixed_ips=sent, fixed_ips_refs=[])
        sp.create_or_update(self.dst)
        [(_, created)] = self.dst.calls_to("create_port")
        self.assertEqual(created["fixed_ips"], [ipv4, stateful])

        # an IPv6-only SLAAC port: an explicit empty list, so Neutron adds that address and no other
        self.dst = fake.FakeCloud("dst")
        self._auto_address_network()
        sp._refs_from_ser = lambda conn: dict(
            server_port_refs(), fixed_ips=[sent[1]], fixed_ips_refs=[]
        )
        sp.create_or_update(self.dst)
        [(_, created)] = self.dst.calls_to("create_port")
        self.assertEqual(created["fixed_ips"], [])

    def test_uses_again_the_earlier_port_with_the_addresses_neutron_derived(self):
        """SDD 6.6, review NC-09: the earlier port holds what was asked for and the EUI-64
        addresses Neutron added on SLAAC and DHCPv6-stateless subnets; a resumed cutover uses it
        again, and still refuses one with another address."""
        self._auto_address_network()
        ipv4 = {"subnet_id": "uuid-test-subnet", "ip_address": "192.168.0.11"}
        self.dst.add_port(
            "p-earlier",
            "fa:16:3e:ab:cd:ef",
            network_id="uuid-test-net",
            description=MARKER,
            fixed_ips=[
                ipv4,
                {"subnet_id": "sub-slaac", "ip_address": SLAAC_EUI64},
                {"subnet_id": "sub-stateless", "ip_address": "fd00:30::f816:3eff:feab:cdef"},
            ],
        )
        sp = ServerPort.from_sdk(None, sdk_server_port())
        sp._refs_from_ser = lambda conn: dict(
            server_port_refs(),
            fixed_ips=[ipv4, {"subnet_id": "sub-slaac", "ip_address": SLAAC_EUI64}],
            fixed_ips_refs=[],
        )
        self.assertEqual(sp.create_or_update(self.dst), {"port": "p-earlier"})
        self.assertEqual(self.dst.calls_to("create_port"), [])
        self.dst.ports["p-earlier"]["fixed_ips"][0] = {
            "subnet_id": "uuid-test-subnet",
            "ip_address": "192.168.0.99",
        }
        with self.assertRaises(exc.InconsistentState):
            sp.create_or_update(self.dst)

    def test_a_port_without_a_fixed_ip_is_created_without_one(self):
        # an explicit empty list: Neutron allocates no address, as at the source
        sp = ServerPort.from_sdk(None, sdk_server_port())
        sp._refs_from_ser = lambda conn: dict(server_port_refs(), fixed_ips=[], fixed_ips_refs=[])

        sp.create_or_update(self.dst)

        [(_, created)] = self.dst.calls_to("create_port")
        self.assertEqual(created["fixed_ips"], [])

    def test_refuses_an_export_without_the_security_groups(self):
        # Nova applies a server's security groups only to the ports it creates: such a
        # port would get the project's default group
        sp = ServerPort.from_sdk(None, sdk_server_port())
        del sp.params()["security_group_refs"]

        with self.assertRaisesRegex(exc.InconsistentState, "security groups"):
            sp.create_or_update(self.dst)
        self.assertEqual(self.dst.calls_to("create_port"), [])


class TestAddressOnlyFixedIps(unittest.TestCase):
    """A fixed IP reference without subnet_ref names only its address (SDD 6.6): the control
    plane writes them for a NIC override; Neutron picks the subnet that holds the address."""

    def setUp(self):
        self.subnet_lookups = []

        def subnet_id(conn, ref, required=True):
            self.subnet_lookups.append(ref)
            return "dst-subnet-" + ref["name"]

        for name, fake_fn in (
            ("subnet_id", subnet_id),
            ("network_id", lambda conn, ref, required=True: "dst-net-" + ref["name"]),
        ):
            patcher = mock.patch.object(reference, name, side_effect=fake_fn)
            patcher.start()
            self.addCleanup(patcher.stop)

    def port(self, fixed_ips_refs):
        sp = server_port.ServerPort.from_data(
            {
                "type": "openstack.network.ServerPort",
                "params": {
                    "binding_profile": None,
                    "binding_vnic_type": "normal",
                    "mac_address": "fa:16:3e:ab:cd:ef",
                    "network_ref": {"name": "rhoso-dmz", "project_name": "p", "domain_name": "d"},
                    "fixed_ips_refs": fixed_ips_refs,
                    "security_group_refs": [],
                    "is_port_security_enabled": True,
                },
                "_info": {"id": "uuid-src-port", "device_id": "srv", "device_owner": "compute:nova"},
            }
        )
        return sp

    def test_an_address_only_reference_resolves_no_subnet(self):
        refs = self.port([{"ip_address": "192.168.10.5"}])._refs_from_ser(None)
        self.assertEqual(refs["fixed_ips"], [{"ip_address": "192.168.10.5"}])
        self.assertEqual(self.subnet_lookups, [])
        both = self.port(
            [
                {"ip_address": "10.0.0.5", "subnet_ref": {"name": "app-subnet"}},
                {"ip_address": "192.168.10.5"},
            ]
        )._refs_from_ser(None)
        self.assertEqual(
            both["fixed_ips"],
            [
                {"ip_address": "10.0.0.5", "subnet_id": "dst-subnet-app-subnet"},
                {"ip_address": "192.168.10.5"},
            ],
        )

    def test_nova_and_neutron_take_the_address_alone(self):
        sp = self.port([{"ip_address": "192.168.10.5"}])
        self.assertEqual(
            sp.nova_sdk_params(None), {"uuid": "dst-net-rhoso-dmz", "fixed_ip": "192.168.10.5"}
        )
        dst = fake.FakeCloud("dst")
        sp.create_or_update(dst)
        [(_, created)] = dst.calls_to("create_port")
        self.assertEqual(created["fixed_ips"], [{"ip_address": "192.168.10.5"}])
