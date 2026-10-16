# shellcheck shell=bash
# token-headers.sh - sourced by the e2e scripts. Each bearer token reaches curl from a header file in
# a private temporary directory (mode 0700, removed when the script exits) as `curl -H @FILE`, never
# as text on a command line, where ps shows it to every user of the host (CLAUDE.md §4, audit A-15).
# What a script writes for itself goes in that directory too (audit A-16).
#
#   . "$(dirname "$0")/token-headers.sh"
#   H=$(token_header admin "$TOKEN") || exit 2      # H="@<dir>/admin.header";  curl -H "$H" ...

# xtrace prints every expanded argument, each token among them (review RV17-02)
case $- in *x*)
  echo "token-headers.sh: xtrace (set -x) is on and would print the tokens; turn it off" >&2
  exit 2 ;;
esac
E2E_TMP=$(mktemp -d "${TMPDIR:-/tmp}/seamless-e2e.XXXXXX") || {
  echo "cannot create a private temporary directory" >&2
  exit 2
}
trap 'rm -rf "$E2E_TMP"' EXIT

# token_header NAME TOKEN: write "Authorization: Bearer TOKEN" to NAME's header file with printf, a
# shell builtin, so no process gets the token as an argument; print curl's "@FILE" argument for it
token_header() {
  case $- in *x*)
    echo "token_header: xtrace (set -x) is on and would print the token; turn it off" >&2
    return 2 ;;
  esac
  local file="$E2E_TMP/$1.header"
  (umask 077 && printf 'Authorization: Bearer %s\n' "$2" >"$file") || return 2
  printf '@%s' "$file"
}
