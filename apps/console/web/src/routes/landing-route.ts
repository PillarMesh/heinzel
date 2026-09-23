import type {SetupView, WorkspaceView} from "../api/generated"

export function selectLandingRoute(workspace: WorkspaceView, setup?: SetupView): string {
  switch (workspace.state) {
    case "setup":
      // A workspace in setup lands on the workbench that carries it, unless the
      // deployment delivers no setup projection to put there. The governed backend
      // no longer reports `setup` for an undelivered warehouse binding, so this
      // fallback is a guard for any other backend that does: landing on a stage list
      // the server will not serve would state a disagreement that did not happen.
      // The inbox does not depend on the warehouse binding, so it carries the landing.
      return setup === undefined ? "/inbox" : "/setup"
    case "pending_activation": {
      const pendingReviews = setup?.pending_review_refs ?? []
      return pendingReviews.length === 1 ? `/reviews/${pendingReviews[0]}` : "/recovery"
    }
    case "active":
      return "/inbox"
    case "unavailable":
      return "/recovery"
  }
}
