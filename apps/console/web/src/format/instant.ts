/**
 * One way to write a moment in time, for every surface that shows one to a person.
 *
 * The product showed four. `toLocaleString()` with nothing said about it produced
 * `10/8/2026, 12:46:45 PM`, which is a different date in most of the world; a sentence beside
 * it said `October 8, 2026`; and the evidence panels printed the stored RFC 3339 string,
 * microseconds and all. A reader comparing an answer against the dashboard drawn from it was
 * comparing two formats as well as two values.
 *
 * The month is a word, so the order of the numbers cannot be misread, and the clock is
 * 24-hour, so no reading depends on spotting an am/pm. Seconds stay: this is an audit surface
 * and two events a second apart are two events.
 *
 * UTC, and it says so. The server records in UTC and the evidence is quoted between people --
 * two reviewers in two offices reading the same receipt as two different times, with nothing
 * on screen to say which, is a worse failure than a timestamp nobody has to convert. It also
 * makes what the console renders the same everywhere, which is what the rest of the product
 * promises about anything it writes down.
 */
const INSTANT = new Intl.DateTimeFormat(undefined, {
  day: "numeric",
  hour: "2-digit",
  hourCycle: "h23",
  minute: "2-digit",
  month: "short",
  second: "2-digit",
  timeZone: "UTC",
  timeZoneName: "short",
  year: "numeric",
})

/**
 * The instant as a person reads it, or the value unchanged when it is not one.
 *
 * A timestamp the console cannot parse is shown as the server sent it rather than as
 * `Invalid Date`: the raw value is the one a reader can quote in a support thread, and a
 * server that sends something unexpected here is not a reason to hide what it sent.
 */
export function formatInstant(value: string): string {
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime()) ? value : INSTANT.format(parsed)
}
