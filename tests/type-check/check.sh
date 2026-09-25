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
providers/snowflake/tests
providers/stripe/tests
providers/superset/tests
services/access-control/tests
services/authoring-mcp/tests
services/catalog-control/tests
services/compiler/tests
services/connection-broker/tests
services/context-exposure/tests
services/contract/tests
services/dbt-adapter/tests
services/evidence/tests
services/knowledge-graph/tests
services/request-management/tests
services/semantic-registry/tests
services/state/tests
services/bi-control/tests
services/trigger/tests
services/warehouse-control/tests
tests/conformance
tests/end-to-end
tests/fault-injection
tests/quickstart
tests/release
'

# Known uncovered, each followed by the error count measured at the time of listing.
# These are debt, not exemptions -- the count is here so a reader can size the next
# promotion rather than guess. Only the first field is read as a directory.
PENDING='
providers/postgresql/tests 72
services/runtime/tests 63
tests/acceptance 30
tests/ci 24
tests/emulators 7
tests/integration 23
'

is_listed() {
    for listed in $COVERED $PENDING; do
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

for directory in $COVERED; do
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
