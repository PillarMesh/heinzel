#!/bin/sh

set -eu

if [ -z "${LOCALSTACK_AUTH_TOKEN:-}" ]; then
    printf '%s\n' 'ERROR: LOCALSTACK_AUTH_TOKEN is required' >&2
    exit 2
fi

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPOSITORY_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../../.." && pwd)
COMPOSE_FILE="$SCRIPT_DIR/compose.yaml"
PROJECT_NAME="pillarmesh-m0-localstack-$$"

cleanup() {
    status=$?
    if [ "$status" -ne 0 ]; then
        printf '%s\n' 'LocalStack Snowflake logs:' >&2
        docker compose --project-name "$PROJECT_NAME" --file "$COMPOSE_FILE" \
            logs --no-color snowflake >&2 || true
    fi
    docker compose --project-name "$PROJECT_NAME" --file "$COMPOSE_FILE" \
        down --volumes --remove-orphans >/dev/null 2>&1 || true
}
on_signal() {
    trap - EXIT HUP INT TERM
    cleanup
    exit 130
}
trap cleanup EXIT
trap on_signal HUP INT TERM

docker compose --project-name "$PROJECT_NAME" --file "$COMPOSE_FILE" up --detach --wait
cd "$REPOSITORY_ROOT"
env -u LOCALSTACK_AUTH_TOKEN -u VIRTUAL_ENV uv run python "$SCRIPT_DIR/wait_ready.py"
env -u LOCALSTACK_AUTH_TOKEN -u VIRTUAL_ENV PILLARMESH_LOCALSTACK_SNOWFLAKE=1 \
    uv run pytest -m emulator tests/emulators/test_localstack_snowflake.py -q
