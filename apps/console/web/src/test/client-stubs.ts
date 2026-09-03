import {vi} from "vitest"

import type {ApiMeta} from "../api/generated"
import type {InboxClient} from "../features/inbox/decision-workspace"
import type {RequesterClient} from "../features/requests/my-requests"
import type {RunsClient} from "../features/runs/runs-page"

/**
 * Stubs for the feature protocols a test does not itself exercise.
 *
 * The app builds one client satisfying every feature protocol, so a partial mock
 * no longer type-checks. Reads return an empty projection, because rendering a
 * route legitimately issues them. Mutations reject, so an unexpected write fails
 * the test rather than passing quietly.
 */
function unexpected(name: string) {
  return vi.fn(async () => {
    throw new Error(`unexpected console client call: ${name}`)
  })
}

function stubMeta(provenance: ApiMeta["data_provenance"]): ApiMeta {
  return {correlation_id: "correlation-stub", data_provenance: provenance}
}

export function featureClientStubs(
  provenance: ApiMeta["data_provenance"] = "demo_fixture",
): InboxClient & RequesterClient & RunsClient {
  const meta = stubMeta(provenance)
  return {
    acceptClarifiedOutcome: unexpected("acceptClarifiedOutcome"),
    appendConversationMessage: unexpected("appendConversationMessage"),
    createRequest: unexpected("createRequest"),
    decideRequest: unexpected("decideRequest"),
    getCatalogAsset: vi.fn(async () => ({meta, data: null})),
    getClarifiedOutcome: unexpected("getClarifiedOutcome"),
    getConversation: unexpected("getConversation"),
    getDashboard: vi.fn(async () => ({meta, data: null})),
    getInbox: vi.fn(async () => ({meta, data: {items: [], selected_request_id: null}})),
    getRequestDetail: unexpected("getRequestDetail"),
    getRequesterRequests: vi.fn(async () => ({meta, data: []})),
    getRuns: vi.fn(async () => ({meta, data: {runs: []}})),
  } as unknown as InboxClient & RequesterClient & RunsClient
}
