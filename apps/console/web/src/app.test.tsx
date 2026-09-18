import { render, screen } from "@testing-library/react"

import { App } from "./app"

test("renders the Heinzel product landmark", () => {
  render(<App />)

  expect(screen.getByRole("main")).toHaveAccessibleName("Heinzel console")
})
