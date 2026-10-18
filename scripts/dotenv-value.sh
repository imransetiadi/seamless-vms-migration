#!/bin/sh
# dotenv-value.sh KEY FILE - print the value Compose reads for KEY from FILE (deploy/compose/.env), by its dotenv
# rules: an `export ` prefix, spaces around the key and the value, single or double quotes, an inline comment after
# an unquoted value (a `#` after a space or a tab); the last definition wins. Prints nothing when FILE is missing or
# defines no KEY. The Makefile and seamless-ctl.sh read SEAMLESS_VERSION and SEAMLESS_HOST_PORT through it, so they
# name the image and the port Compose does (SDD §17.2, reviews RV38B-07, RV38B-09). POSIX sh and awk.
[ $# -eq 2 ] || { echo "usage: dotenv-value.sh KEY FILE" >&2; exit 2; }
[ -r "$2" ] || exit 0
exec awk -v key="$1" '
function trim(s) { sub(/^[ \t]+/, "", s); sub(/[ \t]+$/, "", s); return s }
BEGIN { sq = sprintf("%c", 39); dq = "\"" }
{
  line = $0
  sub(/\r$/, "", line)
  if (line ~ /^[ \t]*(#|$)/) next
  sub(/^[ \t]*export[ \t]+/, "", line)
  eq = index(line, "=")
  if (eq == 0 || trim(substr(line, 1, eq - 1)) != key) next
  value = substr(line, eq + 1)
  sub(/^[ \t]+/, "", value)
  quote = substr(value, 1, 1)
  if (quote == sq || quote == dq) {
    rest = substr(value, 2)
    end = index(rest, quote)
    value = end ? substr(rest, 1, end - 1) : rest
  } else {
    if (match(value, /[ \t]#/)) value = substr(value, 1, RSTART - 1)
    value = trim(value)
  }
  found = value
  seen = 1
}
END { if (seen && found != "") print found }
' "$2"
