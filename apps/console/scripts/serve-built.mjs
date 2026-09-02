#!/usr/bin/env node
// Proves that one Starlette origin serves the compiled application and the API.
// With --check it starts the server, asserts both, and exits without lingering.
import { spawn } from "node:child_process"
import { dirname, resolve } from "node:path"
import process from "node:process"
import { fileURLToPath } from "node:url"

const check = process.argv.includes("--check")
const port = process.env.PILLARMESH_CONSOLE_API_PORT ?? "8000"
const origin = `http://127.0.0.1:${port}`
// The server runs from the repository root so `uv` resolves the workspace, so the
// compiled bundle must be named absolutely rather than relative to that root.
const consoleRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..")
const distDirectory = resolve(consoleRoot, "dist")
const repositoryRoot = resolve(consoleRoot, "..", "..")

const server = spawn(
  "uv",
  [
    "run",
    "uvicorn",
    "pillarmesh_console:create_app",
    "--factory",
    "--host",
    "127.0.0.1",
    "--port",
    port,
  ],
  {
    stdio: check ? "ignore" : "inherit",
    cwd: repositoryRoot,
    env: { ...process.env, PILLARMESH_CONSOLE_DIST: distDirectory },
  },
)

function stop(code) {
  if (server.exitCode === null) {
    server.kill("SIGTERM")
  }
  process.exit(code)
}

process.on("SIGINT", () => stop(0))
process.on("SIGTERM", () => stop(0))

if (!check) {
  process.stdout.write(`\nPillarMesh console served from ${origin}\n\n`)
} else {
  const deadline = Date.now() + 30_000
  let ready = false
  while (Date.now() < deadline && !ready) {
    try {
      const health = await fetch(`${origin}/healthz`)
      ready = health.ok
    } catch {
      await new Promise((resolve) => setTimeout(resolve, 250))
    }
  }
  if (!ready) {
    process.stderr.write("console server did not become ready\n")
    stop(1)
  }
  const page = await fetch(origin)
  const pageBody = await page.text()
  const session = await fetch(`${origin}/api/v1/session`)
  const sessionBody = await session.json()
  const servesApp = page.ok && pageBody.includes("<div id=\"root\"")
  const servesApi = session.ok && typeof sessionBody?.meta?.data_provenance === "string"
  if (!servesApp || !servesApi) {
    process.stderr.write(
      `same-origin check failed: app=${servesApp} api=${servesApi}\n`,
    )
    stop(1)
  }
  process.stdout.write(`same-origin check passed at ${origin}\n`)
  stop(0)
}
