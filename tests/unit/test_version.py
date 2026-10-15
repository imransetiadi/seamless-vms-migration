"""The version resource files carry is the collection's: plugins/module_utils/filesystem.py
refuses a resource file whose os_migrate_version is not const.OS_MIGRATE_VERSION, and a release
bumps galaxy.yml's version (docs/src/devel/releasing.rst). Nothing builds the constant from
galaxy.yml, so this test keeps the two equal."""

from __future__ import absolute_import, division, print_function

__metaclass__ = type

import os

import yaml

from ansible_collections.os_migrate.os_migrate.plugins.module_utils import const

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def test_the_resource_files_version_is_the_collections_version():
    with open(os.path.join(ROOT, "galaxy.yml"), encoding="utf-8") as f:
        assert const.OS_MIGRATE_VERSION == yaml.safe_load(f)["version"]
