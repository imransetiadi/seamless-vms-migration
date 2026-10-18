"""One product release, one version (RELEASING.md §1): every place a release bump changes names
it."""

import json
import os
import re
import subprocess
import tomllib

import pytest
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
        # the release the control-plane image make deploy-runtime-check runs defaults to
        # (docs-drift DD2-06, review RV37B-04)
        "Makefile": _one(r"^SEAMLESS_RELEASE := (\S+)$", "Makefile"),
    }


def test_every_version_location_of_a_release_names_one_version():
    """RELEASING.md §1 (docs-drift DD2-06): a release bump changes every location of its table -
    one left behind, as the Makefile's image tag was, makes make deploy-runtime-check skip or
    check a stale image."""
    versions = _release_versions()
    assert set(versions.values()) == {__version__}, versions


def _resolved(
    tmp_path,
    env_file: str | None,
    environment: dict[str, str] | None = None,
    names: tuple[str, ...] = ("SEAMLESS_VERSION", "SEAMLESS_CP_IMAGE"),
) -> str:
    """``names`` (SEAMLESS_VERSION and SEAMLESS_CP_IMAGE) as the Makefile resolves them, with
    ``env_file`` as deploy/compose/.env (none when ``None``) and ``environment`` exported."""
    wrapper = tmp_path / "print.mk"
    # $(info ...) prints the values as Make holds them - quotes included, which a shell echo drops
    shown = " ".join(f"$({name})" for name in names)
    wrapper.write_text(f"include Makefile\nzz-print:\n\t@true$(info {shown})\n")
    dotenv = tmp_path / "compose.env"
    dotenv.unlink(missing_ok=True)
    if env_file is not None:
        dotenv.write_text(env_file, encoding="utf-8")
    env = {
        k: v for k, v in os.environ.items() if k not in ("SEAMLESS_VERSION", "SEAMLESS_CP_IMAGE")
    }
    env.update(environment or {})
    done = subprocess.run(
        ["make", "-s", "--no-print-directory", "-f", str(wrapper), "zz-print"]
        + [f"SEAMLESS_ENV_FILE={dotenv}"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


@pytest.mark.parametrize(
    "env_file, environment, version",
    [
        (None, None, __version__),
        ("COMPOSE_PROFILES=ai\nSEAMLESS_VERSION=0.2.0\n", None, "0.2.0"),
        ('SEAMLESS_VERSION="0.2.1"\n', None, "0.2.1"),
        ("SEAMLESS_VERSION='0.2.2'\n", None, "0.2.2"),
        ("SEAMLESS_VERSION=0.2.0\n", {"SEAMLESS_VERSION": "0.3.0"}, "0.3.0"),
        ("SEAMLESS_VERSION=0.2.0\n", {"SEAMLESS_VERSION": ""}, __version__),
        ("SEAMLESS_VERSION=\n", None, __version__),
        ("SEAMLESS_VERSION=0.2.0 # pinned\n", None, "0.2.0"),
        ("export SEAMLESS_VERSION=0.2.0\n", None, "0.2.0"),
        ("SEAMLESS_VERSION = 0.2.0\n", None, "0.2.0"),
        ('SEAMLESS_VERSION="0.2.0" # pinned\n', None, "0.2.0"),
        ("SEAMLESS_VERSION: 0.2.0\n", None, "0.2.0"),
        ("\ufeffSEAMLESS_VERSION=0.2.0\n", None, "0.2.0"),
    ],
    ids=[
        "the release",
        "pinned in .env",
        "quoted in .env",
        "single-quoted",
        "the shell over .env",
        "an empty shell value: Compose's default",
        "an empty .env value: Compose's default",
        "an inline comment",
        "an export prefix",
        "spaces around =",
        "quoted with a comment",
        "a YAML-style colon",
        "a byte-order mark",
    ],
)
def test_the_runtime_check_inspects_the_image_compose_runs(
    tmp_path, env_file, environment, version
):
    """RELEASING.md §1, §9 (review RV37B-04): make deploy-runtime-check inspects the image the
    Compose stack runs - SEAMLESS_VERSION read as Compose reads it: the shell or the command line,
    else deploy/compose/.env, else the release; an empty value is ${SEAMLESS_VERSION:-...}'s
    default, the release, never an image without a tag (review 38b)."""
    assert _resolved(tmp_path, env_file, environment) == f"{version} seamless-migrate:{version}"


@pytest.mark.parametrize(
    "env_file, environment, port",
    [
        (None, None, "8080"),
        ("SEAMLESS_HOST_PORT=18081\n", None, "18081"),
        ("export SEAMLESS_HOST_PORT = 18081 # moved\n", None, "18081"),
        ("SEAMLESS_HOST_PORT=18081\n", {"SEAMLESS_HOST_PORT": "18082"}, "18082"),
        ("SEAMLESS_HOST_PORT=18081\n", {"SEAMLESS_HOST_PORT": ""}, "8080"),
        ("SEAMLESS_HOST_PORT: 18081\n", None, "18081"),
        ("\ufeffSEAMLESS_HOST_PORT=18081\n", None, "18081"),
    ],
    ids=[
        "the default",
        "moved in .env",
        "as Compose reads it",
        "the shell over .env",
        "empty",
        "a YAML-style colon",
        "a byte-order mark",
    ],
)
def test_the_make_targets_wait_on_the_port_compose_publishes(tmp_path, env_file, environment, port):
    """SDD §17.2 (reviews RV38B-07b, RV41A-01): make seamless-start and seamless-restart poll the
    port Compose publishes - the shell's, else .env's read by compose-go's rules, else 8080 -
    never the shell's alone."""
    assert _resolved(tmp_path, env_file, environment, ("SEAMLESS_HOST_PORT",)) == port


@pytest.mark.parametrize(
    ("target", "env_file", "key"),
    [
        ("seamless-up", "PORT=18081\nSEAMLESS_HOST_PORT=${PORT}\n", "SEAMLESS_HOST_PORT"),
        ("seamless-start", 'SEAMLESS_HOST_PORT="${PORT:-18081}"\n', "SEAMLESS_HOST_PORT"),
        ("seamless-restart", "SEAMLESS_HOST_PORT=$PORT\n", "SEAMLESS_HOST_PORT"),
        (
            "deploy-runtime-check",
            "RELEASE=0.2.0\nSEAMLESS_VERSION=${RELEASE}\n",
            "SEAMLESS_VERSION",
        ),
    ],
)
def test_a_value_compose_would_interpolate_stops_the_targets_that_use_it(
    tmp_path, target, env_file, key
):
    """SDD §17.2 (review RV41A-02): Compose interpolates a '$' in an unquoted or double-quoted
    .env value; the Make targets read plain values, so a target that uses such a value stops
    before its recipe runs, naming it - the health wait polled port 80 for 300 s, and
    deploy-runtime-check inspected "seamless-migrate:" and skipped the control plane. A target that
    does not use it still runs."""
    dotenv = tmp_path / "compose.env"
    dotenv.write_text(env_file, encoding="utf-8")
    env = {
        k: v for k, v in os.environ.items() if k not in ("SEAMLESS_VERSION", "SEAMLESS_HOST_PORT")
    }

    def dry(name):
        return subprocess.run(
            ["make", "-n", "--no-print-directory", name, f"SEAMLESS_ENV_FILE={dotenv}"]
            + ["SEAMLESS_HOST_OS=Linux", "SEAMLESS_DOCKER_CONTEXT=default"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
        )

    stopped = dry(target)
    assert stopped.returncode != 0, stopped.stdout
    assert f"sets {key} through a variable" in stopped.stderr, stopped.stderr
    assert dry("seamless-stop").returncode == 0
