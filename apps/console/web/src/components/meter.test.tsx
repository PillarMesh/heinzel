import {render, screen} from "@testing-library/react"
import {expect, test} from "vitest"

import {Meter} from "./meter"

test("states the counts it draws, so the bar is never the only copy of the value", () => {
  render(<Meter label="Capabilities ready" of={16} value={8} />)

  const meter = screen.getByRole("meter", {name: "Capabilities ready"})
  expect(meter).toHaveAttribute("aria-valuenow", "8")
  expect(meter).toHaveAttribute("aria-valuemax", "16")
  expect(meter).toHaveAttribute("aria-valuetext", "8 of 16")
})

test("a whole that is accounted for reads as ready rather than as progress", () => {
  render(<Meter label="Capabilities ready" of={4} value={4} />)

  expect(screen.getByRole("meter")).toHaveClass("meter--ready")
})

test.each([
  ["a whole of nothing, which would divide by zero", 0, 3],
  ["more than the whole, which would overrun the track", 9, 4],
  ["less than nothing", -2, 4],
])("%s is bounded rather than drawn", (_name, value, of) => {
  render(<Meter label="Bounded" of={of} value={value} />)

  const fill = screen.getByRole("meter").firstElementChild
  const width = (fill as HTMLElement).style.getPropertyValue("--meter-fill")
  expect(["0%", "100%"]).toContain(width)
})
