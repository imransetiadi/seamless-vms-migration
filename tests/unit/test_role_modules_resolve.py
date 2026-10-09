"""Every ``os_migrate.os_migrate.<name>`` a role or playbook calls must exist: as a module in
``plugins/modules``, as a role, a filter, or as one of the openstack.cloud modules the image
vendors (``scripts/vendor-openstack-cloud.sh``, the same list as the Makefile). The warm
playbook tests stub ``server_action``/``server_info`` and would not notice a missing one."""

from __future__ import absolute_import, division, print_function

__metaclass__ = type

import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))


def _vendored(kind):
    script = open(os.path.join(ROOT, "scripts", "vendor-openstack-cloud.sh")).read()
    match = re.search(kind + r'="([^"]+)"', script)
    assert match, kind
    return set(match.group(1).split())


def _makefile_list(name):
    text = open(os.path.join(ROOT, "Makefile")).read()
    match = re.search(r"^" + name + r"\s*:=\s*((?:[^\n\\]|\\\n)+)", text, re.M)
    assert match, name
    return set(match.group(1).replace("\\\n", " ").split())


def _referenced():
    out = set()
    for base in ("roles", "playbooks"):
        for dirpath, _, files in os.walk(os.path.join(ROOT, base)):
            for name in files:
                if name.endswith((".yml", ".yaml")):
                    text = open(os.path.join(dirpath, name), errors="replace").read()
                    out.update(re.findall(r"os_migrate\.os_migrate\.([a-z_]+)", text))
    return out


def test_vendored_lists_match_the_makefile():
    assert _vendored("VENDORED_MODULES") == _makefile_list("VENDORED_MODULES")
    assert _vendored("VENDORED_MODULE_UTILS") == _makefile_list("VENDORED_MODULE_UTILS")


def test_every_module_the_roles_call_exists_or_is_vendored():
    modules = {
        f[:-3] for f in os.listdir(os.path.join(ROOT, "plugins", "modules")) if f.endswith(".py")
    }
    roles = set(os.listdir(os.path.join(ROOT, "roles")))
    filters = set()
    for f in os.listdir(os.path.join(ROOT, "plugins", "filter")):
        if f.endswith(".py"):
            filters.update(re.findall(r"[\"']([a-z_]+)[\"']\s*:", open(os.path.join(ROOT, "plugins", "filter", f)).read()))
    known = modules | roles | filters | _vendored("VENDORED_MODULES")
    missing = sorted(name for name in _referenced() if name not in known)
    assert missing == [], "referenced by roles/playbooks but neither in the tree nor vendored: %s" % missing
