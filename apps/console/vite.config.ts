import react from "@vitejs/plugin-react"
import { defineConfig } from "vitest/config"
import { thirdPartyLicensesPlugin } from "./build/third-party-licenses"

export default defineConfig({
  root: "web",
  plugins: [react(), thirdPartyLicensesPlugin()],
  server: {
    // The development proxy must preserve the origin so the same-origin and CSRF
    // checks the API enforces in production also hold while developing.
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: false,
      },
    },
  },
  build: {
    outDir: "../dist",
    emptyOutDir: true,
  },
  test: {
    environment: "jsdom",
    // The default include pattern is relative to `root` ("web") and would miss the build-tooling
    // test below `../build`, which has no DOM to test against and needs no jsdom setup.
    include: ["src/**/*.{test,spec}.{ts,tsx}", "../build/**/*.{test,spec}.ts"],
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    // `userEvent` drives each keystroke through jsdom asynchronously, so the
    // interaction-heavy suites finish well inside a second when they have a core
    // to themselves and take several when they do not. Vitest runs the files in
    // parallel, which means the default five seconds is a measure of how many
    // other files are running rather than of anything under test -- adding one
    // test file was enough to start timing the existing ones out. No assertion
    // here measures elapsed time, so the bound only has to exceed contention.
    testTimeout: 20_000,
  },
})
