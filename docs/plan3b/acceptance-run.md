# Run Plan 3B offline acceptance

The Plan 3B acceptance command witnesses a fresh, deterministic control-plane journey for a data
engineering architect. From the repository root, run:

```sh
uv run python -m tests.acceptance.run_plan3b
```

Success prints one JSON object only:

```json
{"schema_version":"1","status":"complete","evidence_digest":"<64 lowercase hex characters>"}
```

The command creates four new in-memory requests and proves:

1. an authorized semantic-definition question binds an accepted clarified outcome and reaches an
   execution-ready answer handoff;
2. a factual answer without governed data creates one blocking data-product dependency;
3. an unauthorized factual question reaches an approved requester-safe denial without exposing
   candidate content;
4. a finance access request is narrowed to the allowed field and binds distinct data-owner and
   policy-authority approvals before reaching an execution-ready handoff;
5. Plan 2 decisions cannot satisfy Plan 3B requirements, requester views omit candidates, and
   cross-tenant and unrelated actors receive generic denial projections; and
6. no SQL, provider call, credential, grant, or external message occurs.

Run the integrated and fault tests as the focused acceptance gate:

```sh
uv run pytest \
  tests/end-to-end/test_architect_inbox_fulfillment.py \
  tests/fault-injection/test_architect_inbox_fulfillment.py \
  tests/acceptance/test_run_plan3b.py -q
```

The fault test interrupts every admission write boundary, verifies no partial request revision,
admission, or evidence remains, removes only the injected trigger, and retries to one terminal
outcome. It also proves cancellation wins a stale admission race. Component suites additionally
cover proposal, dependency, `No Valid Plan`, approval, denial, policy supersession, role
revocation, malformed persistence, and replay boundaries.

Before completion, run the full offline repository gates:

```sh
uv sync --locked --all-packages
uv lock --check
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest -m "not live"
./tests/repository-structure/test.sh
```

This is a control-plane readiness claim only. `ready_for_execution` means exact immutable inputs
and current role-scoped approvals were admitted. It does not prove a query ran, a grant was
applied, data is fresh, a stakeholder received an answer, or access was verified, expired, or
revoked. Those are later data-plane and delivery stages.

If the process is interrupted, its in-memory databases disappear with the process. Start a new
command and require the complete terminal JSON result; partial output or an earlier digest is not
successful evidence.
