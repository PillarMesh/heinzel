import type {SetupView} from "../../api/generated"
import {CapabilityState} from "../../components/capability-state"

interface SourcesStageProps {
  readonly setup: SetupView
}

export function SourcesStage({setup}: SourcesStageProps) {
  return (
    <section aria-labelledby="sources-title" className="setup-stage">
      <p className="eyebrow">Stage 3 · Sources</p>
      <h1 id="sources-title">Source boundaries</h1>
      <p className="setup-stage__lead">
        Confirm least-privilege connection intent through both permitted and denied probes.
      </p>
      <div className="source-grid">
        {(setup.sources ?? []).map((source) => (
          <article aria-label={source.display_name} className="source-card" key={source.source_ref}>
            <header>
              <div>
                <p className="source-card__type">{source.source_type}</p>
                <h2>{source.display_name}</h2>
              </div>
              <CapabilityState state={source.state} />
            </header>
            <section>
              <h3>Intended probes</h3>
              <ul>
                {(source.intended_checks ?? []).map((check) => (
                  <li key={check}>{check}</li>
                ))}
              </ul>
            </section>
            <section>
              <h3>Denial probes</h3>
              <ul>
                {(source.denied_checks ?? []).map((check) => (
                  <li key={check}>{check}</li>
                ))}
              </ul>
            </section>
          </article>
        ))}
      </div>
    </section>
  )
}
