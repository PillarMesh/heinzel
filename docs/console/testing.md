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
  --port 8130 --actor architect-a
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

1. As the requester, submit a stakeholder question with a title, purpose, and question.
   Open it, send a reply, reload, and verify the message and title remain. Submit an
   access request to exercise fields, expiry, and scope intake as well.
2. As the architect, open that new request in the inbox and verify its exact title and
   requester reply. Reply as the architect; reload the requester page to see its role.
   Newly submitted requests do not compile themselves: the interactive clarification
   authoring and proposal-generation commands are not delivered yet.
3. Use the separately seeded “What does net revenue mean?” request for review. As the
   requester, inspect and accept the clarified scope. As the architect, inspect the
   answer candidate, exact dataset/metric/lineage references, and recorded approvals.
   The requester must never see the unapproved candidate.
4. Confirm the reviewed digest and approve. Then separately admit to execution. Verify
   the request reaches `executing`; admission is not answer delivery. Avoid posting a
   new conversation message between approval and admission: messages advance the request
   revision and invalidate approvals, and the UI must withdraw admission.
5. Reload both sessions to check persistence. Try narrow and medium browser widths and
   keyboard-only navigation. An unavailable capability must explain its status rather
   than pretend to complete an effect.

## Limits to record

- Source acquisition needs a composed runtime and approved live binding.
- Process document upload, analyst dashboards, catalog asset previews, and operation
  retry remain unavailable in governed mode.
- Answer delivery, grant application/expiry/revocation, and downstream transformation
  effects are not proved by admission or by this harness.
- The scenario clock is fixed so its semantic observations remain reproducible; displayed
  timestamps are scenario time. The seed and semantic facts are local test data; the browser must perform new writes
  to demonstrate that the supported command path works.

See [known gaps](known-gaps.md) and [HTTP acceptance](acceptance-run.md) for the owning
boundaries and the database verification procedure.
