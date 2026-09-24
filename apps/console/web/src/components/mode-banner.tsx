import type {DataProvenance} from "../api/generated"

interface ModeBannerProps {
  readonly dataProvenance: DataProvenance
}

export function ModeBanner({dataProvenance}: ModeBannerProps) {
  if (dataProvenance === "demo_fixture") {
    return (
      <div className="mode-banner mode-banner--fixture" role="note">
        Demo scenario - no managed effects
      </div>
    )
  }

  return (
    <div className="mode-banner mode-banner--governed" role="note">
      Governed local workspace
    </div>
  )
}
