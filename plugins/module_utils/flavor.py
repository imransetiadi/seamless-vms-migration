from __future__ import absolute_import, division, print_function

__metaclass__ = type

import logging

try:
    import openstack
    from keystoneauth1 import discover
    from openstack import utils as sdk_utils
    HAS_OPENSTACK = True
    OPENSTACK_SDK_FLAVOR = openstack.compute.v2.flavor.Flavor
except ImportError:
    HAS_OPENSTACK = False
    OPENSTACK_SDK_FLAVOR = None

LOG = logging.getLogger("os-migrate")

#: Compute API 2.102 removed rxtx_factor from the flavor create body (sending it is a 400) and
#: from every flavor response (SDD 21.7); openstacksdk 4.25 negotiates flavors up to 2.102.
RXTX_FACTOR_REMOVED_IN = (2, 102)
RXTX_FACTOR_DEFAULT = 1.0


def _flavor_microversion_drops_rxtx_factor(conn):
    """Whether this SDK's flavor calls on `conn` use a microversion without rxtx_factor: the
    one the SDK itself negotiates (its Flavor maximum against the cloud's)."""
    client_max = getattr(OPENSTACK_SDK_FLAVOR, "_max_microversion", None)
    negotiated = sdk_utils.maximum_supported_microversion(conn.compute, client_max)
    if not negotiated:
        return False
    return discover.normalize_version_number(negotiated) >= RXTX_FACTOR_REMOVED_IN

from ansible_collections.os_migrate.os_migrate.plugins.module_utils import (
    const,
    osm_resource,
)


class Flavor(osm_resource.Resource):
    resource_type = const.RES_TYPE_FLAVOR
    sdk_class = OPENSTACK_SDK_FLAVOR

    info_from_sdk = [
        "id",
        # There doesn't seem to be an API for disabling a flavor.
        "is_disabled",
    ]

    params_from_sdk = [
        "description",
        "disk",
        "ephemeral",
        "extra_specs",
        "is_public",
        "name",
        "ram",
        "rxtx_factor",
        "swap",
        "vcpus",
    ]
    sdk_params_from_params = [x for x in params_from_sdk if x not in ["extra_specs"]]

    def _data_from_sdk_and_refs(self, sdk_res, refs):
        super()._data_from_sdk_and_refs(sdk_res, refs)
        params = self.params()
        # APIs before microversion 2.75 return '' instead of 0, but
        # only 0 is accepted when creating a flavor.
        if params["swap"] == "":
            params["swap"] = 0

    def _hook_after_update(self, conn, sdk_res, is_create):
        sdk_extra_specs = sdk_res.get("extra_specs") or {}
        params_extra_specs = self.params().get("extra_specs") or {}
        delete_extra_specs = list(
            set(sdk_extra_specs.keys()) - set(params_extra_specs.keys())
        )
        for prop_name in delete_extra_specs:
            # We can use the 'if' variant exclusively after we mandate OpenStack SDK >= 0.51
            if hasattr(conn.compute, "delete_flavor_extra_specs_property"):
                conn.compute.delete_flavor_extra_specs_property(
                    sdk_res["id"], prop_name
                )
            else:
                # Old non-proxy SDK API
                conn.unset_flavor_specs(sdk_res, [prop_name])
        for prop_name in params_extra_specs.keys():
            if sdk_extra_specs.get(prop_name) != params_extra_specs[prop_name]:
                # We can use the 'if' variant exclusively after we mandate OpenStack SDK >= 0.51
                if hasattr(conn.compute, "update_flavor_extra_specs_property"):
                    conn.compute.update_flavor_extra_specs_property(
                        sdk_res, prop_name, params_extra_specs[prop_name]
                    )
                else:
                    # Old non-proxy SDK API
                    conn.set_flavor_specs(
                        sdk_res["id"], {prop_name: params_extra_specs[prop_name]}
                    )

    @staticmethod
    def _create_sdk_res(conn, sdk_params):
        if _flavor_microversion_drops_rxtx_factor(conn):
            dropped = sdk_params.pop("rxtx_factor", None)
            if dropped not in (None, RXTX_FACTOR_DEFAULT):
                LOG.warning(
                    "flavor %s: rxtx_factor %s dropped, the destination's compute API (2.102 or "
                    "later) no longer takes it",
                    sdk_params.get("name"),
                    dropped,
                )
        return conn.compute.create_flavor(**sdk_params)

    def _needs_update(self, target):
        # A flavor read at 2.102 or later has no rxtx_factor: that is not a difference.
        if target.params().get("rxtx_factor") is None:
            ours = self._data_without_info()
            theirs = target._data_without_info()
            ours[const.RES_PARAMS].pop("rxtx_factor", None)
            theirs[const.RES_PARAMS].pop("rxtx_factor", None)
            return ours != theirs
        return super()._needs_update(target)

    @staticmethod
    def _find_sdk_res(conn, name_or_id, filters=None):
        return conn.compute.find_flavor(name_or_id, **(filters or {}))

    @staticmethod
    def _update_sdk_res(conn, sdk_res, sdk_params):
        return sdk_res
