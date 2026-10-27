#!/usr/bin/env bash
# Translate and compile the GenApp programs for the off-mainframe test run.
# Runs inside the test container (see test/run.sh). Output goes to test/build.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$ROOT/base/src"
RT="$ROOT/test/runtime"
OUT="${GENAPP_BUILD:-$ROOT/test/build}"

# Programs that need a 3270 terminal, CICS browse/startbr, or web services
# and are therefore not part of the off-mainframe tests.
EXCLUDE=" lgtestc1 lgtestp1 lgtestp2 lgtestp3 lgtestp4 lgwebst5 lgsetup lgastat1 lgicvs01 lgipvs01 "

# GnuCOBOL's default binary truncation matches IBM's TRUNC(STD), which is
# what GenApp is compiled with (base/cntl/cobol.jcl sets no TRUNC option).
# GenApp relies on it: LGAPDB01 computes a padding length in a PIC S9(4)
# COMP field, and without truncation it would overrun a 3900-byte buffer.
COBC_FLAGS=(-Wno-others)

# Ubuntu's cobc passes -D_FORTIFY_SOURCE twice, so gcc warns about it for
# every file. Drop just that noise and keep all other compiler output.
cobc() {
    command cobc "$@" 2> >(grep -v -e '"_FORTIFY_SOURCE" redefined' -e 'location of the previous definition' >&2)
}

rm -rf "$OUT"
mkdir -p "$OUT/cob"

echo "Building test runtime"
cobc -x "${COBC_FLAGS[@]}" -I "$RT" -o "$OUT/cicsrun" \
    -A "-I$(pg_config --includedir)" \
    "$RT/CICSRUN.cbl" "$RT/genapprt.c" -lpq -Q -Wl,--export-dynamic

fail=0
count=0
for f in "$SRC"/lg*.cbl; do
    name="$(basename "$f" .cbl)"
    [[ "$EXCLUDE" == *" $name "* ]] && continue
    prog="$(echo "$name" | tr '[:lower:]' '[:upper:]')"
    if ! python3 "$ROOT/test/tools/precompile.py" --copy-dir "$SRC" "$f" "$OUT/cob/$name.cob"; then
        fail=1
        continue
    fi
    if ! cobc -m "${COBC_FLAGS[@]}" -o "$OUT/$prog.so" "$OUT/cob/$name.cob"; then
        echo "compile failed: $name (translated source: ${OUT#$ROOT/}/cob/$name.cob)" >&2
        fail=1
        continue
    fi
    count=$((count + 1))
done

if [[ $fail -ne 0 ]]; then
    echo "BUILD FAILED" >&2
    exit 1
fi
echo "Built $count programs into ${OUT#$ROOT/}"
