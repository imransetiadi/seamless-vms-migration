#!/usr/bin/env bash
#
# compose-init.sh - generate the local secrets for the Seamless Migrate Docker Compose stack.
#
# Creates, inside deploy/compose/ (both files are git-ignored):
#   .env         mode 600  random POSTGRES_PASSWORD and JEV_MCP_AUTH_TOKEN, plus the optional
#                          TYPESAFE_API_KEY and AGENTMEMORY_SECRET taken from YOUR environment
#   tokens.yaml            SHA-256 of a freshly generated admin API token (SDD 13.1):
#                          tokens: [{name: admin, role: admin, sha256: <hex>}]
#
# The plaintext admin token is printed exactly once and is never written anywhere.
# API keys are never printed. Without TYPESAFE_API_KEY the Jev sidecar is left disabled.
#
# Idempotent: existing files are kept. --force regenerates them. It keeps the existing
# POSTGRES_PASSWORD (the database volume was initialised with it) unless --rotate-db-password
# is given, keeps API keys already stored in .env when your environment does not provide new ones,
# and carries over what you changed by hand: lines of .env this script does not manage (for
# example SEAMLESS_HOST_PORT), a removed SEAMLESS_MEMORY_URL (memory stays disabled) and your
# Jev on/off choice. It does ROTATE the admin API token (the old one stops working and any other
# entry of tokens.yaml is dropped) unless --keep-token is given.
#
# Keys reach this script through the environment of your shell, never as command-line text:
#   read -rs TYPESAFE_API_KEY && export TYPESAFE_API_KEY      # in a shell that is not used by agents
#   scripts/compose-init.sh
#
# Options:
#   --force                regenerate .env and tokens.yaml (new admin token, new Jev token)
#   --keep-token           with --force: regenerate .env only; tokens.yaml and the admin token stay valid
#   --rotate-db-password   with --force: also generate a new POSTGRES_PASSWORD
#                          (only valid for a NEW database: run 'make seamless-reset CONFIRM=yes' first)
#   --dir DIR              target directory (default: <repo>/deploy/compose)
#   -h, --help             show this help
#
# Environment (all optional):
#   TYPESAFE_API_KEY       Jev provider key, written to .env only
#   AGENTMEMORY_SECRET     bearer secret of your agentmemory server, written as SEAMLESS_MEMORY_SECRET
#   SEAMLESS_MEMORY_URL    agentmemory URL as seen from the containers
#                          (default http://host.docker.internal:3111)
#   TOKENS_FILE_MODE       mode of tokens.yaml (default 644: it holds only SHA-256 hashes and must be
#                          readable by the container's non-root user through the Colima bind mount)

set -euo pipefail
set +o xtrace   # never trace secrets, even when invoked as 'bash -x compose-init.sh'
umask 077

FORCE=0
KEEP_TOKEN=0
ROTATE_DB=0
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
TARGET_DIR="$SCRIPT_DIR/../deploy/compose"

die() { printf 'compose-init: error: %s\n' "$*" >&2; exit 1; }
info() { printf 'compose-init: %s\n' "$*" >&2; }
usage() { sed -n '2,/^$/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
  case "$1" in
    --force) FORCE=1 ;;
    --keep-token) KEEP_TOKEN=1 ;;
    --rotate-db-password) ROTATE_DB=1 ;;
    --dir)
      [ $# -ge 2 ] || die "--dir needs an argument"
      TARGET_DIR=$2
      shift
      ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'compose-init: unknown option: %s\n' "$1" >&2; exit 2 ;;
  esac
  shift
done

[ "$ROTATE_DB" -eq 0 ] || [ "$FORCE" -eq 1 ] || die "--rotate-db-password requires --force"
[ "$KEEP_TOKEN" -eq 0 ] || [ "$FORCE" -eq 1 ] || die "--keep-token requires --force"
[ -d "$TARGET_DIR" ] || die "directory not found: $TARGET_DIR"
TARGET_DIR=$(cd "$TARGET_DIR" && pwd)
ENV_FILE="$TARGET_DIR/.env"
TOKENS_FILE="$TARGET_DIR/tokens.yaml"
TOKENS_MODE=${TOKENS_FILE_MODE:-644}
case "$TOKENS_MODE" in
  [0-7][0-7][0-7]|[0-7][0-7][0-7][0-7]) ;;
  *) die "TOKENS_FILE_MODE must be an octal mode such as 644 or 600" ;;
esac

# --- helpers ---------------------------------------------------------------------------------

rand_hex() { # $1 = number of random bytes -> lowercase hex
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex "$1"
  elif [ -r /dev/urandom ]; then
    LC_ALL=C head -c "$1" /dev/urandom | od -An -vtx1 | tr -d ' \n'
  else
    die "no source of randomness (install openssl)"
  fi
}

rand_urlsafe() { # $1 = number of random bytes -> unpadded base64url, like Python secrets.token_urlsafe
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -base64 "$1" | tr -d '\n=' | tr '+/' '-_'
  elif [ -r /dev/urandom ]; then
    LC_ALL=C head -c "$1" /dev/urandom | base64 | tr -d '\n=' | tr '+/' '-_'
  else
    die "no source of randomness (install openssl)"
  fi
}

sha256_hex() { # stdin -> hex digest
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum | cut -d' ' -f1
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 | cut -d' ' -f1
  elif command -v openssl >/dev/null 2>&1; then
    openssl dgst -sha256 -r | cut -d' ' -f1
  else
    die "need sha256sum, shasum or openssl"
  fi
}

env_get() { # $1 = file, $2 = KEY -> value of the last assignment (single quotes stripped), empty if absent
  [ -f "$1" ] || return 0
  sed -n "s/^$2=//p" "$1" | tail -n 1 | sed "s/^'\\(.*\\)'\$/\\1/"
}

# keys of .env that this script derives or regenerates; every other active assignment is preserved by --force
MANAGED_KEYS=" POSTGRES_PASSWORD JEV_MCP_AUTH_TOKEN TYPESAFE_API_KEY COMPOSE_PROFILES SEAMLESS_JEV_MODE SEAMLESS_MEMORY_URL SEAMLESS_MEMORY_SECRET "

env_has() { # $1 = file, $2 = KEY -> success when an active (non-comment) assignment exists
  [ -f "$1" ] && grep -q "^$2=" "$1"
}

preserved_lines() { # $1 = file -> active KEY=VALUE lines whose key this script does not manage
  local line key
  [ -f "$1" ] || return 0
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in ''|'#'*|' '*|'export '*) continue ;; esac
    key=${line%%=*}
    [ "$key" != "$line" ] || continue
    case "$key" in [A-Za-z_]*) ;; *) continue ;; esac
    case "$key" in *[!A-Za-z0-9_]*) continue ;; esac
    case "$MANAGED_KEYS" in *" $key "*) continue ;; esac
    printf '%s\n' "$line"
  done < "$1"
}

reject_unsafe() { # $1 = variable name (for the message), $2 = value; never echoes the value
  case "$2" in
    *\'*|*$'\n'*|*$'\r'*) die "$1 contains a single quote or a line break, which .env cannot store safely" ;;
  esac
}

CURRENT_TMP=""
trap 'if [ -n "$CURRENT_TMP" ]; then rm -f "$CURRENT_TMP"; fi' EXIT

write_file() { # $1 = path, $2 = mode; content on stdin; atomic replace in the same directory
  local path=$1 mode=$2
  CURRENT_TMP=$(mktemp "$path.XXXXXX")
  cat > "$CURRENT_TMP"
  chmod "$mode" "$CURRENT_TMP"
  mv -f "$CURRENT_TMP" "$path"
  CURRENT_TMP=""
}

# --- .env ------------------------------------------------------------------------------------

if [ -e "$ENV_FILE" ] && [ "$FORCE" -ne 1 ]; then
  info "$ENV_FILE exists - keeping it (use --force to regenerate)"
else
  previous=0
  [ ! -e "$ENV_FILE" ] || previous=1

  pg_password=""
  if [ "$previous" -eq 1 ] && [ "$ROTATE_DB" -ne 1 ]; then
    pg_password=$(env_get "$ENV_FILE" POSTGRES_PASSWORD)
    [ -z "$pg_password" ] || info "keeping the existing POSTGRES_PASSWORD (use --rotate-db-password for a new database)"
  fi
  [ -n "$pg_password" ] || pg_password=$(rand_hex 24)
  jev_token=$(rand_hex 32)

  # secrets: your environment wins, then what the previous .env stored
  old_key=$(env_get "$ENV_FILE" TYPESAFE_API_KEY)
  api_key=${TYPESAFE_API_KEY:-}
  [ -n "$api_key" ] || api_key=$old_key
  memory_secret=${AGENTMEMORY_SECRET:-}
  [ -n "$memory_secret" ] || memory_secret=$(env_get "$ENV_FILE" SEAMLESS_MEMORY_SECRET)

  # agentmemory URL: your environment wins; otherwise a previous .env decides (a removed line means "memory
  # disabled" and stays that way); a first run uses the Colima host address
  if [ -n "${SEAMLESS_MEMORY_URL:-}" ]; then
    memory_url=$SEAMLESS_MEMORY_URL
  elif [ "$previous" -eq 1 ]; then
    memory_url=$(env_get "$ENV_FILE" SEAMLESS_MEMORY_URL)
  else
    memory_url=http://host.docker.internal:3111
  fi
  reject_unsafe TYPESAFE_API_KEY "$api_key"
  reject_unsafe AGENTMEMORY_SECRET "$memory_secret"
  reject_unsafe SEAMLESS_MEMORY_URL "$memory_url"

  # Jev sidecar: enabled when a key exists. A previous .env that already had a key keeps your on/off choice;
  # a newly supplied key (or a first run) switches it on.
  if [ -z "$api_key" ]; then
    profiles=""
    jev_mode=off
    info "TYPESAFE_API_KEY is not set: the Jev sidecar stays disabled (the advisor falls back to deterministic rules)."
    info "To enable it later: export TYPESAFE_API_KEY in your shell and re-run with --force --keep-token."
  elif [ "$previous" -eq 1 ] && [ -n "$old_key" ] && env_has "$ENV_FILE" COMPOSE_PROFILES; then
    profiles=$(env_get "$ENV_FILE" COMPOSE_PROFILES)
    jev_mode=$(env_get "$ENV_FILE" SEAMLESS_JEV_MODE)
    if [ -z "$jev_mode" ]; then
      if [ -n "$profiles" ]; then jev_mode=http; else jev_mode=off; fi
    fi
  else
    profiles=ai
    jev_mode=http
  fi

  preserved=$(preserved_lines "$ENV_FILE")
  if [ -n "$preserved" ]; then
    preserved_keys=$(printf '%s\n' "$preserved" | sed 's/=.*//' | tr '\n' ' ')
    info "preserved from the previous .env (names only): ${preserved_keys% }"
  fi

  {
    printf '# Seamless Migrate - Docker Compose environment (SDD 17.1)\n'
    printf '# Generated by scripts/compose-init.sh on %s. Git-ignored - never commit.\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf 'POSTGRES_PASSWORD=%s\n' "$pg_password"
    printf 'JEV_MCP_AUTH_TOKEN=%s\n' "$jev_token"
    if [ -n "$api_key" ]; then
      printf "TYPESAFE_API_KEY='%s'\n" "$api_key"
    else
      printf "# TYPESAFE_API_KEY='...'   Jev provider key; also set COMPOSE_PROFILES=ai and SEAMLESS_JEV_MODE=http\n"
    fi
    printf 'COMPOSE_PROFILES=%s\n' "$profiles"
    printf 'SEAMLESS_JEV_MODE=%s\n' "$jev_mode"
    if [ -n "$memory_url" ]; then
      printf '# agentmemory as seen from the containers; delete the line to run without memory\n'
      printf "SEAMLESS_MEMORY_URL='%s'\n" "$memory_url"
    else
      printf "# SEAMLESS_MEMORY_URL='http://host.docker.internal:3111'   agentmemory is disabled; uncomment to enable\n"
    fi
    if [ -n "$memory_secret" ]; then
      printf "SEAMLESS_MEMORY_SECRET='%s'\n" "$memory_secret"
    else
      printf "# SEAMLESS_MEMORY_SECRET='...'   only if your agentmemory server requires a bearer secret\n"
    fi
    if [ -n "$preserved" ]; then
      printf '# preserved from the previous .env by --force\n'
      printf '%s\n' "$preserved"
    fi
  } | write_file "$ENV_FILE" 600
  info "wrote $ENV_FILE (mode 600)"
fi

# --- tokens.yaml + admin token ---------------------------------------------------------------

admin_token=""
had_tokens=0
[ ! -e "$TOKENS_FILE" ] || had_tokens=1
if [ "$had_tokens" -eq 1 ] && { [ "$FORCE" -ne 1 ] || [ "$KEEP_TOKEN" -eq 1 ]; }; then
  info "$TOKENS_FILE exists - keeping it (the admin token stays valid; --force without --keep-token issues a new one)"
else
  old_entries=0
  if [ "$had_tokens" -eq 1 ]; then
    old_entries=$(grep -c '^ *- name:' "$TOKENS_FILE" || true)
  fi
  admin_token="smg_$(rand_urlsafe 32)"
  token_hash=$(printf '%s' "$admin_token" | sha256_hex)
  write_file "$TOKENS_FILE" "$TOKENS_MODE" <<EOF
# Seamless Migrate API tokens (SDD 13.1). Only SHA-256 hashes are stored. Git-ignored.
# Add more entries with:  seamless token create --name NAME --role {viewer,operator,approver,admin}
tokens:
  - name: admin
    role: admin
    sha256: ${token_hash}
EOF
  info "wrote $TOKENS_FILE (mode $TOKENS_MODE, hashes only)"
  if [ "$had_tokens" -eq 1 ]; then
    info "the previous admin token no longer works; entries in the old file: $old_entries (recreate other tokens with 'seamless token create')"
  fi
fi

# --- summary ---------------------------------------------------------------------------------

if [ -n "$admin_token" ]; then
  cat <<EOF

==============================================================================
 Seamless Migrate admin API token - shown ONCE, store it in a password manager

   ${admin_token}

 Use:   Authorization: Bearer <token>        (dashboard: sign in at /login)
 Only its SHA-256 is kept, in ${TOKENS_FILE}
 After --force, recreate the stack so the control plane reloads it:
   make seamless-down seamless-up
==============================================================================
EOF
fi

host_port=$(env_get "$ENV_FILE" SEAMLESS_HOST_PORT)
cat >&2 <<EOF

Next steps:
  make seamless-colima-up     # dedicated Colima profile "seamless" (once; keeps your active Docker context)
  make seamless-up            # or: make seamless-demo   (simulated clouds)
  open http://127.0.0.1:${host_port:-8080}/
EOF
