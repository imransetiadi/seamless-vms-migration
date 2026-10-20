#!/bin/sh
# dotenv-value.sh [--defined] KEY FILE | --unclosed FILE - print the value Compose reads for KEY from FILE
# (deploy/compose/.env), by compose-go's dotenv rules: a leading UTF-8 byte-order mark ignored, an `export ` prefix, a
# key ended by `=` or `:` (its YAML-style form), white space around the key and the value - the Latin-1 white space
# compose-go trims: space, tab, vertical tab, form feed, carriage return, NEL and the no-break space -, single or double
# quotes (a backslash escapes the quote), a statement after a closing quote read as the next assignment, an inline
# comment after ` #` in an unquoted value; the last definition wins (review RV42A-05). A quote
# never closed on any line of FILE - whatever key is read, whatever a later line defines - is no value: Compose
# refuses the whole file for it, for every command, so the reader refuses any key of it, naming the line's key: a
# message, exit 4. A value Compose would interpolate - a `$` in an unquoted or double-quoted value - is refused, never
# guessed: a message, exit 3. Prints nothing when FILE is missing or defines no KEY. With --defined it prints nothing
# and exits 0 when FILE defines KEY (an empty value too), 1 when it does not; --unclosed prints the key of the first
# line whose quote never closes, nothing when every quote closes or there is no FILE. The Makefile and seamless-ctl.sh
# read SEAMLESS_VERSION, SEAMLESS_HOST_PORT and whether .env sets SEAMLESS_DEMO through it, so they take what Compose
# does (SDD §17.2, reviews RV38B-07, RV38B-09, RV41A-01..04, RV46A-01, RV47-01). POSIX sh and awk, in the C locale:
# bytes, whatever the user's locale.
mode=value
case ${1-} in
  --defined) mode=defined; shift ;;
  --unclosed) mode=unclosed; shift; set -- '' "$@" ;;
esac
[ $# -eq 2 ] || { echo "usage: dotenv-value.sh [--defined] KEY FILE | --unclosed FILE" >&2; exit 2; }
if [ ! -r "$2" ]; then [ "$mode" = defined ] && exit 1; exit 0; fi
LC_ALL=C
export LC_ALL
exec awk -v key="$1" -v file="$2" -v mode="$mode" '
# the Latin-1 white space compose-go trims (unicode.IsSpace below U+0100) - NEL and the no-break space in UTF-8: bytes,
# in the C locale
function lead(s) { while (match(s, "^" space)) s = substr(s, RLENGTH + 1); return s }
function trail(s) { while (match(s, space "$")) s = substr(s, 1, RSTART - 1); return s }
function trim(s) { return trail(lead(s)) }
# the value Compose reads from what follows the key: sets unterminated (a quote never closed), interpolated (a value
# Compose interpolates: unquoted or double-quoted) and rest (what follows a closing quote: the next statement)
function scan(value,    quote, out, i, c) {
  value = lead(value)
  quote = substr(value, 1, 1)
  unterminated = 0
  rest = ""
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
      if (c == quote) { unterminated = 0; rest = substr(value, i + 1); break }
      out = out c
    }
    interpolated = quote == dq
    return out
  }
  i = index(value, " #")
  if (i) value = substr(value, 1, i - 1)
  interpolated = 1
  return trim(value)
}
BEGIN {
  sq = sprintf("%c", 39); dq = "\""; bs = "\\"; bom = sprintf("%c%c%c", 239, 187, 191)
  space = "([ \t\v\f\r]|" sprintf("%c%c", 194, 133) "|" sprintf("%c%c", 194, 160) ")"
}
{
  line = $0
  if (NR == 1 && substr(line, 1, 3) == bom) line = substr(line, 4)
  sub(/\r$/, "", line)
  # one statement, then any after a closing quote: compose-go reads it as the next assignment (RV42A-05)
  while (1) {
    line = lead(line)
    if (line == "" || substr(line, 1, 1) == "#") next
    sub(/^export[ \t]+/, "", line)
    end = index(line, "=")
    colon = index(line, ":")
    if (colon && (!end || colon < end)) end = colon
    if (end == 0) next
    name = trim(substr(line, 1, end - 1))
    value = scan(substr(line, end + 1))
    # Compose refuses the whole file at the first quote it never closes (RV47-01)
    if (unterminated && broken == "") broken = name
    if (name == key) {
      found = value
      found_interpolated = interpolated
      seen = 1
    }
    if (rest == "") next
    line = rest
  }
}
END {
  if (mode == "unclosed") { if (broken != "") print broken; exit 0 }
  if (mode == "defined") exit seen ? 0 : 1
  if (broken != "") {
    printf "dotenv-value.sh: %s gives %s a quote it never closes, which Compose refuses with the whole file\n", file, broken > "/dev/stderr"
    exit 4
  }
  if (!seen) exit 0
  if (found_interpolated && index(found, "$")) {
    printf "dotenv-value.sh: %s sets %s through a variable (%s), which Compose interpolates: these scripts read plain values - write the value itself\n", file, key, found > "/dev/stderr"
    exit 3
  }
  if (found != "") print found
}
' "$2"
