#!/bin/sh

set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPOSITORY_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
VALIDATOR="$SCRIPT_DIR/validate.sh"
TEMP_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/pillarmesh-structure.XXXXXX")

cleanup() {
    rm -rf -- "$TEMP_ROOT"
}
trap cleanup EXIT HUP INT TERM

fail() {
    printf 'FAIL: %s\n' "$1" >&2
    exit 1
}

assert_fails_with() {
    fixture=$1
    expected=$2
    output_file="$TEMP_ROOT/output.txt"

    if "$VALIDATOR" "$fixture" >"$output_file" 2>&1; then
        fail "validator unexpectedly accepted $fixture"
    fi

    if ! grep -F "$expected" "$output_file" >/dev/null; then
        printf 'Validator output:\n' >&2
        sed 's/^/  /' "$output_file" >&2
        fail "expected failure output to contain: $expected"
    fi
}

[ -x "$VALIDATOR" ] || fail "validator is missing or not executable: $VALIDATOR"

"$VALIDATOR" "$REPOSITORY_ROOT" >/dev/null

cp -R "$REPOSITORY_ROOT" "$TEMP_ROOT/complete"
"$VALIDATOR" "$TEMP_ROOT/complete" >/dev/null

cp -R "$TEMP_ROOT/complete" "$TEMP_ROOT/missing"
rm "$TEMP_ROOT/missing/README.md"
assert_fails_with "$TEMP_ROOT/missing" "MISSING: README.md"

cp -R "$TEMP_ROOT/complete" "$TEMP_ROOT/unexpected-top-level"
mkdir "$TEMP_ROOT/unexpected-top-level/scheduler"
assert_fails_with "$TEMP_ROOT/unexpected-top-level" "UNEXPECTED: scheduler"

cp -R "$TEMP_ROOT/complete" "$TEMP_ROOT/unexpected-component"
mkdir "$TEMP_ROOT/unexpected-component/services/scheduler"
assert_fails_with "$TEMP_ROOT/unexpected-component" "UNEXPECTED: services/scheduler"

printf 'PASS: repository structure validation fixtures\n'
