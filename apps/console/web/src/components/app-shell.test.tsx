import {render, screen} from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import {readFileSync} from "node:fs"
import {resolve} from "node:path"
import {MemoryRouter, Route, Routes} from "react-router-dom"
import {afterEach, describe, expect, test, vi} from "vitest"

import type {SessionView, WorkspaceView} from "../api/generated"
import {AppShell} from "./app-shell"
import {ErrorBoundary} from "./error-boundary"

const globalStyles = readFileSync(
  resolve(process.cwd(), "web/src/styles/global.css"),
  "utf8",
)

const session: SessionView = {
  actor: {display_name: "Dana Architect"},
  roles: ["data_architect"],
  active_role: "data_architect",
  tenant: {ref: "tenant-demo", display_name: "Northwind Demo"},
  workspace: {ref: "workspace-revenue", display_name: "Revenue to cash"},
  csrf_token: "csrf-token-with-at-least-thirty-two-characters",
}

const workspace: WorkspaceView = {
  workspace: {ref: "workspace-revenue", display_name: "Revenue to cash"},
  state: "active",
  capabilities: [
    {
      capability_id: "governed-evidence",
      label: "Governed evidence",
      state: "not_delivered",
      detail: "No authoritative fixture evidence is available.",
      dependency: "Governed evidence service wiring",
    },
    {
      capability_id: "fixture-journey",
      label: "Fixture journey",
      state: "ready",
      detail: "Synthetic lifecycle interactions are available.",
    },
  ],
}

function installMedia(matches: Readonly<Record<string, boolean>>): void {
  vi.stubGlobal(
    "matchMedia",
    vi.fn((query: string): MediaQueryList => ({
      matches: matches[query] ?? false,
      media: query,
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })),
  )
}

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

function renderShell(path = "/runs") {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <AppShell
        dataProvenance="demo_fixture"
        evidence={<p>Evidence references</p>}
        queue={<button type="button">Request one</button>}
        session={session}
        workspace={workspace}
      >
        <h1>Run history</h1>
        <button type="button">Review run</button>
      </AppShell>
    </MemoryRouter>,
  )
}

describe("AppShell", () => {
  test("exposes landmarks, persistent provenance, active navigation, and server capability state", () => {
    installMedia({})

    renderShell()

    expect(screen.getByRole("banner")).toBeInTheDocument()
    expect(screen.getByRole("navigation", {name: "Product"})).toBeInTheDocument()
    expect(screen.getByRole("main")).toHaveAttribute("id", "main-content")
    expect(screen.getByRole("complementary", {name: "Governance spine"})).toBeInTheDocument()
    expect(screen.getByText("Demo scenario - no managed effects")).toBeInTheDocument()
    expect(screen.getByRole("link", {name: "Runs"})).toHaveAttribute("aria-current", "page")
    expect(screen.getByText("Not delivered")).toBeInTheDocument()
    expect(screen.getByText("Governed evidence service wiring")).toBeInTheDocument()
  })

  test("puts the skip link and all actions in the keyboard order without hover", async () => {
    installMedia({})
    const user = userEvent.setup()
    renderShell()

    await user.tab()

    expect(screen.getByRole("link", {name: "Skip to current work"})).toHaveFocus()
    expect(screen.getByRole("button", {name: "Review run"})).toBeVisible()
    expect(screen.getByRole("button", {name: "Request one"})).toBeVisible()
  })

  test("collapses evidence into a keyboard disclosure below 1120px", () => {
    installMedia({"(max-width: 1119px)": true})

    renderShell()

    expect(screen.getByTestId("responsive-workspace")).toHaveAttribute("data-layout", "drawer")
    expect(screen.getByText("Show evidence").tagName).toBe("SUMMARY")
    expect(screen.queryByRole("complementary", {name: "Evidence"})).not.toBeInTheDocument()
  })

  test("uses a linear queue-detail composition below 760px", () => {
    installMedia({"(max-width: 1119px)": true, "(max-width: 759px)": true})

    renderShell()

    expect(screen.getByTestId("responsive-workspace")).toHaveAttribute(
      "data-layout",
      "queue-detail",
    )
    expect(screen.getByText("Show decision queue").tagName).toBe("SUMMARY")
  })

  test("marks reduced-motion mode for deterministic transition removal", () => {
    installMedia({"(prefers-reduced-motion: reduce)": true})

    renderShell()

    expect(screen.getByRole("main")).toHaveAttribute("data-motion", "reduced")
  })

  test.each([
    [{}, "responsive-workspace--no-slots"],
    [{queue: <p>Queue slot</p>}, "responsive-workspace--has-queue"],
    [{evidence: <p>Evidence slot</p>}, "responsive-workspace--has-evidence"],
  ])("exposes slot presence as an observable grid contract", (slots, expectedClass) => {
    installMedia({})

    render(
      <MemoryRouter>
        <AppShell
          dataProvenance="demo_fixture"
          session={session}
          workspace={workspace}
          {...slots}
        >
          <h1>Current work</h1>
        </AppShell>
      </MemoryRouter>,
    )

    expect(screen.getByTestId("responsive-workspace")).toHaveClass(expectedClass)
  })

  test("defines explicit full-width no-slot contracts for wide and drawer layouts", () => {
    expect(globalStyles).toContain(
      ".responsive-workspace--wide.responsive-workspace--no-slots",
    )
    expect(globalStyles).toContain(
      ".responsive-workspace--drawer.responsive-workspace--no-slots",
    )
    expect(globalStyles).toMatch(/--no-slots[^}]*grid-template-columns:\s*minmax\(0,\s*1fr\)/s)
  })

  test("restores focus and announces a product-route transition once", async () => {
    installMedia({})
    const user = userEvent.setup()

    render(
      <MemoryRouter initialEntries={["/runs"]}>
        <AppShell dataProvenance="demo_fixture" session={session} workspace={workspace}>
          <Routes>
            <Route element={<h1>Run history</h1>} path="/runs" />
            <Route element={<h1>Catalog assets</h1>} path="/catalog" />
          </Routes>
        </AppShell>
      </MemoryRouter>,
    )

    await user.click(screen.getByRole("link", {name: "Catalog"}))

    expect(await screen.findByRole("heading", {name: "Catalog assets"})).toBeVisible()
    expect(screen.getByRole("main")).toHaveFocus()
    expect(screen.getByRole("status", {name: "Route change"})).toHaveTextContent("Catalog assets")
  })

  test("remounts a failed route boundary when navigation reaches a healthy route", async () => {
    function ExplodingView(): never {
      throw new Error("private route canary")
    }
    vi.spyOn(console, "error").mockImplementation(() => undefined)
    installMedia({})
    const user = userEvent.setup()

    render(
      <MemoryRouter initialEntries={["/runs"]}>
        <AppShell dataProvenance="demo_fixture" session={session} workspace={workspace}>
          <Routes>
            <Route element={<ExplodingView />} path="/runs" />
            <Route element={<h1>Healthy catalog</h1>} path="/catalog" />
          </Routes>
        </AppShell>
      </MemoryRouter>,
    )

    expect(screen.getByRole("heading", {name: "This view could not be displayed"})).toBeVisible()
    await user.click(screen.getByRole("link", {name: "Catalog"}))

    expect(await screen.findByRole("heading", {name: "Healthy catalog"})).toBeVisible()
  })
})

test("the error boundary replaces untrusted view content with recovery guidance", () => {
  function ExplodingView(): never {
    throw new Error("private response canary")
  }
  vi.spyOn(console, "error").mockImplementation(() => undefined)

  render(
    <ErrorBoundary>
      <ExplodingView />
    </ErrorBoundary>,
  )

  expect(screen.getByRole("heading", {name: "This view could not be displayed"})).toBeVisible()
  expect(screen.queryByText("private response canary")).not.toBeInTheDocument()
})
