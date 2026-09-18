#!/bin/sh

set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPOSITORY_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
VALIDATOR="$SCRIPT_DIR/validate.sh"
TEMP_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/heinzel-structure.XXXXXX")

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

copy_repository_fixture() {
    destination=$1
    mkdir -p "$destination"
    COPYFILE_DISABLE=1 tar -C "$REPOSITORY_ROOT" \
        --exclude='./.git' \
        --exclude='./.mypy_cache' \
        --exclude='./.pytest_cache' \
        --exclude='./.ruff_cache' \
        --exclude='./.venv' \
        --exclude='*/__pycache__' \
        --exclude='*/node_modules' \
        -cf - . | tar -xf - -C "$destination"
}

[ -x "$VALIDATOR" ] || fail "validator is missing or not executable: $VALIDATOR"

"$VALIDATOR" "$REPOSITORY_ROOT" >/dev/null

copy_repository_fixture "$TEMP_ROOT/complete"
"$VALIDATOR" "$TEMP_ROOT/complete" >/dev/null

cp -R -l "$TEMP_ROOT/complete" "$TEMP_ROOT/missing"
rm "$TEMP_ROOT/missing/README.md"
assert_fails_with "$TEMP_ROOT/missing" "MISSING: README.md"

cp -R -l "$TEMP_ROOT/complete" "$TEMP_ROOT/unexpected-top-level"
mkdir "$TEMP_ROOT/unexpected-top-level/scheduler"
assert_fails_with "$TEMP_ROOT/unexpected-top-level" "UNEXPECTED: scheduler"

cp -R -l "$TEMP_ROOT/complete" "$TEMP_ROOT/unexpected-component"
mkdir "$TEMP_ROOT/unexpected-component/services/scheduler"
assert_fails_with "$TEMP_ROOT/unexpected-component" "UNEXPECTED: services/scheduler"

# A local tool directory the repository already ignores must not fail the gate.
# `.gitignore` alone cannot achieve that: this validator enumerates the filesystem
# and never reads it, so an ignored directory was still reported UNEXPECTED and the
# gate failed for anyone whose tooling created one.
cp -R -l "$TEMP_ROOT/complete" "$TEMP_ROOT/ignored-tool-directory"
mkdir "$TEMP_ROOT/ignored-tool-directory/.superpowers"
"$VALIDATOR" "$TEMP_ROOT/ignored-tool-directory" >/dev/null \
    || fail "validator rejected an ignored local tool directory"

# The gate still exists to catch stray top-level directories, so widening it for one
# tool must not have widened it for everything.
cp -R -l "$TEMP_ROOT/complete" "$TEMP_ROOT/still-strict"
mkdir "$TEMP_ROOT/still-strict/.superpowers-not-really"
assert_fails_with "$TEMP_ROOT/still-strict" "UNEXPECTED: .superpowers-not-really"

printf 'PASS: repository structure validation fixtures\n'
