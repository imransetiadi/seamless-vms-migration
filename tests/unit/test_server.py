from __future__ import absolute_import, division, print_function

__metaclass__ = type

import openstack
import unittest
from unittest import mock

from ansible_collections.os_migrate.os_migrate.plugins.module_utils import server
from ansible_collections.os_migrate.os_migrate.tests.unit.test_keypair import (
    sdk_keypair,
)
from ansible_collections.os_migrate.os_migrate.plugins.module_utils import exc
from ansible_collections.os_migrate.os_migrate.plugins.module_utils import reference
from ansible_collections.os_migrate.os_migrate.tests.unit import fake_openstack as fake


def sdk_server():
    return openstack.compute.v2.server.Server(
        addresses={
            "external_network": [
                {
                    "OS-EXT-IPS-MAC:mac_addr": "fa:16:3e:d7:ae:16",
                    "OS-EXT-IPS:type": "fixed",
                    "addr": "10.19.2.50",
                    "version": 4,
                },
            ],
        },
        description="test server",
        flavor={
            "original_name": "m1.small",
        },
        id="uuid-test-server",
        image=dict(id="uuid-test-image"),
        key_name="test-key",
        name="test-server",
        security_groups=[
            dict(name="testing123"),
            dict(name="default"),
        ],
        status="ACTIVE",
    )


def server_refs():
    return {
        "flavor_id": "uuid-flavor-m1.small",
        "flavor_ref": {
            "name": "m1.small",
            "project_name": "test-project",
            "domain_name": "Default",
        },
        "image_id": "uuid-test-image",
        "image_ref": {
            "name": "test-image",
            "project_name": "test-project",
            "domain_name": "Default",
        },
        "floating_ips": [
            {
                "_info": {
                    "created_at": "2020-11-25T15:26:15Z",
                    "floating_network_id": "uuid-test-external-net",
                    "id": "uuid-test-server-fip",
                    "port_id": "uuid-test-server-port",
                    "router_id": "uuid-test-router",
                    "updated_at": "2020-11-25T15:26:18Z",
                },
                "_migration_params": {},
                "params": {
                    "fixed_ip_address": "192.168.20.7",
                    "floating_ip_address": "172.20.9.135",
                    "tags": [],
                },
                "type": "openstack.network.ServerFloatingIP",
            },
        ],
        "ports": [
            {
                "_info": {
                    "device_id": "uuid-test-server",
                    "device_owner": "compute:None",
                    "id": "uuid-test-server-port",
                },
                "_migration_params": {},
                "params": {
                    "fixed_ips_refs": [
                        {
                            "ip_address": "10.19.2.50",
                            "subnet_ref": {
                                "domain_name": "%auth%",
                                "name": "osm_subnet",
                                "project_name": "%auth%",
                            },
                        },
                    ],
                    "network_ref": {
                        "domain_name": "%auth%",
                        "name": "osm_net",
                        "project_name": "%auth%",
                    },
                },
                "type": "openstack.network.ServerPort",
            },
        ],
        "security_group_ids": [
            "uuid-secgroup-default",
            "uuid-secgroup-testing123",
        ],
        "security_group_refs": [
            {
                "name": "default",
                "project_name": "test-project",
                "domain_name": "Default",
            },
            {
                "name": "testing123",
                "project_name": "test-project",
                "domain_name": "Default",
            },
        ],
        "volumes": [
            {
                "_info": {
                    "attachments": [
                        {
                            "attached_at": "2021-07-22T12:14:23.000000",
                            "attachment_id": "uuid-test-attachment",
                            "device": "/dev/vdb",
                            "host_name": "compute-1.jistr-13.local",
                            "id": "uuid-test-volume",
                            "server_id": "uuid-test-server",
                            "volume_id": "uuid-test-volume",
                        },
                    ],
                    "id": "uuid-test-volume",
                    "is_bootable": False,
                    "size": 1,
                },
                "_migration_params": {},
                "params": {
                    "availability_zone": "nova",
                    "description": None,
                    "name": "test-volume",
                    "volume_type": "tripleo",
                },
                "type": "openstack.network.ServerVolume",
            },
        ],
    }


class Server(server.Server):
    def _refs_from_ser(self, conn):
        return server_refs()

    @staticmethod
    def _refs_from_sdk(conn, sdk_res):
        return server_refs()


class TestServer(unittest.TestCase):
    def test_serialize_server(self):
        srv = Server.from_sdk(None, sdk_server())
        params, info = srv.params_and_info()

        self.assertEqual(srv.type(), "openstack.compute.Server")
        self.assertEqual(
            params["ports"][0]["params"]["fixed_ips_refs"],
            [
                {
                    "ip_address": "10.19.2.50",
                    "subnet_ref": {
                        "domain_name": "%auth%",
                        "name": "osm_subnet",
                        "project_name": "%auth%",
                    },
                },
            ],
        )
        self.assertEqual(params["description"], "test server")
        self.assertEqual(params["flavor_ref"]["name"], "m1.small")
        self.assertEqual(info["id"], "uuid-test-server")
        self.assertEqual(params["key_name"], "test-key")
        self.assertEqual(params["name"], "test-server")
        self.assertEqual(params["security_group_refs"][0]["name"], "default")
        self.assertEqual(params["security_group_refs"][1]["name"], "testing123")
        self.assertEqual(info["status"], "ACTIVE")

    def test_block_device_mapping_boot_disk_copy(self):
        srv = Server.from_sdk(None, sdk_server())
        sdk_params = srv.sdk_params(None)

        block_dev_map = [
            {
                "boot_index": -1,
                "delete_on_termination": False,
                "destination_type": "volume",
                "device_name": "sdb",
                "source_type": "volume",
                "uuid": "uuid-some-volume",
            }
        ]
        srv.update_sdk_params_block_device_mapping_copy(sdk_params, block_dev_map)
        self.assertEqual(
            sdk_params["block_device_mapping"],
            [
                {
                    "boot_index": 0,
                    "delete_on_termination": True,
                    "destination_type": "local",
                    "source_type": "image",
                    "uuid": "uuid-test-image",
                },
                {
                    "boot_index": -1,
                    "delete_on_termination": False,
                    "destination_type": "volume",
                    "device_name": "sdb",
                    "source_type": "volume",
                    "uuid": "uuid-some-volume",
                },
            ],
        )
        self.assertEqual(sdk_params["image_id"], "uuid-test-image")

    def test_block_device_mapping_boot_disk_nocopy(self):
        srv = Server.from_sdk(None, sdk_server())
        srv.migration_params()["boot_disk_copy"] = True
        sdk_params = srv.sdk_params(None)

        block_dev_map = [
            {
                "boot_index": 0,
                "delete_on_termination": True,
                "destination_type": "volume",
                "device_name": "sda",
                "source_type": "volume",
                "uuid": "uuid-boot-volume",
            },
            {
                "boot_index": -1,
                "delete_on_termination": False,
                "destination_type": "volume",
                "device_name": "sdb",
                "source_type": "volume",
                "uuid": "uuid-some-volume",
            },
        ]
        srv.update_sdk_params_block_device_mapping_copy(sdk_params, block_dev_map)
        self.assertEqual(
            sdk_params["block_device_mapping"],
            [
                {
                    "boot_index": 0,
                    "delete_on_termination": True,
                    "destination_type": "volume",
                    "device_name": "sda",
                    "source_type": "volume",
                    "uuid": "uuid-boot-volume",
                },
                {
                    "boot_index": -1,
                    "delete_on_termination": False,
                    "destination_type": "volume",
                    "device_name": "sdb",
                    "source_type": "volume",
                    "uuid": "uuid-some-volume",
                },
            ],
        )
        self.assertTrue("image_id" not in sdk_params)

    def test_block_device_mapping_boot_disk_nocopy_data_copy_false_no_additional_volumes_boot_image(
        self,
    ):
        srv = Server.from_sdk(None, sdk_server())
        srv.migration_params()["data_copy"] = False
        sdk_params = srv.sdk_params(None)

        srv.update_sdk_params_block_device_mapping_nocopy(sdk_params)
        self.assertEqual(
            sdk_params["block_device_mapping"],
            [
                {
                    "boot_index": 0,
                    "delete_on_termination": True,
                    "destination_type": "local",
                    "source_type": "image",
                    "uuid": "uuid-test-image",
                }
            ],
        )
        self.assertEqual(sdk_params["image_id"], "uuid-test-image")

    def test_block_device_mapping_boot_disk_nocopy_data_copy_false_inconsistent_state(
        self,
    ):
        srv = Server.from_sdk(None, sdk_server())
        srv.migration_params()["data_copy"] = False
        sdk_params = srv.sdk_params(None)
        sdk_params["image_id"] = None

        with self.assertRaises(exc.InconsistentState):
            srv.update_sdk_params_block_device_mapping_nocopy(sdk_params)

    def test_block_device_mapping_boot_disk_nocopy_data_copy_false_no_additional_volumes(
        self,
    ):
        srv = Server.from_sdk(None, sdk_server())
        srv.migration_params()["data_copy"] = False
        srv.migration_params()["boot_volume"]["uuid"] = "uuid-new-boot-volume"
        srv.migration_params()["additional_volumes"] = []
        sdk_params = srv.sdk_params(None)

        srv.update_sdk_params_block_device_mapping_nocopy(sdk_params)
        self.assertEqual(
            sdk_params["block_device_mapping"],
            [
                {
                    "boot_index": 0,
                    "uuid": "uuid-new-boot-volume",
                    "delete_on_termination": False,
                    "destination_type": "volume",
                    "source_type": "volume",
                }
            ],
        )

    def test_block_device_mapping_boot_disk_nocopy_data_copy_false_additional_volumes(
        self,
    ):
        srv = Server.from_sdk(None, sdk_server())
        srv.migration_params()["data_copy"] = False
        # Testing additional volume parameters
        extra_params = [
            {"uuid": "uuid-extra-volume"},
            {"uuid": "uuid-extra2-volume"},
            {"uuid": None},
        ]
        srv.migration_params()["additional_volumes"] = extra_params
        srv.migration_params()["boot_volume"]["uuid"] = "uuid-new-boot-volume"
        sdk_params = srv.sdk_params(None)

        srv.update_sdk_params_block_device_mapping_nocopy(sdk_params)
        self.assertEqual(
            sdk_params["block_device_mapping"],
            [
                {
                    "boot_index": 0,
                    "uuid": "uuid-new-boot-volume",
                    "delete_on_termination": False,
                    "destination_type": "volume",
                    "source_type": "volume",
                },
                {
                    "boot_index": -1,
                    "uuid": "uuid-extra-volume",
                    "delete_on_termination": False,
                    "destination_type": "volume",
                    "source_type": "volume",
                },
                {
                    "boot_index": -1,
                    "uuid": "uuid-extra2-volume",
                    "delete_on_termination": False,
                    "destination_type": "volume",
                    "source_type": "volume",
                },
            ],
        )

    def test_keypair_dst_prerequisites_errors(self):
        # When checking the prerequisites for a server/workload migration are present in destination environment
        # we explicitly ensure that the keypair in the server/workload metadata is present.
        # This test gates any regression where we try and migrate a server/workload where the keypair in the server
        # metadata is not present in the destination environment
        srv = Server.from_sdk(None, sdk_server())
        conn = mock.Mock()
        # the ports and floating IPs are looked up too (SDD 6.6, TestServerDstNetworkCheck)
        for cls, name in ((server.ServerPort, "dst_port_errors"), (server.ServerFloatingIP, "dst_errors")):
            patcher = mock.patch.object(cls, name, return_value=[])
            patcher.start()
            self.addCleanup(patcher.stop)

        # This test ensures that if the keypair is present then no keypair error is present.
        with mock.patch.object(conn.compute, "find_keypair") as mocked_find_keypair:
            # Mock `conn.compute.find_keypair` to return a dummy keypair object
            mocked_find_keypair.return_value = sdk_keypair()
            errors = srv.dst_prerequisites_errors(conn, None)
            # Assert that the errors list is empty
            self.assertFalse(errors)

        # More importantly this test ensures that if the keypair is not present then
        # 'Destination keypair prerequisites not met: Keypair not found' is present in the errors list.
        with mock.patch.object(conn.compute, "find_keypair") as mocked_find_keypair:
            mocked_find_keypair.side_effect = openstack.exceptions.ResourceNotFound(
                "Keypair not found"
            )
            errors = srv.dst_prerequisites_errors(conn, None)
            # Assert that the errors list is not empty
            self.assertTrue(errors)
            # Assert that the "'Destination keypair prerequisites not met: Keypair not found'" text present in
            # the errors list
            self.assertIn(
                "Destination keypair prerequisites not met: Keypair not found", errors
            )

        # This test ensures that if the keypair is not present no lookup is performed.
        srv.data["params"]["key_name"] = None
        with mock.patch.object(conn.compute, "find_keypair") as mocked_find_keypair:
            errors = srv.dst_prerequisites_errors(conn, None)
            # Assert that the keypair lookup was not performed
            mocked_find_keypair.assert_not_called()
            # Assert that the errors list is empty
            self.assertFalse(errors)


def network_port(port_id, network, fixed_ips_refs, mac="fa:16:3e:00:00:01"):
    return {
        "type": "openstack.network.ServerPort",
        "params": {
            "binding_profile": None,
            "binding_vnic_type": "normal",
            "mac_address": mac,
            "network_ref": {"name": network, "project_name": "%auth%", "domain_name": "%auth%"},
            "fixed_ips_refs": fixed_ips_refs,
            "security_group_refs": [],
            "is_port_security_enabled": True,
        },
        "_info": {"id": port_id, "device_id": "srv", "device_owner": "compute:nova"},
        "_migration_params": {},
    }


def floating_ip(address, network="public"):
    return {
        "type": "openstack.network.ServerFloatingIP",
        "params": {
            "fixed_ip_address": "10.0.0.5",
            "floating_ip_address": address,
            "floating_network_ref": {
                "name": network,
                "project_name": "admin",
                "domain_name": "Default",
            },
            "qos_policy_ref": None,
            "tags": [],
        },
        "_info": {"id": "fip-" + address},
        "_migration_params": {},
    }


class TestServerDstNetworkCheck(unittest.TestCase):
    """import_workload_dst_check looks up every port's network and subnets, checks every
    address as Neutron's IPAM will take it, and a new floating IP's network, before the
    source is stopped (SDD 6.6): the import would fail on them after the stop."""

    def setUp(self):
        self.dst = fake.FakeCloud("dst")
        for subnet in (
            {"id": "sub-app-v4", "network_id": "net-app", "cidr": "10.0.0.0/24"},
            {"id": "sub-app-v6", "network_id": "net-app", "cidr": "fd00:10::/64",
             "ipv6_address_mode": "dhcpv6-stateful", "ipv6_ra_mode": "dhcpv6-stateful"},
            {"id": "sub-slaac", "network_id": "net-slaac", "cidr": "fd00:20::/64",
             "ipv6_address_mode": "slaac", "ipv6_ra_mode": "slaac"},
            {"id": "sub-p2p", "network_id": "net-p2p", "cidr": "10.255.0.0/31"},
            {"id": "sub-provider", "network_id": "net-provider", "cidr": "10.0.0.0/24"},
            {"id": "sub-private-b", "network_id": "net-private-b", "cidr": "10.0.0.0/24"},
            {"id": "sub-ext", "network_id": "net-ext", "cidr": "10.0.0.0/24"},
        ):
            self.dst.subnets[subnet["id"]] = fake.Obj(
                {"ipv6_address_mode": None, "ipv6_ra_mode": None, **subnet}
            )
        networks = {
            "app": "net-app", "slaac": "net-slaac", "p2p": "net-p2p", "public": "net-public",
            "provider": "net-provider", "private-b": "net-private-b", "ext": "net-ext",
        }
        # the credential's own networks, and three of other projects: shared, private, external
        for net_id, project, shared, external in (
            ("net-app", self.dst.current_project_id, False, False),
            ("net-slaac", self.dst.current_project_id, False, False),
            ("net-p2p", self.dst.current_project_id, False, False),
            ("net-public", self.dst.current_project_id, False, True),
            ("net-provider", "project-admin", True, False),
            ("net-private-b", "project-b", False, False),
            ("net-ext", "project-admin", False, True),
        ):
            self.dst.networks[net_id] = fake.Obj(
                id=net_id, project_id=project, is_shared=shared, is_router_external=external
            )

        def network_id(conn, ref, required=True):
            if ref["name"] == "twice":
                raise openstack.exceptions.DuplicateResource("More than one Network exists with the name 'twice'.")
            if ref["name"] not in networks:
                raise openstack.exceptions.ResourceNotFound("No Network found for " + ref["name"])
            return networks[ref["name"]]

        def subnet_id(conn, ref, required=True):
            if ref["name"] == "gone-subnet":
                raise openstack.exceptions.ResourceNotFound("No Subnet found for gone-subnet")
            return ref["name"]

        for name, fake_fn in (
            ("network_id", network_id),
            ("subnet_id", subnet_id),
            ("flavor_id", lambda conn, ref, required=True: "flavor-1"),
            ("image_id", lambda conn, ref, required=True: "image-1"),
            ("security_group_id", lambda conn, ref, required=True: "sg-1"),
            ("qos_policy_id", lambda conn, ref, required=True: None),
        ):
            patcher = mock.patch.object(reference, name, side_effect=fake_fn)
            patcher.start()
            self.addCleanup(patcher.stop)

    def errors(self, ports, mode="nova", floating_ips=(), fip_mode="auto"):
        srv = server.Server.from_data(
            {
                "type": "openstack.compute.Server",
                "params": {
                    "name": "web-01",
                    "key_name": None,
                    "flavor_ref": {"name": "m1", "project_name": None, "domain_name": None},
                    "image_ref": None,
                    "security_group_refs": [],
                    "ports": list(ports),
                    "floating_ips": list(floating_ips),
                    "volumes": [],
                },
                "_info": {"id": "srv"},
                "_migration_params": {
                    "data_copy": True,
                    "boot_disk_copy": True,
                    "port_creation_mode": mode,
                    "floating_ip_mode": fip_mode,
                },
            }
        )
        return srv.dst_prerequisites_errors(self.dst, None)

    def test_finds_every_port_network_and_subnet_before_the_stop(self):
        app = [{"ip_address": "10.0.0.5"}]
        self.assertEqual(self.errors([network_port("p1", "app", app)]), [])
        [missing] = self.errors([network_port("p1", "provider-elsewhere", app)])
        self.assertIn("Destination prerequisites not met: port p1 on network 'provider-elsewhere'", missing)
        self.assertIn("No Network found", missing)
        [twice] = self.errors([network_port("p2", "twice", app)])
        self.assertIn("port p2 on network 'twice'", twice)
        self.assertIn("More than one Network", twice)
        gone = [{"ip_address": "10.0.0.5", "subnet_ref": {"name": "gone-subnet"}}]
        [subnet] = self.errors([network_port("p3", "app", gone)])
        self.assertIn("port p3 on network 'app'", subnet)
        self.assertIn("No Subnet found for gone-subnet", subnet)

    def test_refuses_a_network_the_credential_may_not_put_a_port_on(self):
        """SDD 6.6: another project's network takes the port only when it is shared with the
        credential's project or external, and only from a token with the admin or advsvc role:
        Neutron's policy lets only those, or the network's project, choose the address or MAC
        that the import asks for - after the source stops."""
        app = [{"ip_address": "10.0.0.5"}]
        for mode in ("nova", "neutron"):
            # the credential's own network, whatever its roles
            self.assertEqual(self.errors([network_port("p1", "app", app)], mode=mode), [])
            [private] = self.errors([network_port("p1", "private-b", app)], mode=mode)
            self.assertEqual(
                "Destination prerequisites not met: port p1 on network 'private-b': the network "
                "belongs to project project-b and is neither shared with the credential's project "
                "nor external",
                private,
            )
            for name in ("provider", "ext"):
                [member] = self.errors([network_port("p1", name, app)], mode=mode)
                self.assertEqual(
                    f"Destination prerequisites not met: port p1 on network '{name}': the "
                    "network belongs to project project-admin, and Neutron lets only the admin or "
                    "advsvc role, or that project, choose a port's address or MAC there; the "
                    "destination credential's token has neither role",
                    member,
                )
        for roles in (["admin", "member", "reader"], ["advsvc"]):
            self.dst.roles = roles
            for mode in ("nova", "neutron"):
                self.assertEqual(self.errors([network_port("p1", "provider", app)], mode=mode), [])
                self.assertEqual(self.errors([network_port("p1", "ext", app)], mode=mode), [])
                # the admin sees every project's networks, yet never puts the VM on another
                # tenant's private one
                [private] = self.errors([network_port("p1", "private-b", app)], mode=mode)
                self.assertIn("is neither shared with the credential's project nor external", private)

    def test_refuses_an_address_neutron_would_refuse_on_the_port(self):
        def check(address, network="app", subnet=None, mode="nova"):
            ref = {"ip_address": address}
            if subnet:
                ref["subnet_ref"] = {"name": subnet}
            return self.errors([network_port("p1", network, [ref])], mode=mode)

        # Neutron picks the subnet that holds an address-only one
        self.assertEqual(check("10.0.0.5"), [])
        [outside] = check("192.168.10.5")
        self.assertIn("port p1 on network 'app': 192.168.10.5 is no host address of any subnet", outside)
        self.assertIn("is no host address", check("10.0.0.0")[0])
        self.assertIn("is no host address", check("10.0.0.255")[0])
        # a /31 has no broadcast address, but its network address is still refused
        self.assertEqual(check("10.255.0.1", network="p2p"), [])
        self.assertIn("is no host address", check("10.255.0.0", network="p2p")[0])
        # a subnet named by reference: the address must lie in it, and it on the port's network
        self.assertEqual(check("10.0.0.5", subnet="sub-app-v4"), [])
        self.assertIn("is no host address of subnet 10.0.0.0/24", check("10.0.1.5", subnet="sub-app-v4")[0])
        self.assertIn("not on the port's network", check("10.0.0.5", subnet="sub-p2p")[0])
        # an IPv6 subnet's first address: Nova and the import create ports with no owner, and
        # check_subnet_ip takes it for one (review NC-08)
        self.assertEqual(check("fd00:10::"), [])
        self.assertEqual(check("fd00:10::", mode="neutron"), [])

    def test_refuses_a_nova_port_without_exactly_one_fixed_ip(self):
        """SDD 6.6, review NC-05: Nova creates each port with one address, and the import
        refuses any other count only after the source stopped; the check refuses it before."""
        two = [{"ip_address": "10.0.0.5"}, {"ip_address": "fd00:10::5"}]
        [several] = self.errors([network_port("p1", "app", two)])
        self.assertIn("Destination prerequisites not met: port p1 on network 'app'", several)
        self.assertIn(
            "Nova creates a port with exactly one fixed IP, and this one has 2: "
            "10.0.0.5, fd00:10::5",
            several,
        )
        [none] = self.errors([network_port("p2", "app", [])])
        self.assertIn("port p2 on network 'app'", none)
        self.assertIn("exactly one fixed IP, and this one has none", none)
        self.assertEqual(self.errors([network_port("p3", "app", two[:1])]), [])
        # through Neutron every address is kept, none included
        self.assertEqual(self.errors([network_port("p1", "app", two)], mode="neutron"), [])
        self.assertEqual(self.errors([network_port("p2", "app", [])], mode="neutron"), [])

    def test_looks_up_a_neutron_ports_security_groups_before_the_stop(self):
        """SDD 6.6: create_or_update looks the port's groups up for the port it creates, after
        the stop; the check looks them up before it. Through Nova the port's own groups are
        never looked up."""
        def security_group_id(conn, ref, required=True):
            if ref["name"] == "gone":
                raise openstack.exceptions.ResourceNotFound("No SecurityGroup found for gone")
            if ref["name"] == "twice":
                raise openstack.exceptions.DuplicateResource(
                    "More than one SecurityGroup exists with the name 'twice'."
                )
            return "sg-" + ref["name"]

        def port(*names):
            data = network_port("p1", "app", [{"ip_address": "10.0.0.5"}])
            data["params"]["security_group_refs"] = [
                {"name": name, "project_name": "%auth%", "domain_name": "%auth%"} for name in names
            ]
            return data

        with mock.patch.object(reference, "security_group_id", side_effect=security_group_id):
            self.assertEqual(self.errors([port("web", "default")], mode="neutron"), [])
            [gone] = self.errors([port("web", "gone")], mode="neutron")
            self.assertIn("Destination prerequisites not met: port p1 on network 'app'", gone)
            self.assertIn("No SecurityGroup found for gone", gone)
            [twice] = self.errors([port("twice")], mode="neutron")
            self.assertIn("More than one SecurityGroup exists with the name 'twice'", twice)
            self.assertEqual(self.errors([port("gone")], mode="nova"), [])

    def test_follows_neutron_on_slaac_and_dhcpv6_stateless_subnets(self):
        def check(address, mode, mac="fa:16:3e:00:00:01"):
            port = network_port("p1", "slaac", [{"ip_address": address}], mac=mac)
            return self.errors([port], mode=mode)

        # the kept MAC's EUI-64 address on the prefix: what Neutron derives itself
        self.assertEqual(check("fd00:20::f816:3eff:fe00:1", "neutron"), [])
        self.assertEqual(check("FD00:20::F816:3EFF:FE00:1", "neutron", mac="FA:16:3E:00:00:01"), [])
        [other] = check("fd00:20::5", "neutron")
        self.assertIn("fd00:20::5 is not fd00:20::f816:3eff:fe00:1", other)
        # through Nova the port gets a new MAC: no explicit address there at all
        [nova] = check("fd00:20::f816:3eff:fe00:1", "nova")
        self.assertIn("SLAAC or DHCPv6-stateless", nova)
        # a stateful DHCPv6 subnet takes any host address
        self.assertEqual(self.errors([network_port("p1", "app", [{"ip_address": "fd00:10::5"}])]), [])

    def test_looks_up_the_network_a_new_floating_ip_needs(self):
        port = network_port("p1", "app", [{"ip_address": "10.0.0.5"}])
        self.assertEqual(self.errors([port], floating_ips=[floating_ip("172.20.9.135")]), [])
        [missing] = self.errors([port], floating_ips=[floating_ip("172.20.9.135", network="gone")])
        self.assertIn("Destination prerequisites not met: floating IP 172.20.9.135 on network 'gone'", missing)
        self.assertIn("No Network found", missing)
        self.assertIn(
            "floating IP 172.20.9.135",
            self.errors([port], floating_ips=[floating_ip("172.20.9.135", network="gone")], fip_mode="new")[0],
        )
        # nothing to create: skipped, or a detached floating IP with the address is attached
        self.assertEqual(
            self.errors([port], floating_ips=[floating_ip("172.20.9.135", network="gone")], fip_mode="skip"), []
        )
        self.dst.floating_ips["fip-1"] = fake.Obj(
            id="fip-1", floating_ip_address="172.20.9.135", port_id=None
        )
        self.assertEqual(self.errors([port], floating_ips=[floating_ip("172.20.9.135", network="gone")]), [])
        self.assertEqual(
            self.errors([port], floating_ips=[floating_ip("172.20.9.135", network="gone")], fip_mode="existing"), []
        )

    def test_names_a_floating_ip_the_import_could_not_attach(self):
        port = network_port("p1", "app", [{"ip_address": "10.0.0.5"}])
        [none] = self.errors([port], floating_ips=[floating_ip("172.20.9.135")], fip_mode="existing")
        self.assertIn("floating IP 172.20.9.135", none)
        self.assertIn("No floating IPs found with address 172.20.9.135", none)
        self.dst.floating_ips["fip-1"] = fake.Obj(
            id="fip-1", floating_ip_address="172.20.9.135", port_id="someone-else"
        )
        [attached] = self.errors([port], floating_ips=[floating_ip("172.20.9.135")])
        self.assertIn("already", attached)

    def test_reports_every_problem_with_the_keypairs_and_takes_the_default_modes(self):
        # an export without the two migration params: Nova creates the ports and the
        # floating IP mode is auto (Server.migration_param_defaults)
        srv = server.Server.from_data(
            {
                "type": "openstack.compute.Server",
                "params": {
                    "name": "web-01",
                    "key_name": "gone-key",
                    "flavor_ref": {"name": "m1", "project_name": None, "domain_name": None},
                    "image_ref": None,
                    "security_group_refs": [],
                    "ports": [
                        network_port("ok", "app", [{"ip_address": "10.0.0.5"}]),
                        network_port("v6", "slaac", [{"ip_address": "fd00:20::f816:3eff:fe00:1"}]),
                        network_port("lost", "gone", [{"ip_address": "10.0.0.6"}]),
                    ],
                    "floating_ips": [floating_ip("172.20.9.135", network="gone")],
                    "volumes": [],
                },
                "_info": {"id": "srv"},
                "_migration_params": {"data_copy": True, "boot_disk_copy": True},
            }
        )
        self.dst.compute.find_keypair = mock.Mock(
            side_effect=openstack.exceptions.ResourceNotFound("Keypair not found")
        )
        errors = srv.dst_prerequisites_errors(self.dst, None)
        self.assertEqual(len(errors), 4, errors)
        self.assertIn("Destination keypair prerequisites not met: Keypair not found", errors)
        self.assertTrue(any("port v6 on network 'slaac'" in e and "SLAAC" in e for e in errors), errors)
        self.assertTrue(any("port lost on network 'gone'" in e for e in errors), errors)
        self.assertTrue(any("floating IP 172.20.9.135 on network 'gone'" in e for e in errors), errors)
        self.assertFalse(any("port ok" in e for e in errors), errors)

    def test_refuses_an_address_or_mac_another_port_holds(self):
        """SDD 6.6: Neutron refuses an address another port of the network holds, and with the MAC
        kept the MAC too; only the unbound port an earlier attempt created is used again."""
        app = [{"ip_address": "10.0.0.5"}]
        self.dst.ports["taken"] = fake.Obj(
            id="taken", network_id="net-app", mac_address="fa:16:3e:99:99:99",
            fixed_ips=[{"ip_address": "10.0.0.5", "subnet_id": "sub-app-v4"}],
            device_id="other-vm", description="",
        )
        for mode in ("nova", "neutron"):
            [held] = self.errors([network_port("p1", "app", app)], mode=mode)
            self.assertIn("port p1 on network 'app': 10.0.0.5 is held by port taken", held)
        del self.dst.ports["taken"]
        # an earlier attempt's port also holds what Neutron derived on a SLAAC subnet: still the
        # one create_or_update uses again (SDD 6.6, review NC-09)
        self.dst.subnets["sub-app-slaac"] = fake.Obj(
            id="sub-app-slaac", network_id="net-app", cidr="fd00:50::/64",
            ipv6_address_mode="slaac", ipv6_ra_mode="slaac",
        )
        self.dst.ports["mine"] = fake.Obj(
            id="mine", network_id="net-app", mac_address="fa:16:3e:00:00:01",
            fixed_ips=[
                {"ip_address": "10.0.0.5", "subnet_id": "sub-app-v4"},
                {"ip_address": "fd00:50::f816:3eff:fe00:1", "subnet_id": "sub-app-slaac"},
            ],
            device_id="", description="os-migrate:p1",
        )
        self.assertEqual(self.errors([network_port("p1", "app", app)], mode="neutron"), [])
        del self.dst.ports["mine"], self.dst.subnets["sub-app-slaac"]
        # the earlier attempt's own unbound port holds it: used again, nothing to refuse
        self.dst.ports["mine"] = fake.Obj(
            id="mine", network_id="net-app", mac_address="fa:16:3e:00:00:01",
            fixed_ips=[{"ip_address": "10.0.0.5", "subnet_id": "sub-app-v4"}],
            device_id="", description="os-migrate:p1",
        )
        self.assertEqual(self.errors([network_port("p1", "app", app)], mode="neutron"), [])
        del self.dst.ports["mine"]
        # another port with the MAC: Neutron refuses the MAC when it is kept, Nova gives a new one
        self.dst.ports["same-mac"] = fake.Obj(
            id="same-mac", network_id="net-app", mac_address="fa:16:3e:00:00:01",
            fixed_ips=[{"ip_address": "10.0.0.77", "subnet_id": "sub-app-v4"}],
            device_id="other-vm", description="",
        )
        [mac] = self.errors([network_port("p1", "app", app)], mode="neutron")
        self.assertIn("port p1 on network 'app'", mac)
        self.assertIn("already has MAC fa:16:3e:00:00:01", mac)
        self.assertEqual(self.errors([network_port("p1", "app", app)], mode="nova"), [])
