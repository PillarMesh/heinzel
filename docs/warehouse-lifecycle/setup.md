# Plan 3A witnessed acceptance setup

Plan 3A acceptance exercises a fresh PostgreSQL lifecycle followed by a fresh ClickHouse lifecycle
on one reserved local Docker environment. It is destructive only to resources recorded for that run.
Use a disposable host with no unrelated Heinzel warehouse containers. Cost sampling is still
scoped to exact HMAC-authenticated Plan 3A resource identities and never attributes whole-daemon
usage to the witness.

## Prerequisites

- Docker Engine with Compose v2, enough free space for both pinned images, and permission to create
  private networks, volumes, and loopback-only ports.
- The Python and `uv` versions pinned by this repository.
- A committed Heinzel checkout. Record its full commit; do not substitute a branch name.
- An owner-only directory outside the repository. Every configured output must be new or empty.
- Separate operator time for setup, retention-gated cleanup, and investigation. The authoritative
  witnessed command itself has a hard ten-minute ceiling. A run that cannot finish in that envelope
  is a failed gate requiring design review; it is not an expected 20-to-45-minute run.

The local Compose witness proves lifecycle conformance, not production readiness. In particular,
local storage encryption remains `deferred_local_acceptance`.

## Create the private shell environment

Run this in a fresh shell. Replace `/absolute/private/warehouse-lifecycle` with a path outside the repository.
The parent is intentionally created as mode `0700`; do not use `/tmp`, a shared workspace, or a
repository descendant.

```sh
export HEINZEL_WAREHOUSE_LIFECYCLE_PRIVATE_ROOT=/absolute/private/warehouse-lifecycle
mkdir -m 0700 "$HEINZEL_WAREHOUSE_LIFECYCLE_PRIVATE_ROOT"

export HEINZEL_WAREHOUSE_LIFECYCLE_STATE_PATH="$HEINZEL_WAREHOUSE_LIFECYCLE_PRIVATE_ROOT/state"
export HEINZEL_WAREHOUSE_LIFECYCLE_SECRET_DIRECTORY="$HEINZEL_WAREHOUSE_LIFECYCLE_PRIVATE_ROOT/secrets"
export HEINZEL_WAREHOUSE_LIFECYCLE_BACKUP_DIRECTORY="$HEINZEL_WAREHOUSE_LIFECYCLE_PRIVATE_ROOT/backups"
export HEINZEL_WAREHOUSE_LIFECYCLE_EVIDENCE_DIRECTORY="$HEINZEL_WAREHOUSE_LIFECYCLE_PRIVATE_ROOT/evidence"
export HEINZEL_WAREHOUSE_LIFECYCLE_RESERVATION_PATH="$HEINZEL_WAREHOUSE_LIFECYCLE_PRIVATE_ROOT/reservation.json"

export HEINZEL_WAREHOUSE_LIFECYCLE_SOURCE_COMMIT="$(git rev-parse HEAD)"
export HEINZEL_WAREHOUSE_LIFECYCLE_POSTGRES_IMAGE='postgres:18.6-bookworm@sha256:33c86c9cfb790e257e470b29e8c97bd1bd6fee0a70ab2d7a2e377ab639c09935'
export HEINZEL_WAREHOUSE_LIFECYCLE_CLICKHOUSE_IMAGE='clickhouse/clickhouse-server:25.8.32.4@sha256:7c39abeb161d627fa3ca6a1e5f6241ecdc24501e8463486e61b80be3ab4471b0'
export HEINZEL_WAREHOUSE_LIFECYCLE_RETENTION_DEADLINE="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"

export HEINZEL_WAREHOUSE_LIFECYCLE_STATE_ENCRYPTION_KEY="$(uv run python -c 'import secrets; print(secrets.token_urlsafe(48))')"
export HEINZEL_WAREHOUSE_LIFECYCLE_EVIDENCE_SIGNING_KEY="$(uv run python -c 'import secrets; print(secrets.token_urlsafe(48))')"
export HEINZEL_WAREHOUSE_LIFECYCLE_CREDENTIAL_CANARIES="$(uv run python -c 'import json,secrets; print(json.dumps([secrets.token_urlsafe(24) for _ in range(7)]))')"
```

Setting the deadline records that cleanup is authorized from the current UTC instant. The run still
requires the explicit cleanup flag, and it compares the real clock with this deadline before any
engine effect. To preserve resources longer, set a later approved deadline and do not start the
witness until that deadline has elapsed. The keys and raw canaries must remain only in this shell
environment. Never place them in a command argument, evidence file, issue, CI log, or chat
transcript.

Before running, confirm the configured parent and image pins without printing secrets:

```sh
test "$(stat -f '%Lp' "$HEINZEL_WAREHOUSE_LIFECYCLE_PRIVATE_ROOT")" = 700
test -z "$(find "$HEINZEL_WAREHOUSE_LIFECYCLE_PRIVATE_ROOT" -mindepth 1 -print -quit)"
docker version
docker compose version
uv sync --locked --all-packages
```

Configuration fails closed for relative paths, symlink components, repository descendants,
duplicate targets, group/world access, nonempty output, floating images, malformed commits, and
missing keys.
