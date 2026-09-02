import { defineConfig, devices } from "@playwright/test"

// Determinism is the point: a fixed viewport, locale, timezone, colour scheme,
// and disabled animations, against the built same-origin server so the browser
// suite exercises what production serves rather than the dev proxy.
// The server admits a command only from its own configured origin. The suite
// uses the port `create_app` trusts by default; another port would need
// `PILLARMESH_CONSOLE_ALLOWED_ORIGIN` set to match, and a mismatch turns every
// command in the journey into `same_origin_required`.
const PORT = 8000
const ORIGIN = `http://127.0.0.1:${PORT}`

export default defineConfig({
  testDir: "e2e",
  outputDir: "test-results",
  forbidOnly: Boolean(process.env.CI),
  retries: 0,
  workers: 1,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : [["list"]],
  expect: { toHaveScreenshot: { maxDiffPixelRatio: 0.01 } },
  use: {
    baseURL: ORIGIN,
    viewport: { width: 1440, height: 1024 },
    locale: "en-US",
    timezoneId: "UTC",
    colorScheme: "light",
    reducedMotion: "reduce",
    trace: "retain-on-failure",
  },
  projects: [
    // The device descriptor carries its own 1280x720 viewport, and project-level
    // `use` outranks the top-level one, so the documented desktop viewport has to
    // be restated after the spread or the screenshots are captured at the wrong size.
    {
      name: "desktop",
      use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 1024 } },
    },
  ],
  webServer: {
    command: `npm run build && PILLARMESH_CONSOLE_API_PORT=${PORT} node scripts/serve-built.mjs`,
    url: `${ORIGIN}/healthz`,
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
})
