import type {SetupView} from "../../api/generated"
import {CapabilityState} from "../../components/capability-state"
import {Nothing, Panel} from "../../components/panel"
import {StatusPill} from "../../components/status-pill"

interface ServicesStageProps {
  readonly setup: SetupView
}

export function ServicesStage({setup}: ServicesStageProps) {
  const services = setup.managed_services ?? []
  const binding = setup.warehouse_binding
  return (
    <section aria-labelledby="services-title" className="setup-stage">
      <p className="eyebrow">Stage 2 · Managed services</p>
      <h1 id="services-title">Managed services</h1>
      <p className="setup-stage__lead">
        Validate each managed dependency without exposing provider administration or private
        infrastructure details.
      </p>

      {/*
        The data plane this workspace runs on, which the page already knew and did not show.
        In governed-local mode the service ledger below is empty, so the stage was a heading,
        one sentence and nine hundred pixels of nothing -- on the stage the workspace is
        actually standing on.
      */}
      {binding === null || binding === undefined ? null : (
        <Panel
          aside={<StatusPill tone={binding.state === "ready" ? "ready" : "attention"}>{binding.state}</StatusPill>}
          description="Provisioned for this workspace. Its address and credentials stay with the service that holds them."
          headingLevel={2}
          title="Warehouse binding"
        >
          <dl className="record">
            <dt>Binding</dt>
            <dd>
              <code>{binding.binding_ref}</code>
            </dd>
            <dt>Engine</dt>
            <dd>{binding.engine}</dd>
            <dt>Region</dt>
            <dd>{binding.region}</dd>
            <dt>Capacity</dt>
            <dd>{binding.capacity}</dd>
            <dt>Mutability</dt>
            <dd>{binding.immutable ? "Immutable once bound" : "Replaceable"}</dd>
          </dl>
        </Panel>
      )}

      <Panel
        description="Each dependency reports its own state; none of them exposes provider administration."
        headingLevel={2}
        title="Dependencies"
      >
        {services.length === 0 ? (
          <Nothing>
            This deployment runs no managed dependency of its own. The warehouse above is
            provisioned directly by the workspace.
          </Nothing>
        ) : (
          <ul aria-label="Managed service states" className="service-ledger">
            {services.map((service) => (
              <li className="service-card" key={service.service}>
                <div>
                  <h3>{service.label}</h3>
                  <p>{service.detail}</p>
                  {service.validation_summary === null ||
                  service.validation_summary === undefined ? null : (
                    <p className="service-card__validation">{service.validation_summary}</p>
                  )}
                </div>
                <div className="service-card__state">
                  <CapabilityState state={service.state} />
                  {service.service === "superset" ? (
                    <button disabled={service.state === "not_delivered"} type="button">
                      Open dashboards
                    </button>
                  ) : null}
                </div>
              </li>
            ))}
          </ul>
        )}
      </Panel>
    </section>
  )
}
