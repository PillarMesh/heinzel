import type {SetupView} from "../../api/generated"
import {CapabilityState} from "../../components/capability-state"

interface ServicesStageProps {
  readonly setup: SetupView
}

export function ServicesStage({setup}: ServicesStageProps) {
  return (
    <section aria-labelledby="services-title" className="setup-stage">
      <p className="eyebrow">Stage 2 · Managed services</p>
      <h1 id="services-title">Managed services</h1>
      <p className="setup-stage__lead">
        Validate each managed dependency without exposing provider administration or private
        infrastructure details.
      </p>
      <ul aria-label="Managed service states" className="service-ledger">
        {(setup.managed_services ?? []).map((service) => (
          <li className="service-card" key={service.service}>
            <div>
              <h2>{service.label}</h2>
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
    </section>
  )
}
