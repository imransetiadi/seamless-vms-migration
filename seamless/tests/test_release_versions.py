"""One product release, one version (RELEASING.md §1): every place a release bump changes names
it."""

import json
import re
import tomllib

import yaml

from seamless_migrate import __version__
from seamless_migrate.config import find_repo_root

ROOT = find_repo_root()


def _one(pattern: str, path: str) -> str:
    """The single version ``pattern`` captures in ``path``."""
    found = re.findall(pattern, (ROOT / path).read_text(encoding="utf-8"), re.MULTILINE)
    assert len(found) == 1, (path, pattern, found)
    return found[0]


def _release_versions() -> dict[str, str]:
    """Every version location of RELEASING.md §1's table, by where it lives."""
    package = json.loads((ROOT / "dashboard/package.json").read_text(encoding="utf-8"))
    lock = json.loads((ROOT / "dashboard/package-lock.json").read_text(encoding="utf-8"))
    openshift = yaml.safe_load((ROOT / "deploy/openshift/kustomization.yaml").read_text())
    kubernetes = yaml.safe_load((ROOT / "deploy/kubernetes/kustomization.yaml").read_text())
    pyproject = tomllib.loads((ROOT / "seamless/pyproject.toml").read_text(encoding="utf-8"))
    return {
        "seamless/pyproject.toml": pyproject["project"]["version"],
        "seamless_migrate.__version__": __version__,
        "dashboard/package.json": package["version"],
        "dashboard/package-lock.json": lock["version"],
        "dashboard/package-lock.json packages['']": lock["packages"][""]["version"],
        "deploy/compose/compose.yaml": _one(
            r"image: seamless-migrate:\$\{SEAMLESS_VERSION:-([^}]+)\}",
            "deploy/compose/compose.yaml",
        ),
        "deploy/compose/README.md": _one(
            r"^\| `SEAMLESS_HOST_PORT`, `SEAMLESS_VERSION` \| `8080`, `([^`]+)` \|",
            "deploy/compose/README.md",
        ),
        "deploy/openshift newTag": next(
            i["newTag"] for i in openshift["images"] if i["name"].endswith("seamless-migrate")
        ),
        "deploy/openshift version label": openshift["labels"][0]["pairs"][
            "app.kubernetes.io/version"
        ],
        "deploy/kubernetes newTag": next(
            i["newTag"] for i in kubernetes["images"] if i["name"].endswith("seamless-migrate")
        ),
        "seamless/Containerfile": _one(
            r'org\.opencontainers\.image\.version="([^"]+)"', "seamless/Containerfile"
        ),
        # the control-plane image make deploy-runtime-check runs (docs-drift DD2-06)
        "Makefile": _one(r"^SEAMLESS_VERSION \?= (\S+)$", "Makefile"),
    }


def test_every_version_location_of_a_release_names_one_version():
    """RELEASING.md §1 (docs-drift DD2-06): a release bump changes every location of its table -
    one left behind, as the Makefile's image tag was, makes make deploy-runtime-check skip or
    check a stale image."""
    versions = _release_versions()
    assert set(versions.values()) == {__version__}, versions
