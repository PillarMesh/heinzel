import {defineConfig, devices} from "@playwright/test"

const REQUESTER_ORIGIN = "http://127.0.0.1:8231"

export default defineConfig({
  testDir: "e2e-native",
  outputDir: "test-results-native",
  forbidOnly: Boolean(process.env.CI),
  retries: 0,
  workers: 1,
  reporter: process.env.CI ? [["list"], ["html", {open: "never"}]] : [["list"]],
  use: {
    baseURL: REQUESTER_ORIGIN,
    viewport: {width: 1440, height: 1024},
    locale: "en-US",
    timezoneId: "UTC",
    colorScheme: "light",
    trace: "retain-on-failure",
  },
  projects: [
    {
      name: "desktop",
      use: {...devices["Desktop Chrome"], viewport: {width: 1440, height: 1024}},
    },
  ],
  webServer: {
    command: "node scripts/serve-native-e2e.mjs",
    url: `${REQUESTER_ORIGIN}/healthz`,
    reuseExistingServer: false,
    timeout: 180_000,
  },
})
