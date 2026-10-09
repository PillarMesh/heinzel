import {useEffect, useState} from "react"

import {ArtifactDigest} from "./artifact-reference"
import {Nothing, Panel} from "../../components/panel"
import {asPageFailure} from "../../format/failure"
import {formatInstant} from "../../format/instant"
import type {PageFailureState} from "../../format/failure"
import type {
  ConsoleEnvelopeProvenanceView,
  DataProvenance,
  ProvenanceExecutionView,
  ProvenanceLandingView,
  ProvenanceMaterializationView,
  ProvenanceProductView,
  ProvenanceQueryView,
  ProvenanceSourceView,
  ProvenanceView,
  ProvenanceWarehouseView,
} from "../../api/generated"

/**
 * How this answer was produced, from the source system to the rows.
 *
 * Every other surface in the console shows what was decided. None of them showed how the thing
 * being decided about came to exist -- which warehouse it lives in, what shape the source was
 * read in, what statement built the product, what statement answered the question. That chain
 * is recorded in full by the services that did the work, and a reviewer approving an answer has
 * a fair claim on seeing it.
 *
 * Nothing here is narrated. Each stage is fields off one receipt, and a stage whose step has
 * not happened for this request says so rather than describing what it would have contained.
 */
export interface ProvenanceClient {
  getRequestProvenance(requestId: string): Promise<ConsoleEnvelopeProvenanceView>
}

interface ProvenancePaneProps {
  readonly client: ProvenanceClient
  /** The provenance the console is serving. A chain composed under another is not this one's. */
  readonly dataProvenance: DataProvenance
  readonly requestId: string
}

/**
 * One state, not three. The pane is keyed on the request, so it is remounted rather than reset,
 * and the effect never writes state synchronously on the way in.
 */
type PaneState =
  | {readonly kind: "loading"}
  | {readonly kind: "failed"; readonly failure: PageFailureState}
  | {readonly kind: "ready"; readonly chain: ProvenanceView}

export function ProvenancePane({client, dataProvenance, requestId}: ProvenancePaneProps) {
  const [state, setState] = useState<PaneState>({kind: "loading"})

  useEffect(() => {
    let current = true
    void client
      .getRequestProvenance(requestId)
      .then((envelope) => {
        if (!current) {
          return
        }
        if (
          envelope.meta.data_provenance !== dataProvenance ||
          envelope.data.request_id !== requestId
        ) {
          setState({
            kind: "failed",
            failure: {
              code: "provenance_mismatch",
              message: "The production chain the console returned is not this request's.",
            },
          })
          return
        }
        setState({kind: "ready", chain: envelope.data})
      })
      .catch((error: unknown) => {
        if (current) {
          setState({
            kind: "failed",
            failure: asPageFailure(error, "The production chain could not be read."),
          })
        }
      })
    return () => {
      current = false
    }
  }, [client, dataProvenance, requestId])

  if (state.kind === "failed") {
    return (
      <Panel title="How this answer was produced">
        {state.failure.code === "capability_not_delivered" ? (
          <Nothing>{state.failure.message}</Nothing>
        ) : (
          <p role="alert">{state.failure.message}</p>
        )}
      </Panel>
    )
  }
  if (state.kind === "loading") {
    return (
      <Panel title="How this answer was produced">
        <Nothing>Reading the production chain.</Nothing>
      </Panel>
    )
  }
  const chain = state.chain
  return (
    <div className="panel-stack provenance">
      <Warehouse warehouse={chain.warehouse} />
      <Source source={chain.source} />
      <Landing landing={chain.landing} />
      <Product product={chain.product} />
      <Materialization materialization={chain.materialization} />
      <Query query={chain.query} />
      <Execution execution={chain.execution} />
    </div>
  )
}

/** The step's place in the chain, carried on the panel header rather than in its title. */
function Step({ordinal}: {readonly ordinal: number}) {
  return <span className="provenance__step">Step {ordinal}</span>
}

function Warehouse({warehouse}: {readonly warehouse: ProvenanceWarehouseView | null}) {
  return (
    <Panel
      aside={<Step ordinal={1} />}
      description="The warehouse this tenant's data lives in, provisioned and recorded by warehouse control."
      title="Warehouse"
    >
      {warehouse === null ? (
        <Nothing>
          This deployment answers over a database no governing service provisioned, so there is no
          binding to show.
        </Nothing>
      ) : (
        <dl className="record">
          <dt>Binding</dt>
          <dd>
            <code>{warehouse.binding_ref}</code>
          </dd>
          <dt>Engine</dt>
          <dd>{warehouse.engine_kind}</dd>
          <dt>Deployment</dt>
          <dd>
            {warehouse.deployment_mode.replaceAll("_", " ")} &middot; {warehouse.region}
          </dd>
          <dt>State</dt>
          <dd>{warehouse.lifecycle_state}</dd>
          {warehouse.provisioned_at === null ? null : (
            <>
              <dt>Provisioned</dt>
              <dd>{formatInstant(warehouse.provisioned_at)}</dd>
            </>
          )}
          <dt>Capabilities</dt>
          <dd>
            <ArtifactDigest digest={warehouse.capability_profile_digest} label="Profile" />
          </dd>
        </dl>
      )}
    </Panel>
  )
}

function Source({source}: {readonly source: ProvenanceSourceView | null}) {
  return (
    <Panel
      aside={<Step ordinal={2} />}
      description="The object in the source system, in the shape the tenant's acquisition contract agreed to read it."
      title="Source schema"
    >
      {source === null ? (
        <Nothing>No acquisition contract has been activated for this tenant.</Nothing>
      ) : (
        <>
          <dl className="record">
            <dt>Source</dt>
            <dd>
              <code>{source.source_binding_ref}</code>
            </dd>
            <dt>Object</dt>
            <dd>
              <code>{source.logical_object_ref}</code>
            </dd>
            <dt className="record__wide">Read as</dt>
            <dd className="record__wide">
              {source.acquisition_modes.join(", ")} &middot;{" "}
              {source.operation_semantics.replaceAll("_", " ")}
            </dd>
            <dt>Record key</dt>
            <dd>
              {source.record_key_fields.length === 0
                ? "None declared"
                : source.record_key_fields.join(", ")}
            </dd>
            {source.source_updated_at_field === null ? null : (
              <>
                <dt>Change marker</dt>
                <dd>
                  <code>{source.source_updated_at_field}</code>
                </dd>
              </>
            )}
            {source.validated_at === null ? null : (
              <>
                <dt>Validated</dt>
                <dd>{formatInstant(source.validated_at)}</dd>
              </>
            )}
            <dt>Shape</dt>
            <dd>
              <ArtifactDigest digest={source.schema_digest} label="Schema" />
            </dd>
          </dl>
          <div className="provenance-fields-frame">
            <table className="provenance-fields">
              <caption>
                {source.fields.length} {source.fields.length === 1 ? "field" : "fields"} this
                contract admits
              </caption>
              <thead>
                <tr>
                  <th scope="col">Field</th>
                  <th scope="col">Type</th>
                  <th scope="col">Required</th>
                </tr>
              </thead>
              <tbody>
                {source.fields.map((field) => (
                  <tr key={field.name}>
                    <th scope="row">
                      <code>{field.name}</code>
                    </th>
                    <td>{field.value_type}</td>
                    <td>{field.nullable ? "Optional" : "Required"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </Panel>
  )
}

function Landing({landing}: {readonly landing: ProvenanceLandingView | null}) {
  return (
    <Panel
      aside={<Step ordinal={3} />}
      description="The acquisition run that copied records into the warehouse, and what it attested about them."
      title="Landing run"
    >
      {landing === null ? (
        <Nothing>Nothing has been landed for this tenant yet.</Nothing>
      ) : (
        <dl className="record">
          <dt>Landed into</dt>
          <dd>
            <code>{landing.target_table_ref}</code>
          </dd>
          <dt>Records</dt>
          <dd>{landing.record_count.toLocaleString()}</dd>
          <dt>Window</dt>
          <dd>{landing.trigger_window.replaceAll("_", " ")}</dd>
          <dt>Committed</dt>
          <dd>{formatInstant(landing.committed_at)}</dd>
          <dt className="record__wide">Attested</dt>
          <dd className="record__wide">
            <ArtifactDigest digest={landing.schema_digest} label="Schema" />
            <ArtifactDigest digest={landing.segment_digest} label="Records" />
          </dd>
        </dl>
      )}
    </Panel>
  )
}

function Product({product}: {readonly product: ProvenanceProductView | null}) {
  return (
    <Panel
      aside={<Step ordinal={4} />}
      description="The statement the compiler emitted to build the data product out of the landed records, as it signed it."
      title="Transform"
    >
      {product === null ? (
        <Nothing>No signed transform has been issued for this product generation.</Nothing>
      ) : (
        <>
          <dl className="record">
            <dt>Product</dt>
            <dd>
              <code>{product.product_id}</code> revision {product.product_revision}, generation{" "}
              {product.generation}
            </dd>
            <dt className="record__wide">Built as</dt>
            <dd className="record__wide">
              <code>
                {product.target_schema}.{product.model_name}
              </code>
            </dd>
            <dt>Columns</dt>
            <dd>{product.output_columns.join(", ")}</dd>
            <dt className="record__wide">Guarantees</dt>
            <dd className="record__wide">
              {product.quality_tests.length === 0 ? (
                "No quality tests declared"
              ) : (
                <ul className="provenance-checks">
                  {product.quality_tests.map((test) => (
                    <li key={`${test.column_name}:${test.kind}`}>
                      <code>{test.column_name}</code> {test.kind.replaceAll("_", " ")}
                    </li>
                  ))}
                  {product.magnitude_checks.map((check) => (
                    <li key={`magnitude:${check.column_name}`}>
                      <code>{check.column_name}</code> fits {check.precision} digits with{" "}
                      {check.scale} decimal places
                    </li>
                  ))}
                </ul>
              )}
            </dd>
            <dt>Signed</dt>
            <dd>
              <ArtifactDigest digest={product.model_digest} label="Transform" />
            </dd>
          </dl>
          <Statement label="Show the compiled transform" statement={product.compiled_sql} />
        </>
      )}
    </Panel>
  )
}

function Materialization({
  materialization,
}: {
  readonly materialization: ProvenanceMaterializationView | null
}) {
  return (
    <Panel
      aside={<Step ordinal={5} />}
      description="The receipt the transform provider returned for the run that built the product."
      title="Build"
    >
      {materialization === null ? (
        <Nothing>This product generation has not been built.</Nothing>
      ) : (
        <dl className="record">
          <dt>Rows built</dt>
          <dd>{materialization.output_row_count.toLocaleString()}</dd>
          <dt>Quality</dt>
          <dd>
            {materialization.quality_assertion_count.toLocaleString()}{" "}
            {materialization.quality_assertion_count === 1 ? "assertion" : "assertions"}{" "}
            {materialization.quality_disposition}
          </dd>
          <dt>Committed</dt>
          <dd>{formatInstant(materialization.committed_at)}</dd>
          <dt className="record__wide">Attested</dt>
          <dd className="record__wide">
            <ArtifactDigest digest={materialization.lineage_digest} label="Lineage" />
            <ArtifactDigest digest={materialization.dbt_manifest_digest} label="Manifest" />
            <ArtifactDigest digest={materialization.dbt_run_results_digest} label="Run results" />
          </dd>
        </dl>
      )}
    </Panel>
  )
}

function Query({query}: {readonly query: ProvenanceQueryView | null}) {
  return (
    <Panel
      aside={<Step ordinal={6} />}
      description="The statement the compiler emitted to answer this question, and the limits it was admitted under."
      title="Query"
    >
      {query === null ? (
        <Nothing>No query has been compiled for this request.</Nothing>
      ) : (
        <>
          <Statement label="Show the compiled query" open statement={query.statement} />
          <dl className="record">
            <dt className="record__wide">Compiled by</dt>
            <dd className="record__wide">
              Compiler version {query.compiler_version}, for {query.engine_kind}, against
              allowlist {query.allowlist_version}
            </dd>
            {query.parameters.length === 0 ? null : (
              <>
                <dt>Parameters</dt>
                <dd>
                  {query.parameters
                    .map((parameter) => `${parameter.name} (${parameter.value_type})`)
                    .join(", ")}
                </dd>
              </>
            )}
            <dt className="record__wide">Limits</dt>
            <dd className="record__wide">
              At most {query.row_limit.toLocaleString()} rows returned, from a scan of at most{" "}
              {query.scan_row_ceiling.toLocaleString()} rows and{" "}
              {query.scan_byte_ceiling.toLocaleString()} bytes.
              {/*
                A floor of one suppresses nothing, so saying so would read as a disclosure
                control where there is none. It is stated only where it actually binds.
              */}
              {query.minimum_group_size <= 1
                ? ""
                : ` No group smaller than ${query.minimum_group_size.toLocaleString()} is disclosed.`}
            </dd>
            {query.estimated_rows === null ? null : (
              <>
                <dt>Estimated</dt>
                <dd>
                  {query.estimated_rows.toLocaleString()} rows
                  {query.estimated_bytes === null
                    ? ""
                    : `, ${query.estimated_bytes.toLocaleString()} bytes`}
                </dd>
              </>
            )}
            <dt>Admitted</dt>
            <dd>
              {query.routing.replaceAll("_", " ")}, signed by <code>{query.signing_key_id}</code>
            </dd>
            <dt className="record__wide">Attested</dt>
            <dd className="record__wide">
              <ArtifactDigest digest={query.plan_digest} label="Plan" />
              <ArtifactDigest digest={query.statement_digest} label="Statement" />
            </dd>
          </dl>
        </>
      )}
    </Panel>
  )
}

function Execution({execution}: {readonly execution: ProvenanceExecutionView | null}) {
  return (
    <Panel
      aside={<Step ordinal={7} />}
      description="The runtime's receipt for the run that produced the rows this answer is drawn from."
      title="Execution"
    >
      {execution === null ? (
        <Nothing>This request has no completed execution.</Nothing>
      ) : (
        <dl className="record">
          <dt className="record__wide">Receipt</dt>
          <dd className="record__wide">
            <code>{execution.execution_receipt_id}</code>
          </dd>
          <dt>Rows returned</dt>
          <dd>{execution.row_count.toLocaleString()}</dd>
          <dt className="record__wide">Attested</dt>
          <dd className="record__wide">
            <ArtifactDigest digest={execution.result_digest} label="Rows" />
            <ArtifactDigest digest={execution.result_schema_digest} label="Columns" />
          </dd>
        </dl>
      )}
    </Panel>
  )
}

/**
 * A generated statement, wrapped rather than scrolled.
 *
 * These come out of the compiler as one line -- the product's is over a thousand characters --
 * so a `pre` that honoured the newlines would be a single horizontal scrollbar with nothing
 * readable in it.
 */
function Statement({
  label,
  open = false,
  statement,
}: {
  readonly label: string
  readonly open?: boolean
  readonly statement: string
}) {
  return (
    <details className="provenance-statement" open={open}>
      <summary>{label}</summary>
      <pre>
        <code>{statement}</code>
      </pre>
    </details>
  )
}
