# Plan 3B offline acceptance setup

Plan 3B proves the governed architect-inbox control plane. Its acceptance harness uses new
in-memory SQLite repositories, the committed revenue-to-cash Plan 2 publication fixture, the real
Plan 3B policy compiler, projections, and evidence packager. It does not connect to OpenMetadata,
a warehouse, a source, an identity provider, or a messaging system.

Run from the repository root with Python 3.13 and the locked `uv` workspace:

```sh
repository_root=$(git rev-parse --show-toplevel)
cd "$repository_root"
test -f tests/acceptance/run_request_fulfillment.py
test -f tests/end-to-end/test_architect_inbox_fulfillment.py
test -f tests/fault-injection/test_architect_inbox_fulfillment.py
uv sync --locked --all-packages
uv lock --check
```

No credentials, endpoints, external services, environment files, or mutable fixture directories
are required. Do not inject production credentials into this run. The committed Plan 2 fixture is
read through its real strict models; it is not evidence that a live catalog or warehouse exists.

Setup is complete when the locked environment resolves and the three Plan 3B acceptance paths
above exist. A modified dependency lock, missing fixture, or unavailable Python version is a setup
failure and must not be bypassed with an unlocked environment.
