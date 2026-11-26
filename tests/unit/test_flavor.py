from __future__ import absolute_import, division, print_function

__metaclass__ = type

import openstack
import unittest
from unittest import mock

from ansible_collections.os_migrate.os_migrate.plugins.module_utils import const, flavor


def sdk_flavor():
    return openstack.compute.v2.flavor.Flavor(
        description="",
        disk=256,
        ephemeral=1,
        extra_specs={},
        id="uuid-test-flavor",
        is_disabled=False,
        is_public=True,
        name="test-flavor",
        ram=128,
        rxtx_factor=1.5,
        swap=64,
        vcpus=2,
        links=[
            {"rel": "self", "href": "http://192.168.122.85/compute/v2.1/flavors/d1"},
            {"rel": "bookmark", "href": "http://192.168.122.85/compute/flavors/d1"},
        ],
    )


def serialized_flavor():
    return {
        const.RES_PARAMS: {
            "description": "",
            "disk": 256,
            "ephemeral": 1,
            "extra_specs": {},
            "is_public": True,
            "links": [
                {
                    "rel": "self",
                    "href": "http://192.168.122.85/compute/v2.1/flavors/d1",
                },
                {"rel": "bookmark", "href": "http://192.168.122.85/compute/flavors/d1"},
            ],
            "name": "test-flavor",
            "ram": 128,
            "rxtx_factor": 1.5,
            "swap": 64,
            "vcpus": 2,
        },
        const.RES_INFO: {
            "id": "uuid-test-flavor",
            "is_disabled": False,
        },
        const.RES_TYPE: "openstack.compute.Flavor",
    }


class TestFlavor(unittest.TestCase):

    def test_serialize_flavor(self):
        sdk_net = sdk_flavor()
        flv = flavor.Flavor.from_sdk(None, sdk_net)  # conn=None
        params, info = flv.params_and_info()

        self.assertEqual(flv.type(), const.RES_TYPE_FLAVOR)
        self.assertEqual(params["description"], "")
        self.assertEqual(params["disk"], 256)
        self.assertEqual(params["ephemeral"], 1)
        self.assertEqual(params["extra_specs"], {})
        self.assertEqual(params["is_public"], True)
        self.assertEqual(params["name"], "test-flavor")
        self.assertEqual(params["ram"], 128)
        self.assertEqual(params["rxtx_factor"], 1.5)
        self.assertEqual(params["swap"], 64)
        self.assertEqual(params["vcpus"], 2)

        self.assertEqual(info["id"], "uuid-test-flavor")
        self.assertEqual(info["is_disabled"], False)

    def test_flavor_sdk_params(self):
        flv = flavor.Flavor.from_data(serialized_flavor())
        sdk_params = flv._to_sdk_params(None)

        self.assertEqual(sdk_params["description"], "")
        self.assertEqual(sdk_params["disk"], 256)
        self.assertEqual(sdk_params["ephemeral"], 1)
        self.assertEqual(sdk_params["is_public"], True)
        self.assertEqual(sdk_params["name"], "test-flavor")
        self.assertEqual(sdk_params["ram"], 128)
        self.assertEqual(sdk_params["rxtx_factor"], 1.5)
        self.assertEqual(sdk_params["swap"], 64)
        self.assertEqual(sdk_params["vcpus"], 2)

        # disallowed params when creating a flavor
        self.assertNotIn("id", sdk_params)
        self.assertNotIn("is_disabled", sdk_params)


def _conn(server_max, existing=None):
    """A connection whose compute endpoint offers microversions up to `server_max`."""
    conn = mock.Mock()
    conn.compute.get_endpoint_data.return_value = mock.Mock(
        min_microversion="2.1", max_microversion=server_max
    )
    conn.compute.find_flavor.return_value = existing
    conn.compute.create_flavor.return_value = sdk_flavor()
    return conn


def _with_rxtx(value):
    """A flavor as the export writes it from a source cloud (below 2.102)."""
    source = sdk_flavor()
    source.rxtx_factor = value
    return flavor.Flavor.from_data(flavor.Flavor.from_sdk(None, source).data)


class TestFlavorRxtxFactorFrom2102(unittest.TestCase):
    """Nova 2.102 removed rxtx_factor from the flavor create body (400 if sent) and from
    responses (SDD 21.7). openstacksdk 4.25 negotiates flavors up to 2.102."""

    def setUp(self):
        patcher = mock.patch.object(flavor.OPENSTACK_SDK_FLAVOR, "_max_microversion", "2.102")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_creates_without_rxtx_factor_at_2_102_and_warns_on_a_dropped_value(self):
        conn = _conn("2.103")
        with self.assertLogs("os-migrate", level="WARNING") as logged:
            self.assertTrue(_with_rxtx(1.5).create_or_update(conn))
        sdk_params = conn.compute.create_flavor.call_args.kwargs
        self.assertNotIn("rxtx_factor", sdk_params)
        self.assertEqual(sdk_params["name"], "test-flavor")
        self.assertIn("rxtx_factor 1.5", "\n".join(logged.output))

    def test_drops_the_default_rxtx_factor_quietly(self):
        conn = _conn("2.103")
        with mock.patch.object(flavor.LOG, "warning") as warning:
            _with_rxtx(1.0).create_or_update(conn)
        self.assertNotIn("rxtx_factor", conn.compute.create_flavor.call_args.kwargs)
        warning.assert_not_called()

    def test_keeps_rxtx_factor_below_2_102(self):
        conn = _conn("2.101")
        _with_rxtx(1.5).create_or_update(conn)
        self.assertEqual(conn.compute.create_flavor.call_args.kwargs["rxtx_factor"], 1.5)

    def test_keeps_rxtx_factor_with_an_sdk_that_stops_below_2_102(self):
        conn = _conn("2.103")
        with mock.patch.object(flavor.OPENSTACK_SDK_FLAVOR, "_max_microversion", "2.61"):
            _with_rxtx(1.5).create_or_update(conn)
        self.assertEqual(conn.compute.create_flavor.call_args.kwargs["rxtx_factor"], 1.5)

    def test_a_flavor_read_back_without_rxtx_factor_needs_no_update(self):
        existing = sdk_flavor()
        existing.rxtx_factor = None  # not returned at 2.102 or later
        conn = _conn("2.103", existing=existing)
        self.assertFalse(_with_rxtx(1.0).create_or_update(conn))
        conn.compute.create_flavor.assert_not_called()
