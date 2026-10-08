import {
  NOTICE_SOURCES,
  THIRD_PARTY_COMPONENTS,
  type NoticeSurface,
  type ThirdPartyComponent,
} from "./generated-notices"
import "./notices.css"

const SURFACES: readonly {readonly surface: NoticeSurface; readonly detail: string}[] = [
  {
    surface: "deployed service",
    detail: "Runs as its own service in a deployment of this product.",
  },
  {
    surface: "console server",
    detail: "Installed into the console service and distributed with it.",
  },
  {
    surface: "console bundle",
    detail: "Compiled into the JavaScript a browser is served.",
  },
]

function byLicenceThenName(a: ThirdPartyComponent, b: ThirdPartyComponent): number {
  return a.license.localeCompare(b.license) || a.name.localeCompare(b.name)
}

export function NoticesPage() {
  return (
    <section aria-labelledby="notices-title" className="summary-page">
      <p className="eyebrow">Open source</p>
      <h1 id="notices-title">Third-party notices</h1>
      <p className="summary-page__lead">
        The open-source components this product is built on, with the licence each one declares.
      </p>
      <p className="summary-page__guidance">
        Generated from the manifests that decide what is distributed, not maintained by hand, so
        it cannot describe a build other than this one. The full licence text of every component
        ships inside the artifact that carries it. {NOTICE_SOURCES.length} manifests are read:{" "}
        {NOTICE_SOURCES.map((source, index) => (
          <span key={source}>
            {index === 0 ? null : ", "}
            <code>{source}</code>
          </span>
        ))}
        .
      </p>

      {SURFACES.map(({surface, detail}) => {
        const components = THIRD_PARTY_COMPONENTS.filter(
          (component) => component.surface === surface,
        ).sort(byLicenceThenName)
        if (components.length === 0) return null
        return (
          <section aria-label={surface} className="notices-group" key={surface}>
            <h2>{surface}</h2>
            <p className="notices-group__detail">{detail}</p>
            <table className="notices-table">
              <caption className="visually-hidden">
                {components.length} components distributed as part of the {surface}
              </caption>
              <thead>
                <tr>
                  <th scope="col">Component</th>
                  {/*
                    Not every component is pinned by a version: the images are pinned by
                    digest, deliberately, so a rebuilt tag cannot change what runs. One
                    column, named for both, rather than a digest sitting under "Version".
                  */}
                  <th scope="col">Version or digest</th>
                  <th scope="col">Licence</th>
                </tr>
              </thead>
              <tbody>
                {components.map((component) => (
                  <tr key={`${component.name}@${component.version}`}>
                    <th scope="row">{component.name}</th>
                    <td>
                      <code>{component.version}</code>
                    </td>
                    <td>
                      {component.license === "unknown" ? (
                        // Said plainly rather than guessed. A licence this page invented would
                        // be the one thing on it nobody could check.
                        <span className="notices-table__unknown">
                          not declared in its metadata
                        </span>
                      ) : (
                        component.license
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>
        )
      })}
    </section>
  )
}
