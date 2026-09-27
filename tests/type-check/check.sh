#!/bin/sh
# Type check the test directories, one directory per mypy run.
#
# Colocated tests are where a double drifts from the component it stands in for.
# A stub that took `list_proposals(tenant_id)` against a repository requiring
# `(tenant_id, request_id)` made every data product read return 500 while its unit
# tests passed, because nothing type checked the file the stub lived in.
#
# Every test directory must be named below. A directory that is neither COVERED nor
# PENDING fails this gate, so a new component cannot arrive uncovered in silence --
# the gap this gate exists for was itself an unenumerated directory.

set -u

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
CONFIG="$ROOT/tests/type-check/mypy.ini"
status=0

# `explicit_package_bases` resolves a module name against the working directory, so this
# gate has to name one. Run from `services/` instead, the root is not a base at all and
# the run reports 162 errors that do not exist; add the root to MYPYPATH and the working
# directory competes with it, so `services/runtime/tests/test_acquisition.py` resolves
# as both `runtime.tests.test_acquisition` and `services.runtime.tests.test_acquisition`
# and the run stops. Every path below is already absolute; this makes the naming
# cwd-independent too.
cd -- "$ROOT" || exit 1

# Directories that must type check. Move a directory here from PENDING once its
# errors are fixed; that promotion is the unit of work, one directory per change.
COVERED='
apps/console/server/tests
packages/contract-model/tests
packages/execution-graph/tests
packages/iir/tests
packages/provider-sdk/tests
providers/clickhouse/tests
providers/openmetadata/tests
providers/postgresql/tests
providers/snowflake/tests
providers/stripe/tests
providers/superset/tests
services/access-control/tests
services/authoring-mcp/tests
services/bi-control/tests
services/catalog-control/tests
services/compiler/tests
services/connection-broker/tests
services/context-exposure/tests
services/contract/tests
services/dbt-adapter/tests
services/evidence/tests
services/knowledge-graph/tests
services/request-management/tests
services/runtime/tests
services/semantic-registry/tests
services/state/tests
services/trigger/tests
services/warehouse-control/tests
tests/acceptance
tests/ci
tests/conformance
tests/emulators
tests/end-to-end
tests/fault-injection
tests/integration
tests/quickstart
tests/release
'

# Known uncovered, each followed by the error count measured at the time of listing,
# so a reader can size the next promotion rather than guess. Only the first field is
# read as a directory.
#
# Empty: every test directory in the repository is covered. A directory added here is
# debt, not an exemption -- list it with its count, and take it off this list in the
# change that brings the count to zero.
PENDING='
'

# Each line is a directory optionally followed by a note, so read the first field and
# drop the rest. Word-splitting the lists whole would make a trailing error count a
# listed entry in its own right, and a line promoted verbatim into COVERED would then
# be reported as a missing directory named after its count.
directories() {
    printf '%s\n' "$1" | while read -r directory _; do
        [ -n "$directory" ] || continue
        printf '%s\n' "$directory"
    done
}

COVERED_DIRECTORIES=$(directories "$COVERED")
PENDING_DIRECTORIES=$(directories "$PENDING")

is_listed() {
    for listed in $COVERED_DIRECTORIES $PENDING_DIRECTORIES; do
        [ "$listed" = "$1" ] && return 0
    done
    return 1
}

# Every directory holding test modules, discovered rather than assumed.
discovered=$(
    {
        for path in "$ROOT"/apps/*/*/tests "$ROOT"/packages/*/tests \
                    "$ROOT"/providers/*/tests "$ROOT"/services/*/tests "$ROOT"/tests/*; do
            [ -d "$path" ] || continue
            find "$path" -name '*.py' -type f | head -n 1 | grep -q . || continue
            printf '%s\n' "${path#"$ROOT"/}"
        done
    } | sort
)

for directory in $discovered; do
    if ! is_listed "$directory"; then
        printf 'UNLISTED: %s is neither covered nor pending in %s\n' \
            "$directory" "tests/type-check/check.sh" >&2
        status=1
    fi
done

for directory in $COVERED_DIRECTORIES; do
    if [ ! -d "$ROOT/$directory" ]; then
        printf 'MISSING: covered directory does not exist: %s\n' "$directory" >&2
        status=1
        continue
    fi
    if ! uv run mypy --config-file "$CONFIG" \
        --cache-dir "$ROOT/.mypy_cache/type-check/$(printf '%s' "$directory" | tr / _)" \
        "$ROOT/$directory"; then
        status=1
    fi
done

if [ "$status" -eq 0 ]; then
    printf 'PASS: test directory type checking\n'
fi

exit "$status"
