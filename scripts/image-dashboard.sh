#!/usr/bin/env bash
# The dashboard the control-plane image serves (SDD §17.1); seamless/Containerfile runs it with the build
# context bind-mounted read-only:
#
#   image-dashboard.sh <build context> <built dashboard, relative to the context> <target directory>
#
# A built dashboard in the context (CI's artifact, `make dashboard-build`) is copied as it is. Without one, the
# dashboard is built from its sources in the context - `npm ci` and `npm run build` with the image's own Node -
# in a scratch copy that takes its node_modules and npm cache with it, and only its dist is kept: a Linux host
# needs no Node.js. No build and no sources is a broken context, never an image without its UI.
set -euo pipefail

[ $# -eq 3 ] || { echo "usage: $0 <build context> <built dashboard> <target directory>" >&2; exit 2; }
ctx=$1
dist=$2
target=$3
mkdir -p "$target"

if [ -f "$ctx/$dist/index.html" ]; then
  cp -R "$ctx/$dist/." "$target/"
  echo "dashboard: copied the built one at $dist"
  exit 0
fi

if [ ! -f "$ctx/dashboard/package-lock.json" ]; then
  echo "dashboard: neither a built dashboard at $dist nor its sources (dashboard/) in the build context" >&2
  exit 1
fi

work=$(mktemp -d "${TMPDIR:-/tmp}/image-dashboard.XXXXXX")
trap 'rm -rf "$work"' EXIT
cp -R "$ctx/dashboard/." "$work/"
cd "$work"
# the npm cache stays in the scratch copy; the e2e devDependency downloads no browser
export npm_config_cache="$work/.npm-cache" PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
npm ci --no-audit --no-fund
npm run build
cp -R "$work/dist/." "$target/"
echo "dashboard: built from its sources"
