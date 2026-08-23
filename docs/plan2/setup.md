# Plan 2 witnessed-run setup

This runbook prepares one disposable, host-private OpenMetadata 1.13.3 emulator for
the Plan 2 witnessed acceptance. It does not provision a customer catalog, use a
production endpoint, or create a supported external-catalog binding. The semantic
registry remains the authority for process, candidate, authority, review, approval,
contract, publication, and drift state; OpenMetadata is the catalog presentation and
provider-observation boundary.

The witnessed run is one exclusive cycle. Do not start a second cycle against the
same Docker project, output directory, state directory, or private ledger. Do not
reuse an old catalog or an old evidence package as proof of a new transaction.

## Preconditions

Run these commands from the repository root:

```sh
repository_root=$(git rev-parse --show-toplevel)
cd "$repository_root"
test "$(git rev-parse --show-toplevel)" = "$PWD"
test -f pyproject.toml
test -f uv.lock
test -x tests/emulators/openmetadata/run.sh
test -f tests/emulators/openmetadata/compose.yaml
test -f tests/emulators/openmetadata/wait_ready.py
test -f tests/integration/test_openmetadata_live.py
test -f tests/integration/test_openmetadata_publication_live.py
test -f tests/acceptance/run_plan2.py
test -f tests/acceptance/plan2_orchestration.py
```

The last two checks are load-bearing. They fail until the Task 9 witnessed runner
and orchestration module exist; do not replace them with an older M0 runner or claim
Plan 2 acceptance from the Task 8 publication test alone.

The host must provide:

- Python 3.13 as required by `pyproject.toml` and `uv`;
- Docker Engine with a running daemon and Docker Compose v2 (`docker compose version`);
- at least 8 GiB available memory for the pinned MySQL, Elasticsearch, OpenMetadata,
  and ingestion services;
- no externally reachable bind for the emulator ports; and
- an owner-private parent directory for every local run target.

Check the tool versions without recording machine-specific output in public evidence:

```sh
python3 --version
uv --version
docker version --format '{{.Server.Version}}'
docker compose version
docker info >/dev/null
```

The repository lock is the offline dependency authority:

```sh
uv sync --locked --all-packages
uv lock --check
```

## Pinned emulator

`tests/emulators/openmetadata/compose.yaml` is the only accepted Compose definition.
It uses OpenMetadata release 1.13.3 and the release-compatible database, search, and
ingestion images pinned by digest:

| Component | Image digest in the repository |
| --- | --- |
| MySQL-compatible OpenMetadata database | `docker.getcollate.io/openmetadata/db@sha256:8a77669a2e64769dbb3ba4684fd527cc4a68e54879b199a6a8f1e74fa14da557` |
| Elasticsearch | `docker.elastic.co/elasticsearch/elasticsearch@sha256:4f6bdcb742e892539c6ac49b0dd3e4e182e90218546e8c6a22db378c344acb60` |
| OpenMetadata server | `docker.getcollate.io/openmetadata/server@sha256:6c878281973d9e2c366e9da4f256a744acf67b1e53195fab67c3191e504e4169` |
| OpenMetadata ingestion | `docker.getcollate.io/openmetadata/ingestion@sha256:fe5effad9dbce98852b2f588905a4a8926c3c03de97fcceac8fe8e3ec927d717` |

The Compose file binds only `127.0.0.1:8585` and `127.0.0.1:8586`. Do not change
those bindings, image digests, search settings, or database versions for an acceptance
run. A tag, a different Elasticsearch major, or a public port is a different and
unverified environment.

Pulling the exact images is optional when they are already present, but if performed
it must use the exact references above:

```sh
docker pull docker.getcollate.io/openmetadata/db@sha256:8a77669a2e64769dbb3ba4684fd527cc4a68e54879b199a6a8f1e74fa14da557
docker pull docker.elastic.co/elasticsearch/elasticsearch@sha256:4f6bdcb742e892539c6ac49b0dd3e4e182e90218546e8c6a22db378c344acb60
docker pull docker.getcollate.io/openmetadata/server@sha256:6c878281973d9e2c366e9da4f256a744acf67b1e53195fab67c3191e504e4169
docker pull docker.getcollate.io/openmetadata/ingestion@sha256:fe5effad9dbce98852b2f588905a4a8926c3c03de97fcceac8fe8e3ec927d717
```

The Plan 2 runner starts the stack through the existing provider lifecycle. Do not
run `docker compose up` manually: the provider records the generated project,
containers, volumes, network, tenant resources, service accounts, and backup artifact
in its private resource ledger so teardown can resolve exact targets.

## Secret injection

Supply every value below from an external secret manager or an ephemeral shell. Never
commit a `.env` file, put a secret in a command argument, print the environment, or
copy a secret into evidence. The names are documented; the values are not.

Required by `tests/emulators/openmetadata/run.sh`:

- `DOCKER_CONFIG` — absolute path to the operator's Docker client configuration;
- `PILLARMESH_OPENMETADATA_BOOTSTRAP_ADMIN_PASSWORD` — the actual credential with
  which this fresh emulator initialized `admin@open-metadata.org`. It is used once
  to rotate into a run-generated administrator credential; an arbitrary newly
  generated value will not bootstrap an already initialized image; and
- `PILLARMESH_OPENMETADATA_SECRET_STORE_KEY` — Fernet key for the encrypted private
  operation-secret directory. Generate a fresh key for this cycle.

Required by the Compose services and consumed only through the provider's controlled
environment:

- `PILLARMESH_OPENMETADATA_MYSQL_ROOT_PASSWORD`;
- `PILLARMESH_OPENMETADATA_DATABASE_PASSWORD`; and
- `PILLARMESH_OPENMETADATA_AIRFLOW_DATABASE_PASSWORD`.

Required run-local paths and evidence controls, supplied to the Task 9 runner:

- `PILLARMESH_PLAN2_STATE_PATH` — fresh private SQLite/control state path;
- `PILLARMESH_PLAN2_OUTPUT_DIR` — fresh evidence output directory;
- `PILLARMESH_PLAN2_CLEANUP_LEDGER_PATH` — fresh owner-readable private ledger path;
- `PILLARMESH_PLAN2_SECRET_STORE_DIR` — fresh `0700` encrypted secret directory;
- `PILLARMESH_PLAN2_BACKUP_PATH` — fresh backup artifact path outside the repository;
- `PILLARMESH_PLAN2_OPERATOR_PSEUDONYM`; and
- `PILLARMESH_PLAN2_HOST_PSEUDONYM`.

The runner may use additional non-secret correlation settings only if they are named
by its committed configuration module. Do not invent a fallback variable, endpoint,
credential, or catalog identifier. The provider endpoint for this emulator is the
fixed loopback URL `http://127.0.0.1:8585`; it is operational state and must not be
exported in the public evidence package.

The cleanup ledger is not deletion authority merely because it is private. Every
entry binds its resource kind and exact identifier into a digest, the ledger binds
all configured run paths and operator/host pseudonyms into a scope digest, and the
whole ledger is authenticated with an HMAC derived from the externally supplied
secret-store key. Cleanup refuses an altered entry, foreign scope, duplicate
singleton, unsupported resource kind, or invalid HMAC.

Create fresh private paths before injection. The parent must already be private and
the targets must not exist:

```sh
umask 077
plan2_private_parent="$(mktemp -d "${TMPDIR:-/tmp}/pillarmesh-plan2.XXXXXX")"
plan2_private_parent="$(cd "$plan2_private_parent" && pwd -P)"
chmod 700 "$plan2_private_parent"
export PILLARMESH_PLAN2_STATE_PATH="$plan2_private_parent/state/plan2.sqlite"
export PILLARMESH_PLAN2_OUTPUT_DIR="$plan2_private_parent/evidence"
export PILLARMESH_PLAN2_CLEANUP_LEDGER_PATH="$plan2_private_parent/private-ledger.json"
export PILLARMESH_PLAN2_SECRET_STORE_DIR="$plan2_private_parent/openmetadata-operation-secrets"
export PILLARMESH_PLAN2_BACKUP_PATH="$plan2_private_parent/openmetadata-backup.sql"
export PILLARMESH_PLAN2_OPERATOR_PSEUDONYM="operator-1"
export PILLARMESH_PLAN2_HOST_PSEUDONYM="host-1"
mkdir -p "$(dirname "$PILLARMESH_PLAN2_STATE_PATH")"
test ! -e "$PILLARMESH_PLAN2_STATE_PATH"
test ! -e "$PILLARMESH_PLAN2_OUTPUT_DIR"
test ! -e "$PILLARMESH_PLAN2_CLEANUP_LEDGER_PATH"
test ! -e "$PILLARMESH_PLAN2_SECRET_STORE_DIR"
test ! -e "$PILLARMESH_PLAN2_BACKUP_PATH"
```

The witnessed command also requires a clean Git checkout. It refuses tracked or
untracked changes before exporting evidence, then binds both the exact `HEAD` revision
and a SHA-256 digest of the staged source inventory. Commit the reviewed implementation
before the witnessed run; do not use `git stash` as a way to hide unreviewed source.

The state directory is dedicated to this run. The runner pre-registers the control,
journey, semantic, request, publication, and replay artifacts plus possible SQLite
journal files before creating them. Do not place unrelated files in that directory.

The operator must inject the six secret variables in the same shell that invokes the
runner. Verify presence without printing values:

```sh
for variable_name in \
  DOCKER_CONFIG \
  PILLARMESH_OPENMETADATA_BOOTSTRAP_ADMIN_PASSWORD \
  PILLARMESH_OPENMETADATA_SECRET_STORE_KEY \
  PILLARMESH_OPENMETADATA_MYSQL_ROOT_PASSWORD \
  PILLARMESH_OPENMETADATA_DATABASE_PASSWORD \
  PILLARMESH_OPENMETADATA_AIRFLOW_DATABASE_PASSWORD; do
  test -n "${!variable_name:?missing $variable_name}"
done
test -d "$DOCKER_CONFIG"
test "$(stat -f '%Lp' "$plan2_private_parent")" = 700
```

The `${!variable_name}` form is supported by Bash, not the repository's `/bin/sh`
launcher. Run this verification in Bash (`bash -s` or an interactive Bash session);
the launcher itself remains invoked as the checked-in shell script.

## Setup boundary

The setup is complete only when the runner's preflight confirms all of the following:

1. the running provider reports version `1.13.3` plus a valid build revision and
   timestamp through its version API, and fresh Docker inspection shows the exact
   four pinned image references rather than configured constants alone;
2. OpenMetadata reports terminal health from `tests/emulators/openmetadata/wait_ready.py`;
3. the catalog binding is tenant-scoped and reaches `ready` only after positive and
   denial validation;
4. the private ledger contains exact planned resources before provider effects;
5. the successful and Refund NVP tenant identifiers are newly generated for this run;
6. the state, evidence, backup, secret, and private-ledger paths are outside the
   repository and contain no symlink component; and
7. no pre-existing containers, volumes, networks, catalog objects, or evidence are
   being treated as run output.

Do not proceed if a preflight check reports an existing resource, an unknown image
reference, a missing secret name, a non-loopback port, a missing Task 9 runner, or a
non-empty target path. Preserve the private ledger and follow
[`teardown.md`](teardown.md) for exact cleanup; never repair setup by deleting all
Docker resources.
