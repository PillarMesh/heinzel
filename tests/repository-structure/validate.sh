#!/bin/sh

set -u

TARGET=${1:-$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)}

if [ ! -d "$TARGET" ]; then
    printf 'ERROR: repository target is not a directory: %s\n' "$TARGET" >&2
    exit 2
fi

status=0

required_paths='
.editorconfig
.gitattributes
.gitignore
.github
.github/pull_request_template.md
.github/workflows/repository-structure.yml
.python-version
CONTRIBUTING.md
LICENSE
NOTICE
README.md
SECURITY.md
pyproject.toml
uv.lock
apps
apps/README.md
deploy
deploy/README.md
docs
docs/architecture/decisions/ADR-0001-monorepo-structure.md
docs/architecture/decisions/ADR-0003-managed-data-engineering-platform.md
docs/architecture/decisions/ADR-0007-access-control.md
docs/architecture/repository-layout.md
packages
packages/README.md
providers
providers/README.md
services
services/README.md
tests
tests/repository-structure/test.sh
tests/repository-structure/validate.sh
'

for path in $required_paths; do
    if [ ! -e "$TARGET/$path" ]; then
        printf 'MISSING: %s\n' "$path" >&2
        status=1
    fi
done

for entry_path in "$TARGET"/* "$TARGET"/.[!.]* "$TARGET"/..?*; do
    [ -e "$entry_path" ] || continue
    entry=${entry_path##*/}
    case "$entry" in
        .git|.editorconfig|.env.example|.gitattributes|.gitignore|.github|.mypy_cache|.pytest_cache|.python-version|.ruff_cache|.hypothesis|.venv|AGENTS.md|CONTRIBUTING.md|LICENSE|NOTICE|CODE_OF_CONDUCT.md|CHANGELOG.md|THIRD_PARTY_NOTICES.md|README.md|SECURITY.md|apps|deploy|docs|packages|providers|pyproject.toml|services|tests|uv.lock)
            ;;
        *)
            printf 'UNEXPECTED: %s\n' "$entry" >&2
            status=1
            ;;
    esac
done

validate_components() {
    area=$1
    allowed=$2

    [ -d "$TARGET/$area" ] || return
    for component_path in "$TARGET/$area"/*; do
        [ -d "$component_path" ] || continue
        component=${component_path##*/}
        case " $allowed " in
            *" $component "*) ;;
            *)
                printf 'UNEXPECTED: %s/%s\n' "$area" "$component" >&2
                status=1
                ;;
        esac
    done
}

validate_components apps 'console'
validate_components services 'access-control authoring-mcp bi-control catalog-control compiler connection-broker context-exposure contract dbt-adapter evidence knowledge-graph provider-registry reconciliation relay request-management runtime semantic-registry state trigger warehouse-control'
validate_components packages 'client-sdk contract-model execution-graph iir observability provider-sdk'

if [ "$status" -eq 0 ]; then
    printf 'OK: repository structure is valid\n'
fi

exit "$status"
