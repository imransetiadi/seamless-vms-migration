#!/usr/bin/env bash
# Modified by the Seamless Migrate project, 2026.

set -euxo pipefail

# Check if running inside a container (Podman or Docker compatible)
# Podman sets 'container=podman' in /run/.containerenv
if [ ! -f "/run/.containerenv" ]; then
    echo "This script is only meant to be run in a container"
    exit 1
fi

if [ -z "${VIRTUAL_ENV:-}" ]; then
    # shellcheck disable=SC1091
    source "${VENV_DIR:-.venv}/bin/activate"
fi

export GITCHANGELOG_CONFIG_FILENAME=./scripts/gitchangelog.rc
gitchangelog > ./docs/src/changelog.rst

cd ./docs/src
make html
