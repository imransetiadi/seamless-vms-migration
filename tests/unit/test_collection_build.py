"""What `ansible-galaxy collection build` packs - `make build`, the Makefile's default target, runs it at
the repository root, and ansible-galaxy packs files from disk, not from git. It never packs a
git-ignored local secret file a checkout may hold, nor the control plane, the dashboard or the
deployment around the collection, which ship as container images. The probe runs ansible-core's
own build file walk, with this repository's galaxy.yml, on a synthetic tree of dummy files: no
real file of the checkout is read (CLAUDE.md §7, Security.md R-26)."""

from __future__ import absolute_import, division, print_function

__metaclass__ = type

import os

import yaml
from ansible.galaxy import collection as galaxy_collection

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

# git-ignored local secret files a checkout may hold (CLAUDE.md §7), at the top and further down
SECRETS = [
    ".env",
    "deploy/compose/.env",
    "tokens.yaml",
    "deploy/compose/tokens.yaml",
    "clouds.yaml",
    "roles/import_workloads/files/clouds.yaml",
    "secure.yaml",
    "playbooks/secure.yaml",
    "conv.key",
    "roles/conversion_host/files/conv.key",
    "server.pem",
    "plugins/module_utils/ca.pem",
]
# the product around the collection, its build output and the agent configuration
AROUND = [
    "seamless/src/seamless_migrate/__init__.py",
    "seamless/.venv/bin/python",
    "dashboard/node_modules/left-pad/index.js",
    "dashboard/dist/index.html",
    "deploy/openshift/kustomization.yaml",
    ".claude/settings.json",
    ".mcp.json",
    "CLAUDE.md",
]
COLLECTION = [
    "plugins/modules/example.py",
    "plugins/module_utils/example.py",
    "roles/example/tasks/main.yml",
    "playbooks/example.yml",
    "meta/runtime.yml",
    "README.md",
    "LICENSE",
    "CHANGELOG.rst",
]


def _packed(tmp_path, files):
    with open(os.path.join(ROOT, "galaxy.yml"), encoding="utf-8") as f:
        galaxy = yaml.safe_load(f)
    for name in files:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("dummy\n")
    manifest = galaxy_collection._build_files_manifest_walk(
        str(tmp_path).encode(), galaxy["namespace"], galaxy["name"], galaxy.get("build_ignore", [])
    )
    return {entry["name"] for entry in manifest["files"] if entry["ftype"] == "file"}


def test_the_collection_build_packs_no_local_secret_and_nothing_around_the_collection(tmp_path):
    packed = _packed(tmp_path, SECRETS + AROUND + COLLECTION)

    assert sorted(packed & set(SECRETS)) == []
    assert sorted(packed & set(AROUND)) == []
    assert set(COLLECTION) <= packed
