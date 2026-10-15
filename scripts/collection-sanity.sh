#!/usr/bin/env bash
# ansible-test sanity over the os-migrate collection, as upstream runs it (QASuite §14.2).
#
# ansible-test refuses a collection reached through a symlink ("must be within the source tree"), the
# layout CI and `make seamless-check` use for the unit tests, so the collection's tracked files are
# copied into a real ansible_collections/os_migrate/os_migrate tree first: plugins, roles, playbooks,
# meta, tests/sanity (its ignore files), tests/unit and galaxy.yml. Never a file git does not track:
# a checkout's local secrets stay where they are (CLAUDE.md §7).
#
# usage: scripts/collection-sanity.sh [ansible-test sanity options]
#   the ansible-test and python3 first on PATH run it; PYTHON_VERSION overrides --python
#   (default: the version of that python3)
set -euo pipefail

repo=$(cd "$(dirname "$0")/.." && pwd)
work=$(mktemp -d "${TMPDIR:-/tmp}/collection-sanity.XXXXXX")
trap 'rm -rf "$work"' EXIT
tree="$work/ansible_collections/os_migrate/os_migrate"
mkdir -p "$tree"
git -C "$repo" ls-files -z -- plugins roles playbooks meta tests/sanity tests/unit galaxy.yml \
  | rsync -a --from0 --files-from=- "$repo/" "$tree/"

python=${PYTHON_VERSION:-$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')}
cd "$tree"
ansible-test sanity --python "$python" "$@"
