import { render, screen } from "@testing-library/react"

import { App } from "./app"

test("renders the PillarMesh product landmark", () => {
  render(<App />)

  expect(screen.getByRole("main")).toHaveAccessibleName("PillarMesh console")
})
