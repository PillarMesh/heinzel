/**
 * A page's failure, kept with the service's own code.
 *
 * Pages stored the message alone, which threw away the one thing that tells a deployment
 * boundary (`capability_not_delivered`) apart from something having gone wrong -- so six of
 * the nine pages the navigation offers rendered a boundary as an alert.
 */
export interface PageFailureState {
  readonly code: string
  readonly message: string
}

export function asPageFailure(error: unknown, fallback: string): PageFailureState {
  if (typeof error === "object" && error !== null && "code" in error && "message" in error) {
    const {code, message} = error as {code: unknown; message: unknown}
    if (typeof code === "string" && typeof message === "string") {
      return {code, message}
    }
  }
  return {code: "unavailable", message: fallback}
}
