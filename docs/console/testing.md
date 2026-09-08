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
requester in another terminal, using the same directory:

```sh
uv run python -m tests.acceptance.run_console_governed \
  --directory /private/tmp/pillarmesh-ui-test-session \
  --port 8130 --actor architect-a --no-seed
```

```sh
uv run python -m tests.acceptance.run_console_governed \
  --directory /private/tmp/pillarmesh-ui-test-session \
  --port 8131 --actor requester-a --no-seed
```

- Requester: <http://127.0.0.1:8131/requests>
- Architect: <http://127.0.0.1:8130/inbox>
- Architect setup: <http://127.0.0.1:8130/setup>

These are fixed local sessions, not production authentication. The selected actor cannot
be changed by request headers. The harness refuses non-loopback binding. Do not expose
these servers through a tunnel or public interface. Both sessions read and write the
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
   the request displays **execution ready** (owning state `executing`); admission is not
   answer delivery. Avoid posting a new conversation message between approval and
   admission: messages advance the request revision and invalidate approvals, and the
   UI must withdraw admission.
5. Reload both sessions to check persistence. Try narrow and medium browser widths and
   keyboard-only navigation. An unavailable capability must explain its status rather
   than pretend to complete an effect.

## Limits to record

- Access request intake is supported, but access proposal preparation is not composed
  in this harness. Its buttons remain unavailable.
- Missing governed data produces an explicit dependency. No Valid Plan includes the
  owning refusal reasons and required changes; neither outcome invents an answer.
- Source acquisition needs a composed runtime and approved live binding.
- Process document upload, analyst dashboards, catalog asset previews, and operation
  retry remain unavailable in governed mode.
- Answer delivery, grant application/expiry/revocation, and downstream transformation
  effects are not proved by admission or by this harness.
- The scenario clock is fixed so its semantic observations remain reproducible; displayed
  timestamps are scenario time. Semantic facts are local test data; the browser must
  perform new writes to demonstrate that the supported command path works.

See [known gaps](known-gaps.md) and [HTTP acceptance](acceptance-run.md) for the owning
boundaries and the database verification procedure.
