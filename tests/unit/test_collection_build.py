"""What `ansible-galaxy collection build` packs - `make build`, the Makefile's default target, runs it at
the repository root, and ansible-galaxy packs files from disk, not from git. It never packs a
git-ignored local secret file a checkout may hold, nor the control plane, the dashboard or the
deployment around the collection, which ship as container images. The probe runs ansible-core's
own build file walk, with this repository's galaxy.yml, on a synthetic tree of dummy files: of the
checkout, only its .gitignore files and the names of its tracked files are read (CLAUDE.md §7,
Security.md R-26)."""

from __future__ import absolute_import, division, print_function

__metaclass__ = type

import os
import posixpath
import re
import subprocess

import pytest
import yaml
from ansible.galaxy import collection as galaxy_collection

from ansible_collections.os_migrate.os_migrate.tests.unit.test_role_modules_resolve import _vendored

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
    "tests/clouds.yml",  # make generate-auth-files input
    "tests/auth_tenant.yml",  # make generate-auth-files output: cloud credentials
    "tests/func/auth_admin.yml",
    "clouds.yml",
    "secrets/provider.yml",
    "local.env",
    ".env.local",
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
    "RELEASING.md",  # how the product is released on GitHub, not the collection
    ".worktrees/track-b/seamless/data/secrets/provider-1.json",  # a track's checkout (CLAUDE.md §3)
    ".worktrees/track-b/tests/auth_tenant.yml",
    ".cache/seamless-check/report.txt",  # make seamless-check scratch
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
# the .gitignore files of the checkout (test_the_gitignore_list_names_every_gitignore_file_of_the_checkout)
GITIGNORES = [".gitignore", "docs/src/.gitignore"]
# a directory the artifact packs, for the patterns that match at any depth
DEEPER = "roles/example/files"
# the collection's own files: the artifact packs every tracked file under these
# docs/src is the collection's documentation; the product docs beside it are packed too, but the audit
# material and agent plans under docs/audits, docs/advisor and docs/superpowers are not (audit D-21)
OWN = ["plugins", "roles", "playbooks", "meta", "docs/src", "tests", "README.md", "LICENSE", "CHANGELOG.rst"]


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


def _git(*args):
    try:
        out = subprocess.run(
            ["git", "-C", ROOT] + list(args),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as error:
        pytest.skip("needs a git checkout: %s" % error)
    return [name for name in out.split("\0") if name]


def _spellings(text):
    """Every spelling of text with each [...] character class replaced by one of its members."""
    match = re.search(r"\[([^\]]*)\]", text)
    if match is None:
        return [text]
    members = match.group(1)
    assert members and not set(members) & set("!^-"), "teach _spellings this class: " + text
    return [
        spelled
        for member in members
        for spelled in _spellings(text[: match.start()] + member + text[match.end():])
    ]


def _gitignored_samples(gitignore, lines):
    """One path, relative to the collection root, per place a pattern of the .gitignore file at
    gitignore matches; a directory pattern gives a file inside that directory."""
    base = posixpath.dirname(gitignore)
    samples = []
    for line in lines:
        pattern = line.strip()
        if not pattern or pattern.startswith("#"):
            continue
        assert not pattern.startswith("!"), "teach _gitignored_samples negation: " + pattern
        body = pattern.strip("/")
        anchored = pattern.startswith("/") or "/" in body
        places = [base] if anchored else [base, posixpath.join(base, DEEPER)]
        forms = [body.replace("**/", ""), body.replace("**/", "sub/")] if "**/" in body else [body]
        for form in forms:
            for spelled in _spellings(form):
                name = spelled.replace("**", "sample").replace("*", "sample").replace("?", "x")
                for place in places:
                    path = posixpath.join(place, name)
                    samples.append(path + "/dummy" if pattern.endswith("/") else path)
    return sorted(set(samples))


def test_the_collection_build_packs_no_local_secret_and_nothing_around_the_collection(tmp_path):
    packed = _packed(tmp_path, SECRETS + AROUND + COLLECTION)

    assert sorted(packed & set(SECRETS)) == []
    assert sorted(packed & set(AROUND)) == []
    assert set(COLLECTION) <= packed


def test_the_collection_build_packs_nothing_a_gitignore_file_names(tmp_path):
    """Review RV6-02: build_ignore mirrors every pattern of the checkout's .gitignore files, so no
    file git ignores - the cloud credentials `make generate-auth-files` writes to
    tests/auth_tenant.yml, a secrets/ directory, a track's checkout under .worktrees/ - reaches the
    artifact, and a pattern a .gitignore file gains later is held to the same rule here."""
    samples = []
    for gitignore in GITIGNORES:
        with open(os.path.join(ROOT, gitignore), encoding="utf-8") as f:
            samples += _gitignored_samples(gitignore, f.read().splitlines())

    packed = _packed(tmp_path, samples + COLLECTION)

    assert sorted(packed & set(samples)) == []
    assert set(COLLECTION) <= packed


def test_the_gitignore_list_names_every_gitignore_file_of_the_checkout():
    assert sorted(_git("ls-files", "-z", "--", ".gitignore", "*/.gitignore")) == sorted(GITIGNORES)


def test_the_collection_build_packs_every_tracked_file_of_the_collection(tmp_path):
    """build_ignore leaves out none of the collection's own files: every tracked file under its
    directories, and its top-level documents, is packed."""
    tracked = _git("ls-files", "-z", "--", *OWN)

    assert sorted(set(tracked) - _packed(tmp_path, tracked)) == []


def test_the_collection_build_packs_the_vendored_openstack_cloud_modules(tmp_path):
    """build_ignore leaves out none of the openstack.cloud modules the roles call: neither the
    untracked plugins/modules/<module>.py and plugins/module_utils/<util>.py, links after `make
    vendor-links` and copies in the image (scripts/vendor-openstack-cloud.sh), nor the files of
    the openstack.cloud checkout under plugins/modules/_vendor the links point to."""
    upstream = "plugins/modules/_vendor/openstack.cloud/plugins"
    modules, utils = sorted(_vendored("VENDORED_MODULES")), sorted(_vendored("VENDORED_MODULE_UTILS"))
    assert modules and utils
    vendored = []
    for module in modules:
        vendored += ["plugins/modules/%s.py" % module, "%s/modules/%s.py" % (upstream, module)]
    for util in utils:
        vendored += ["plugins/module_utils/%s.py" % util, "%s/module_utils/%s.py" % (upstream, util)]

    assert sorted(set(vendored) - _packed(tmp_path, vendored)) == []
