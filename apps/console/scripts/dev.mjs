#!/usr/bin/env node
// One command for the local demonstration: the loopback API and the Vite dev
// server together. It prints the authenticated local URL and the backend mode so
// a demonstration can never be mistaken for a governed environment.
import { spawn } from "node:child_process"
import process from "node:process"

const mode = process.env.HEINZEL_CONSOLE_MODE ?? "demo_fixture"
const apiPort = process.env.HEINZEL_CONSOLE_API_PORT ?? "8000"
const children = []

function run(command, args, options) {
  const child = spawn(command, args, { stdio: "inherit", ...options })
  children.push(child)
  child.on("exit", (code) => {
    if (code !== 0 && code !== null) {
      shutdown(code)
    }
  })
  return child
}

function shutdown(code) {
  for (const child of children) {
    if (child.exitCode === null) {
      child.kill("SIGTERM")
    }
  }
  process.exit(code)
}

process.on("SIGINT", () => shutdown(0))
process.on("SIGTERM", () => shutdown(0))

run("uv", [
  "run",
  "uvicorn",
  "heinzel_console:create_app",
  "--factory",
  "--host",
  "127.0.0.1",
  "--port",
  apiPort,
], { cwd: "../.." })
run("npm", ["run", "dev"])

process.stdout.write(
  `\nHeinzel console\n  mode: ${mode}\n  api:  http://127.0.0.1:${apiPort}\n` +
    "  app:  http://127.0.0.1:5173\n\n",
)
