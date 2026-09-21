import type {SetupView, WorkspaceView} from "../api/generated"

export function selectLandingRoute(workspace: WorkspaceView, setup?: SetupView): string {
  switch (workspace.state) {
    case "setup":
      // A workspace in setup lands on the workbench that carries it, unless the
      // deployment delivers no setup projection to put there. Landing on a stage
      // list the server will not serve states a disagreement that did not happen,
      // so the inbox - the governed surface that does not depend on the warehouse
      // binding - carries the landing instead. Where `/setup` answers normally the
      // setup projection is present and this returns `/setup` as before.
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
