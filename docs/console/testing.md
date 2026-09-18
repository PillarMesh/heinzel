# Local UI testing

This setup runs the built product UI against the owning services and disposable local
SQLite stores. It uses the local-acceptance warehouse harness and makes no external
provider calls. It proves local product behavior, not production readiness or live
warehouse effects. Each page identifies the environment as a governed local workspace.

## Start

From the repository root, prepare the locked Python environment and build the console
with Node 24.20.0:

```sh
uv sync --locked --all-packages
cd apps/console
npm ci
npm run build
cd ../..
```

Choose a new private directory for this test run. Start the architect first, then the
requester and approval roles in separate terminals, using the same directory:

```sh
uv run python -m tests.acceptance.run_console_governed \
  --directory /private/tmp/heinzel-ui-test-session \
  --port 8130 --actor architect-a --no-seed
```

```sh
uv run python -m tests.acceptance.run_console_governed \
  --directory /private/tmp/heinzel-ui-test-session \
  --port 8131 --actor requester-a --no-seed
```

```sh
uv run python -m tests.acceptance.run_console_governed \
  --directory /private/tmp/heinzel-ui-test-session \
  --port 8132 --actor data-owner-a --no-seed
```

```sh
uv run python -m tests.acceptance.run_console_governed \
  --directory /private/tmp/heinzel-ui-test-session \
  --port 8133 --actor policy-approver-a --no-seed
```

- Requester: <http://127.0.0.1:8131/requests>
- Architect: <http://127.0.0.1:8130/inbox>
- Architect setup: <http://127.0.0.1:8130/setup>
- Finance data owner: <http://127.0.0.1:8132/inbox>
- Policy approver: <http://127.0.0.1:8133/inbox>

These are fixed local sessions, not production authentication. The selected actor cannot
be changed by request headers. The harness refuses non-loopback binding. Do not expose
these servers through a tunnel or public interface. All sessions read and write the
same durable state. Restarting preserves it; choose a fresh directory to repeat a fresh
approval scenario. Stop each server with Ctrl-C when finished.

## Test the supported journey

1. As the requester, submit a stakeholder question with title “Net revenue definition”,
   purpose `semantic definition`, and question “What does net revenue mean?”. Open it,
   send a reply, reload, and verify the message and title remain. The local candidate
   provider is a fixed scenario for this definition; it does not answer arbitrary questions.
2. As the architect, open that new request and inspect its original question and reply.
   Record a clarification with the intended outcome, in-scope summary, and out-of-scope
   summary. Choose **Prepare answer proposal**, review the owning candidate and exact
   dataset/metric/lineage references, then **Submit proposal for approval**.
3. Reload the requester page and accept the clarified scope. Reload the architect page
   and verify the recorded requester approval. The requester must never see the
   unapproved candidate. No seeded request is needed for this journey.
4. Confirm the reviewed digest and approve. Then separately admit to execution. Verify
   the request displays **Delivered answer** (owning state `delivered`) with its catalog,
   semantic, contract, product, and warehouse references. Avoid posting a new conversation message between approval and
   admission: messages advance the request revision and invalidate approvals, and the
   UI must withdraw admission.
5. Reload both sessions to check persistence. Try narrow and medium browser widths and
   keyboard-only navigation. An unavailable capability must explain its status rather
   than pretend to complete an effect.

For data access, submit `product-revenue` with the published `net-revenue` field and a
future expiry. The architect records clarification, prepares the access proposal, and
submits it. The requester accepts the clarified scope; the finance data owner and policy
approver each review the proposal in their own session and approve it. The architect then
admits it. The requester must see **Access is ready**, only the approved fields and
permissions, and the automatic expiry. Grant IDs and provider-effect references must not
appear. If current authority changes, admission supersedes the proposal and every role must
review the replacement revision.

## Limits to record

- Data access is composed against strict local result and warehouse surfaces. These are
  acceptance providers; they do not prove a live PostgreSQL, ClickHouse, or Superset grant.
- Missing governed data produces an explicit dependency. No Valid Plan includes the
  owning refusal reasons and required changes; neither outcome invents an answer.
- Source acquisition needs a composed runtime and approved live binding.
- Process document upload, analyst dashboards, catalog asset previews, and operation
  retry remain unavailable in governed mode.
- Manual revocation controls and intermediate provider-cleanup state remain outside the
  browser journey. Access-control still denies expired or revoked authority immediately.
- Interactive transactions use wall-clock UTC. Semantic facts come from the selected
  approved publication; the default publication is local test data unless
  `--publication-database` selects a live publication store.

## Test an unsupported local question

The local harness resolves a stakeholder question when it names exactly one term in the
workspace's current approved semantic publication. A question with no matching term, or
an ambiguous match, terminates in the owning `No Valid Plan` state. The requester sees
the original question and the safe explanation, while the architect sees the internal
reason and smallest required change.

Use a fresh state directory and the requester and architect commands above. Submit title
`Test`, purpose `This is a test request`, and question `What is the current MRR` at
<http://127.0.0.1:8131/requests>. At <http://127.0.0.1:8130/inbox>, record clarification
and select **Prepare answer proposal**. Confirm the architect sees
`published_semantic_term_not_found`, then return to the requester detail and use **Start revised
request**. Opening that form must not create anything. Edit the question and submit it;
the UI confirms `Revised request submitted` and offers a labeled **View request** link. The
confirmation intentionally omits the internal request identifier, revision, and owning state.
Automated coverage compares the two link destinations for distinct identities and verifies
revision 1 at the owning repository boundary.

The acceptance test also queries the owning request repository after the action. It requires
state `NO_VALID_PLAN`, a resulting revision of at least 2, zero proposals, and zero calls to
the answer candidate provider. Browser status text alone does not prove the terminal state.
Run both boundaries with:

```sh
uv run pytest services/runtime/tests/test_acquisition.py \
  tests/acceptance/test_run_console_governed.py -q
cd apps/console
npm run test:e2e:governed
```

See [known gaps](known-gaps.md) and [HTTP acceptance](acceptance-run.md) for the owning
boundaries and the database verification procedure.
