import {describe, expect, test} from "vitest"

import {formatInstant} from "./instant"

describe("formatInstant", () => {
  test("writes the instant the same way wherever the console is opened", () => {
    // The point of one formatter is that two people comparing an answer against the dashboard
    // drawn from it are comparing values rather than formats, so the month is a word, the
    // clock is 24-hour, and the zone is stated instead of assumed.
    expect(formatInstant("2026-09-11T11:55:00Z")).toBe("Sep 11, 2026, 11:55:00 UTC")
  })

  test("states the zone rather than quietly shifting the instant into the reader's own", () => {
    // Offsets resolve to the one moment they name, and it is reported in UTC: an evidence
    // timestamp that reads differently in two offices is a timestamp nobody can quote.
    expect(formatInstant("2026-09-11T13:55:00+02:00")).toBe("Sep 11, 2026, 11:55:00 UTC")
  })

  test("shows a value it cannot read as the server sent it", () => {
    // `Invalid Date` tells a reader nothing and loses the only thing worth quoting in a
    // support thread, which is what actually arrived.
    expect(formatInstant("not-an-instant")).toBe("not-an-instant")
  })
})
