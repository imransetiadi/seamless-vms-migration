"""os_conversion_host_info never prints a conversion host's user data: Nova details it to an admin,
and a server the role created holds its injected host key there for its whole life (Seamless Migrate
SDD 6.5, 26.3)."""

from __future__ import absolute_import, division, print_function

__metaclass__ = type

import base64
import json

from ansible_collections.os_migrate.os_migrate.plugins.modules import (
    os_conversion_host_info as info_module,
)
from ansible_collections.os_migrate.os_migrate.tests.unit import fake_openstack as fake

#: a stand-in for the injected key's text in the user data - no key at all, never printed either way
SECRET_LINE = "ed25519_private: the-injected-private-half-stands-here"


class _Conn:
    def __init__(self, server):
        self.server = server

    def get_server(self, name_or_id=None, filters=None):
        return self.server


def test_a_conversion_host_without_an_address_fails_without_its_user_data():
    user_data = base64.b64encode(
        ("#cloud-config\nssh_deletekeys: true\nssh_keys:\n  " + SECRET_LINE + "\n").encode()
    ).decode()
    server = fake.Obj(
        id="srv-conv-1",
        name="os_migrate_conv",
        status="ACTIVE",
        access_ipv4="",
        public_v4="",
        user_data=user_data,
    )

    result = fake.run_module(
        info_module, {"server": "os_migrate_conv", "cloud": "src"}, _Conn(server)
    )

    assert result["failed"] is True
    printed = json.dumps(result)
    assert user_data not in printed
    assert SECRET_LINE not in printed
    # the message still names the host and says why
    assert "srv-conv-1" in result["msg"] and "os_migrate_conv" in result["msg"]
    assert "address" in result["msg"]
