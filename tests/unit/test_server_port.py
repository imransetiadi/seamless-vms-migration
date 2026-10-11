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
