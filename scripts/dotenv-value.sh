#!/bin/sh
# dotenv-value.sh [--defined] KEY FILE - print the value Compose reads for KEY from FILE (deploy/compose/.env), by
# compose-go's dotenv rules: a leading UTF-8 byte-order mark ignored, an `export ` prefix, a key ended by `=` or `:`
# (its YAML-style form), spaces around the key and the value, single or double quotes (a backslash escapes the
# quote), an inline comment after ` #` in an unquoted value; the last definition wins. A value Compose would
# interpolate - a `$` in an unquoted or double-quoted value - is refused, never guessed: a message, exit 3; a quote
# that never closes, which Compose refuses with the whole file, is no interpolation: a message, exit 4. Prints
# nothing when FILE is missing or defines no KEY. With --defined it prints nothing and exits 0 when FILE defines KEY
# (an empty value too), 1 when it does not. The Makefile and seamless-ctl.sh read SEAMLESS_VERSION, SEAMLESS_HOST_PORT
# and whether .env sets SEAMLESS_DEMO through it, so they take what Compose does (SDD §17.2, reviews RV38B-07,
# RV38B-09, RV41A-01..04). POSIX sh and awk, in the C locale: bytes, whatever the user's locale.
defined=0
[ "${1-}" = --defined ] && { defined=1; shift; }
[ $# -eq 2 ] || { echo "usage: dotenv-value.sh [--defined] KEY FILE" >&2; exit 2; }
if [ ! -r "$2" ]; then [ "$defined" = 1 ] && exit 1; exit 0; fi
LC_ALL=C
export LC_ALL
exec awk -v key="$1" -v file="$2" -v defined="$defined" '
function trim(s) { sub(/^[ \t]+/, "", s); sub(/[ \t]+$/, "", s); return s }
BEGIN { sq = sprintf("%c", 39); dq = "\""; bs = "\\"; bom = sprintf("%c%c%c", 239, 187, 191) }
{
  line = $0
  if (NR == 1 && substr(line, 1, 3) == bom) line = substr(line, 4)
  sub(/\r$/, "", line)
  if (line ~ /^[ \t]*(#|$)/) next
  sub(/^[ \t]*export[ \t]+/, "", line)
  end = index(line, "=")
  colon = index(line, ":")
  if (colon && (!end || colon < end)) end = colon
  if (end == 0 || trim(substr(line, 1, end - 1)) != key) next
  value = substr(line, end + 1)
  sub(/^[ \t]+/, "", value)
  quote = substr(value, 1, 1)
  unterminated = 0
  if (quote == sq || quote == dq) {
    out = ""
    unterminated = 1
    for (i = 2; i <= length(value); i++) {
      c = substr(value, i, 1)
      if (c == bs && i < length(value)) {
        i++
        c = substr(value, i, 1)
        out = out (c == quote ? c : bs c)
        continue
      }
      if (c == quote) { unterminated = 0; break }
      out = out c
    }
    value = out
    interpolated = quote == dq
  } else {
    cut = index(value, " #")
    if (cut) value = substr(value, 1, cut - 1)
    value = trim(value)
    interpolated = 1
  }
  found = value
  found_interpolated = interpolated
  found_unterminated = unterminated
  seen = 1
}
END {
  if (!seen) exit defined ? 1 : 0
  if (defined) exit 0
  if (found_unterminated) {
    printf "dotenv-value.sh: %s gives %s a quote it never closes, which Compose refuses\n", file, key > "/dev/stderr"
    exit 4
  }
  if (found_interpolated && index(found, "$")) {
    printf "dotenv-value.sh: %s sets %s through a variable (%s), which Compose interpolates: these scripts read plain values - write the value itself\n", file, key, found > "/dev/stderr"
    exit 3
  }
  if (found != "") print found
}
' "$2"
