import {render, screen, waitFor, within} from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import {vi} from "vitest"

import {ConsoleApiError} from "../../api/client"
import {ProvenancePane} from "./provenance-pane"
import type {ConsoleEnvelopeProvenanceView} from "../../api/generated"

const envelope: ConsoleEnvelopeProvenanceView = {
  meta: {correlation_id: "correlation-provenance", data_provenance: "governed_local"},
  data: {
    request_id: "request-answer",
    warehouse: {
      binding_ref: "warehouse-demo",
      engine_kind: "postgresql",
      deployment_mode: "heinzel_cloud",
      region: "local",
      lifecycle_state: "ready",
      capability_profile_digest: "a".repeat(64),
      provisioned_at: "2026-09-12T09:00:00Z",
    },
    source: {
      source_binding_ref: "source-demo-orders",
      logical_object_ref: "customer_orders",
      source_observation_ref: "source-observation-demo-orders",
      capability_profile_digest: "b".repeat(64),
      acquisition_modes: ["snapshot", "incremental"],
      operation_semantics: "upsert_only",
      record_key_fields: ["order_id"],
      source_updated_at_field: "updated_at",
      schema_digest: "c".repeat(64),
      fields: [
        {name: "order_id", value_type: "integer", nullable: false},
        {name: "order_total", value_type: "decimal", nullable: true},
      ],
      validated_at: "2026-09-12T09:05:00Z",
    },
    landing: {
      target_table_ref: "raw_customer_orders",
      trigger_window: "manual_window",
      record_count: 5,
      schema_digest: "d".repeat(64),
      segment_digest: "e".repeat(64),
      committed_at: "2026-09-12T09:10:00Z",
    },
    product: {
      product_id: "orders_daily",
      product_revision: 1,
      generation: 1,
      model_name: "orders_daily_g1",
      target_schema: "contract_fe27",
      output_columns: ["ordered_on", "total_order_value"],
      quality_tests: [{column_name: "ordered_on", kind: "not_null"}],
      magnitude_checks: [{column_name: "total_order_value", precision: 57, scale: 9}],
      compiled_sql: 'SELECT "ordered_on" FROM "raw"."raw_customer_orders"',
      model_digest: "f".repeat(64),
    },
    materialization: {
      output_row_count: 3,
      quality_assertion_count: 3,
      quality_disposition: "passed",
      lineage_digest: "0".repeat(64),
      dbt_manifest_digest: "1".repeat(64),
      dbt_run_results_digest: "2".repeat(64),
      committed_at: "2026-09-12T09:20:00Z",
    },
    query: {
      engine_kind: "postgresql",
      compiler_version: "compiler-1",
      allowlist_version: "allowlist-1",
      statement: 'SELECT "ordered_on" FROM "contract_fe27"."orders_daily_g1"',
      parameters: [{name: "minimum_group_size", value_type: "integer"}],
      minimum_group_size: 1,
      row_limit: 100,
      scan_row_ceiling: 1000000,
      scan_byte_ceiling: 102400,
      estimated_rows: 682,
      estimated_bytes: 16384,
      routing: "policy_admitted",
      plan_digest: "3".repeat(64),
      statement_digest: "4".repeat(64),
      signing_key_id: "compiler-demo-query-1",
    },
    execution: {
      execution_receipt_id: "execution-1",
      result_digest: "5".repeat(64),
      result_schema_digest: "6".repeat(64),
      row_count: 3,
    },
  },
}

function renderPane(response: ConsoleEnvelopeProvenanceView | Error) {
  render(
    <ProvenancePane
      client={{
        getRequestProvenance: vi.fn(async () => {
          if (response instanceof Error) {
            throw response
          }
          return response
        }),
      }}
      dataProvenance="governed_local"
      requestId="request-answer"
    />,
  )
}

test("shows every recorded stage from the warehouse to the rows", async () => {
  renderPane(envelope)

  const warehouse = await screen.findByRole("region", {name: "Warehouse"})
  expect(within(warehouse).getByText("warehouse-demo")).toBeVisible()
  expect(within(warehouse).getByText("postgresql")).toBeVisible()
  expect(within(warehouse).getByText("heinzel cloud · local")).toBeVisible()

  // The schema the contract agreed to read, field by field -- the step a reader most often
  // asks about and the one no other surface in the console shows.
  const source = screen.getByRole("region", {name: "Source schema"})
  const fields = within(source).getByRole("table")
  expect(within(fields).getByRole("rowheader", {name: "order_id"})).toBeVisible()
  expect(within(fields).getAllByRole("row")).toHaveLength(3)
  expect(within(source).getByText("snapshot, incremental · upsert only")).toBeVisible()

  const landing = screen.getByRole("region", {name: "Landing run"})
  expect(within(landing).getByText("raw_customer_orders")).toBeVisible()
  expect(within(landing).getByText("5")).toBeVisible()

  const transform = screen.getByRole("region", {name: "Transform"})
  expect(within(transform).getByText("contract_fe27.orders_daily_g1")).toBeVisible()
  expect(
    within(transform).getByText("fits 57 digits with 9 decimal places", {exact: false}),
  ).toBeVisible()

  const build = screen.getByRole("region", {name: "Build"})
  expect(within(build).getByText("3 assertions passed")).toBeVisible()

  // The generated statement is in the page, folded like the transform's: both are a screenful
  // of SQL, and the stage reads as a record of limits and attestations until one is asked for.
  const query = screen.getByRole("region", {name: "Query"})
  expect(
    within(query).getByText('SELECT "ordered_on" FROM "contract_fe27"."orders_daily_g1"'),
  ).not.toBeVisible()
  expect(within(query).getByText("At most 100 rows returned", {exact: false})).toBeVisible()
  expect(within(query).getByText("policy admitted", {exact: false})).toBeVisible()

  const execution = screen.getByRole("region", {name: "Execution"})
  expect(within(execution).getByText("execution-1")).toBeVisible()
})

test("a digest stays folded away until it is asked for", async () => {
  renderPane(envelope)

  const execution = await screen.findByRole("region", {name: "Execution"})
  const rows = within(execution).getByText("Rows", {selector: "summary"})
  expect(rows).toBeVisible()
  // The whole digest is in the page for anyone who opens it, and not on the reading line.
  expect(within(execution).getByText("5".repeat(64))).not.toBeVisible()
})

test("the chain is drawn as a chain, and every node reaches its own receipt", async () => {
  renderPane(envelope)

  const flow = await screen.findByRole("navigation", {name: "Production chain"})
  const nodes = within(flow).getAllByRole("link")
  expect(nodes.map((node) => node.textContent)).toEqual([
    "Warehouse, recorded",
    "Source schema, recorded",
    "Landing run, recorded",
    "Transform, recorded",
    "Build, recorded",
    "Query, recorded",
    "Execution, recorded",
  ])
  // A node that points at nothing is a dead link in the middle of the page, and a renamed
  // stage would make one silently -- so each anchor is checked against the panel it names.
  for (const node of nodes) {
    const target = node.getAttribute("href")?.slice(1) ?? ""
    expect(document.getElementById(target)).not.toBeNull()
  }
  expect(
    within(flow).getByText("7 of 7 steps recorded for this request"),
  ).toBeVisible()
})

test("a step that has not run is drawn as not run, not left off the chain", async () => {
  renderPane({...envelope, data: {...envelope.data, query: null, execution: null}})

  const flow = await screen.findByRole("navigation", {name: "Production chain"})
  expect(within(flow).getAllByRole("link")).toHaveLength(7)
  expect(within(flow).getByText("5 of 7 steps recorded for this request")).toBeVisible()
  expect(
    within(flow).getByRole("link", {name: "Query, not yet recorded"}),
  ).toBeVisible()
})

test("a compiled statement reads once it is asked for", async () => {
  const user = userEvent.setup()
  renderPane(envelope)

  const query = await screen.findByRole("region", {name: "Query"})
  await user.click(within(query).getByText("Show the compiled query", {selector: "summary"}))
  expect(
    within(query).getByText('SELECT "ordered_on" FROM "contract_fe27"."orders_daily_g1"'),
  ).toBeVisible()
})

test("a step that has not happened says so rather than describing it", async () => {
  renderPane({
    ...envelope,
    data: {...envelope.data, query: null, execution: null},
  })

  const query = await screen.findByRole("region", {name: "Query"})
  expect(within(query).getByText("No query has been compiled for this request.")).toBeVisible()
  expect(within(query).queryByText("At most 100 rows returned", {exact: false})).toBeNull()
  const execution = screen.getByRole("region", {name: "Execution"})
  expect(within(execution).getByText("This request has no completed execution.")).toBeVisible()
})

test("a deployment that does not run the read says so without raising an alert", async () => {
  renderPane(
    new ConsoleApiError(503, {
      meta: {correlation_id: "correlation-absent", data_provenance: "governed_local"},
      error: {
        code: "capability_not_delivered",
        safe_message: "This deployment does not provide a governed provenance read interface.",
        recovery_action: "none",
      },
    }),
  )

  expect(
    await screen.findByText(
      "This deployment does not provide a governed provenance read interface.",
    ),
  ).toBeVisible()
  expect(screen.queryByRole("alert")).toBeNull()
  expect(screen.queryByRole("region", {name: "Warehouse"})).toBeNull()
})

test.each([
  ["another request", {...envelope, data: {...envelope.data, request_id: "request-other"}}],
  [
    "another provenance",
    {...envelope, meta: {...envelope.meta, data_provenance: "demo_fixture" as const}},
  ],
])("refuses a chain composed for %s", async (_case, response) => {
  renderPane(response)

  await waitFor(() =>
    expect(
      screen.getByText("The production chain the console returned is not this request's."),
    ).toBeVisible(),
  )
  expect(screen.queryByText("warehouse-demo")).toBeNull()
  expect(screen.queryByRole("table")).toBeNull()
})
