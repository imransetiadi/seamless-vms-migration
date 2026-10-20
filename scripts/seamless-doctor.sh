#!/usr/bin/env bash
# seamless-doctor.sh - check this machine against what the repository pins, before `make seamless-check` or the
# stack targets find it out (SDD §17.1, `make seamless-doctor`). Read-only: it installs nothing and names the fix of
# each tool missing or off its pin, for the host it runs on (apt and release binaries on Linux, Homebrew on macOS).
# Each pin is read from the file that sets it: Python from seamless/pyproject.toml, Node.js from
# dashboard/package.json, gitleaks, ruff, actionlint and shellcheck from .pre-commit-config.yaml (the version the
# hooks, CI and the venv share). The engine and Compose are checked by the stack targets' own check
# (`make seamless-check-context`), and curl, which their health wait needs. What `make seamless-check` works without
# - SEAMLESS_TEST_PG_URL, Playwright's Chromium, agentmemory (from the host and, on a Linux machine's own engine, from
# a container), Jev's key, and gitleaks, actionlint and shellcheck, which it skips when absent and runs at the
# machine's own version - is noted, never failed, with its fix. No secret's value or URL is printed or put on a
# command line, and nothing is installed or pulled. Exit 1 when something `make seamless-check`, the stack targets or
# the pre-commit hook need is missing or off its pin, else 0 (SDD §17.1). Bash 3.2 compatible.
set -uo pipefail

repo=$(cd "$(dirname "$0")/.." && pwd)
os=$(uname -s)
failed=0

ok() { printf '  ok    %s\n' "$1"; }
note() { printf '  --    %s\n' "$1"; }
fail() {
  printf '%s\n' "$1" | awk 'NR == 1 { print "  FAIL  " $0; next } { print "        " $0 }'
  [ -n "${2:-}" ] && printf '        fix: %s\n' "$2"
  failed=1
}
# the fix on this host: $1 on Linux, $2 on macOS
on_host() { if [ "$os" = Darwin ]; then printf '%s' "$2"; else printf '%s' "$1"; fi; }
# the first dotted version number of standard input
first_version() { grep -oE '[0-9]+(\.[0-9]+)+' | head -n 1; }
# exit 0 when dotted version $1 is at least $2
version_ge() {
  awk -v a="$1" -v b="$2" 'BEGIN {
    n = split(a, x, "."); m = split(b, y, ".")
    for (i = 1; i <= (n > m ? n : m); i++) if (x[i] + 0 != y[i] + 0) exit !(x[i] + 0 > y[i] + 0)
    exit 0 }'
}
# the rev of the pre-commit hook repository whose URL ends with /$1, without its leading v
hook_rev() {
  awk -v repo="/$1" '
    /^[[:space:]]*-[[:space:]]*repo:/ { inside = (substr($NF, length($NF) - length(repo) + 1) == repo); next }
    inside && /^[[:space:]]*rev:/ { sub(/^[[:space:]]*rev:[[:space:]]*v?/, ""); sub(/[[:space:]]*#.*$/, ""); print; exit }
  ' "$repo/.pre-commit-config.yaml"
}
# a scanner the hooks pin exactly: name, hook repository, version option, Linux fix, macOS fix. make seamless-check
# skips it when absent and runs the machine's own version, so either is noted, never failed (SDD §17.1)
pinned() {
  local pin version
  pin=$(hook_rev "$2")
  if ! command -v "$1" >/dev/null 2>&1; then
    note "$1: not found (.pre-commit-config.yaml pins $pin; make seamless-check skips it) - fix: $(on_host "$4" "$5")"
    return
  fi
  version=$("$1" "$3" 2>/dev/null | first_version)
  if [ "$version" = "$pin" ]; then
    ok "$1 $version"
  else
    note "$1 ${version:-of unknown version}: .pre-commit-config.yaml pins $pin (make seamless-check runs this one) - fix: $(on_host "$4" "$5")"
  fi
}

echo "What make seamless-check and the pre-commit hooks need:"

py_min=$(sed -n 's/^requires-python *= *">= *\([0-9.]*\)".*/\1/p' "$repo/seamless/pyproject.toml")
venv=$repo/seamless/.venv/bin
venv_fix="cd seamless && python3 -m venv .venv && .venv/bin/pip install -e '.[dev,jev,collection]'"
if [ ! -x "$venv/python" ]; then
  fail "seamless/.venv: not found (Python $py_min+, seamless/pyproject.toml)" "$venv_fix"
else
  py=$("$venv/python" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null)
  if ! version_ge "${py:-0}" "$py_min"; then
    fail "seamless/.venv: Python ${py:-unknown}, seamless/pyproject.toml requires $py_min+" \
      "rm -rf seamless/.venv, then with Python $py_min+: $venv_fix"
  elif ! "$venv/python" -c 'import seamless_migrate, pytest, ansible, yaml, openstack' >/dev/null 2>&1; then
    fail "seamless/.venv: Python $py without the dev or collection extra" \
      "seamless/.venv/bin/pip install -e './seamless[dev,jev,collection]'"
  else
    ok "seamless/.venv: Python $py with the dev and collection extras"
  fi
  ruff_pin=$(hook_rev astral-sh/ruff-pre-commit)
  ruff=$("$venv/ruff" --version 2>/dev/null | first_version)
  if [ "$ruff" = "$ruff_pin" ]; then
    ok "seamless/.venv: ruff $ruff"
  else
    fail "seamless/.venv: ruff ${ruff:-not found}, .pre-commit-config.yaml pins $ruff_pin (one formatter style)" \
      "seamless/.venv/bin/pip install 'ruff==$ruff_pin'"
  fi
fi

node_min=$(sed -n 's/.*"node": *">= *\([0-9.]*\)".*/\1/p' "$repo/dashboard/package.json")
node_fix=$(on_host "Node.js $node_min from NodeSource (https://github.com/nodesource/distributions), npm included" \
  "brew install node@$node_min")
if ! command -v node >/dev/null 2>&1; then
  fail "node: not found (dashboard/package.json needs $node_min+)" "$node_fix"
else
  node=$(node --version 2>/dev/null | first_version)
  if version_ge "${node:-0}" "$node_min"; then
    ok "node $node"
  else
    fail "node ${node:-of unknown version}: dashboard/package.json needs $node_min+" "$node_fix"
  fi
fi
if command -v npm >/dev/null 2>&1; then
  ok "npm $(npm --version 2>/dev/null | first_version)"
else
  fail "npm: not found" "$node_fix"
fi
modules=$repo/dashboard/node_modules/.package-lock.json
if [ ! -f "$modules" ]; then
  fail "dashboard/node_modules: not installed" "cd dashboard && npm ci"
elif [ "$repo/dashboard/package-lock.json" -nt "$modules" ]; then
  fail "dashboard/node_modules: older than package-lock.json" "cd dashboard && npm ci"
else
  ok "dashboard/node_modules"
fi

gitleaks_pin=$(hook_rev gitleaks/gitleaks)
pinned gitleaks gitleaks/gitleaks --version \
  "the v$gitleaks_pin release binary: https://github.com/gitleaks/gitleaks/releases/tag/v$gitleaks_pin" \
  "brew install gitleaks (check that it is $gitleaks_pin)"
actionlint_pin=$(hook_rev rhysd/actionlint)
pinned actionlint rhysd/actionlint --version \
  "the v$actionlint_pin release binary: https://github.com/rhysd/actionlint/releases/tag/v$actionlint_pin" \
  "brew install actionlint (check that it is $actionlint_pin)"

# make seamless-check runs this machine's shellcheck, the hook shellcheck-py's own: another version is named only.
# the hook's rev is shellcheck's version and a fourth number of shellcheck-py's packaging (v0.10.0.1: 0.10.0)
shellcheck_pin=$(hook_rev shellcheck-py/shellcheck-py | grep -oE '^[0-9]+\.[0-9]+\.[0-9]+')
if ! command -v shellcheck >/dev/null 2>&1; then
  note "shellcheck: not found (make seamless-check skips it) - fix: $(on_host 'sudo apt-get install -y shellcheck' 'brew install shellcheck')"
else
  shellcheck=$(shellcheck --version 2>/dev/null | first_version)
  if [ "$shellcheck" = "$shellcheck_pin" ]; then
    ok "shellcheck $shellcheck"
  else
    ok "shellcheck $shellcheck (the pre-commit hook runs $shellcheck_pin; its findings may differ)"
  fi
fi

hook=$(git -C "$repo" rev-parse --git-path hooks/pre-commit 2>/dev/null)
case $hook in /*) ;; *) hook=$repo/$hook ;; esac
if ! command -v pre-commit >/dev/null 2>&1; then
  fail "pre-commit: not found" "pipx install pre-commit, then pre-commit install"
elif ! grep -qs pre-commit "$hook"; then
  fail "pre-commit: the hook is not installed in this checkout" "pre-commit install"
else
  ok "pre-commit $(pre-commit --version 2>/dev/null | first_version), hook installed"
fi

echo "What the stack targets need (make seamless-up, seamless-demo):"
engine_ok=0
if engine=$(make -s -C "$repo" seamless-check-context 2>&1); then
  engine_ok=1
  ok "container engine and Compose (make seamless-check-context)"
else
  # the stack targets' own words, without make's line about the target that failed
  engine=$(printf '%s\n' "$engine" | grep -v '^make\(\[[0-9]*\]\)\{0,1\}: \*\*\*')
  fail "${engine:-make seamless-check-context failed}"
fi
if command -v curl >/dev/null 2>&1; then
  ok "curl (the stack targets wait for /api/v1/health with it)"
else
  fail "curl: not found (the stack targets wait for /api/v1/health with it)" \
    "$(on_host 'sudo apt-get install -y curl' 'brew install curl')"
fi

echo "Optional (make seamless-check works without them):"
if [ -n "${SEAMLESS_TEST_PG_URL:-}" ]; then
  ok "SEAMLESS_TEST_PG_URL is set: the PostgreSQL store tests run"
else
  note "SEAMLESS_TEST_PG_URL is not set: the PostgreSQL store tests are skipped (CLAUDE.md §6)"
fi

browsers=${PLAYWRIGHT_BROWSERS_PATH:-$(on_host "$HOME/.cache/ms-playwright" "$HOME/Library/Caches/ms-playwright")}
chromium=0
for dir in "$browsers"/chromium*; do [ -d "$dir" ] && chromium=1; done
if [ "$chromium" = 1 ]; then
  ok "Playwright's Chromium: npm run test:e2e and the browser journeys can run"
else
  note "Playwright's Chromium is not installed: cd dashboard && npx playwright install chromium$(on_host ' --with-deps' '')"
fi

# the URL is never printed nor put on curl's command line, which ps shows every local user: it may carry a user and
# password, so curl reads it from its configuration on standard input (CLAUDE.md §4)
if printf 'url = "%s"\n' "${AGENTMEMORY_URL:-http://localhost:3111}/agentmemory/livez" |
  curl -fsS -m 3 -K - -o /dev/null >/dev/null 2>&1; then
  ok "agentmemory answers on this machine"
  # a Linux machine's own engine reaches the host through the bridge, never its 127.0.0.1
  if [ "$os" = Linux ] && [ "$engine_ok" = 1 ] && [ "${SEAMLESS_ENGINE:-docker}" = docker ] &&
    [ "${SEAMLESS_DOCKER_CONTEXT:-default}" = default ]; then
    # a cached busybox only: the doctor pulls nothing, and docker exits 125 when there is none
    docker run --pull=never --rm --add-host host.docker.internal:host-gateway busybox \
      wget -qO- -T 3 http://host.docker.internal:3111/agentmemory/livez >/dev/null 2>&1
    case $? in
      0) ok "agentmemory answers from a container (host.docker.internal:3111)" ;;
      125) note "agentmemory from a container: skipped - no busybox image here to look with, and the doctor pulls nothing (docker pull busybox to look)" ;;
      *) note "agentmemory does not answer from a container: the control plane runs without memory until it listens on the bridge address (docs/MEMORY.md §4.2)" ;;
    esac
  fi
else
  note "agentmemory does not answer: memory is off for agents and the stack (docs/MEMORY.md §4.1)"
fi

if [ -n "${TYPESAFE_API_KEY:-}" ]; then
  ok "TYPESAFE_API_KEY is set: Jev (.mcp.json, compose-init.sh's ai profile)"
else
  note "TYPESAFE_API_KEY is not set: Jev is off - read -rs TYPESAFE_API_KEY && export TYPESAFE_API_KEY (CLAUDE.md §4)"
fi

if [ "$failed" = 1 ]; then
  echo "Fix the FAIL lines, then run make seamless-doctor again."
  exit 1
fi
echo "Nothing make seamless-check or the stack targets need is missing."
