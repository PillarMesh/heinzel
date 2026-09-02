import type {SetupView, WorkspaceView} from "../api/generated"

export function selectLandingRoute(workspace: WorkspaceView, setup?: SetupView): string {
  switch (workspace.state) {
    case "setup":
      return "/setup"
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
