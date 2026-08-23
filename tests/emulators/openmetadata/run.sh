#!/bin/sh

set -eu

if [ -z "${DOCKER_CONFIG:-}" ]; then
    printf '%s' 'ERROR: DOCKER_CONFIG is required\n' >&2
    exit 2
fi

if ! command -v docker >/dev/null 2>&1; then
    printf '%s\n' 'ERROR: docker is required' >&2
    exit 2
fi

if [ -z "${PILLARMESH_OPENMETADATA_BOOTSTRAP_ADMIN_PASSWORD:-}" ]; then
    printf '%s\n' 'ERROR: PILLARMESH_OPENMETADATA_BOOTSTRAP_ADMIN_PASSWORD is required' >&2
    exit 2
fi

if [ -z "${PILLARMESH_OPENMETADATA_SECRET_STORE_KEY:-}" ]; then
    printf '%s\n' 'ERROR: PILLARMESH_OPENMETADATA_SECRET_STORE_KEY is required' >&2
    exit 2
fi

if ! docker info >/dev/null 2>&1; then
    printf '%s\n' 'ERROR: Docker daemon is unavailable' >&2
    exit 2
fi

if [ "$#" -eq 0 ]; then
    set -- pytest tests/integration/test_openmetadata_live.py -q
fi

REPOSITORY_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../../.." && pwd)
cd "$REPOSITORY_ROOT"
exec env -u VIRTUAL_ENV uv run "$@"
