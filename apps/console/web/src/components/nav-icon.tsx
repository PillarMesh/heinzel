/**
 * A mark beside each place in the product.
 *
 * The rail was ten words in a column, which gives a reader nothing to aim at: every item is the
 * same shape, so finding `Evidence` means reading the list rather than recognising a position.
 * A mark is the recognition channel, and it is never the only one -- the label stays, these are
 * `aria-hidden`, and the rail reads identically to a screen reader as it did before.
 *
 * Outlined paths in `currentColor` on a 16-unit grid, so an icon inherits the link's own colour
 * in both themes and in its active and hover states without a second palette to maintain.
 */
const ARTWORK: Readonly<Record<string, string>> = {
  // Sliders: a workspace being set up rather than a cog, which would read as preferences.
  "/setup":
    "M2 4h12M2 8h12M2 12h12M10.5 2.5v3M5.5 6.5v3M9.5 10.5v3",
  // A tray with an open mouth.
  "/inbox": "M2 9.5h3.5l1 2h3l1-2H14M2.5 9.5V4a1.5 1.5 0 0 1 1.5-1.5h8A1.5 1.5 0 0 1 13.5 4v5.5",
  // A built thing, seen as a box with its top face drawn.
  "/data-products": "M8 2.2l5.5 3v5.6L8 13.8l-5.5-3V5.2zM2.5 5.2L8 8.2l5.5-3M8 8.2v5.6",
  // Something that executes.
  "/runs": "M8 2a6 6 0 1 1 0 12A6 6 0 0 1 8 2zM6.8 5.6l3.8 2.4-3.8 2.4z",
  "/incidents": "M8 2.6l5.7 9.9H2.3zM8 6.6v2.6M8 11.1v.1",
  // Records arriving from outside and landing.
  "/acquisition-receipts": "M8 2v6.6M5.6 6.4L8 8.8l2.4-2.4M2.6 10.8v1.6a1.4 1.4 0 0 0 1.4 1.4h8a1.4 1.4 0 0 0 1.4-1.4v-1.6",
  "/catalog": "M8 4.6A2.6 2.6 0 0 0 5.4 2H2.4v9.4h3.4A2.2 2.2 0 0 1 8 13.6a2.2 2.2 0 0 1 2.2-2.2h3.4V2h-3A2.6 2.6 0 0 0 8 4.6zM8 4.6v9",
  // What a dashboard draws.
  "/dashboards": "M2.4 13.6h11.2M4.6 13.6V8.4M8 13.6V3.8M11.4 13.6v-3.4",
  // A shield that has been checked, not a padlock: this is attestation, not secrecy.
  "/evidence": "M8 2.2l5 1.9v4.3c0 3-2.1 5.2-5 5.9-2.9-.7-5-2.9-5-5.9V4.1zM5.9 8.1l1.5 1.5 2.7-2.8",
  // A question asked and waiting on an answer.
  "/requests": "M3.4 2.8h9.2a1.4 1.4 0 0 1 1.4 1.4v5.4a1.4 1.4 0 0 1-1.4 1.4H7l-3.2 2.4v-2.4h-.4A1.4 1.4 0 0 1 2 9.6V4.2a1.4 1.4 0 0 1 1.4-1.4zM6.4 5.6a1.6 1.6 0 0 1 3.1.5c0 1.1-1.5 1.3-1.5 2.3M8 10v.1",
}

export function NavIcon({to}: {readonly to: string}) {
  const artwork = ARTWORK[to]
  if (artwork === undefined) {
    return null
  }
  return (
    <svg
      aria-hidden="true"
      className="nav-icon"
      fill="none"
      focusable="false"
      stroke="currentColor"
      strokeLinecap="round"
      strokeLinejoin="round"
      strokeWidth={1.3}
      viewBox="0 0 16 16"
    >
      <path d={artwork} />
    </svg>
  )
}
