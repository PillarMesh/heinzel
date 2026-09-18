#!/usr/bin/env node
import {spawn} from "node:child_process"
import {mkdtemp, rm} from "node:fs/promises"
import {tmpdir} from "node:os"
import {dirname, join, resolve} from "node:path"
import process from "node:process"
import {fileURLToPath} from "node:url"

const consoleRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..")
const repositoryRoot = resolve(consoleRoot, "..", "..")
const stateDirectory = await mkdtemp(join(tmpdir(), "heinzel-governed-e2e-"))
const children = []
let stopping = false

function start(port, actor) {
  const child = spawn(
    "uv",
    [
      "run",
      "python",
      "-m",
      "tests.acceptance.run_console_governed",
      "--directory",
      stateDirectory,
      "--port",
      String(port),
      "--actor",
      actor,
      "--no-seed",
      "--dist",
      resolve(consoleRoot, "dist"),
    ],
    {cwd: repositoryRoot, env: process.env, stdio: "inherit"},
  )
  children.push(child)
  child.on("exit", (code, signal) => {
    if (!stopping) {
      process.stderr.write(
        `governed ${actor} server exited unexpectedly: code=${String(code)} signal=${String(signal)}\n`,
      )
      void shutdown(1)
    }
  })
}

async function waitUntilHealthy(port) {
  const deadline = Date.now() + 30_000
  while (!stopping && Date.now() < deadline) {
    try {
      const response = await fetch(`http://127.0.0.1:${String(port)}/healthz`)
      if (response.ok) {
        return
      }
    } catch {
      await new Promise((resolveWait) => setTimeout(resolveWait, 100))
    }
  }
  throw new Error(`governed server on port ${String(port)} did not become healthy`)
}

async function shutdown(exitCode) {
  if (stopping) {
    return
  }
  stopping = true
  const exits = children.map(
    (child) =>
      new Promise((resolveExit) => {
        if (child.exitCode !== null || child.signalCode !== null) {
          resolveExit()
          return
        }
        child.once("exit", resolveExit)
        child.kill("SIGTERM")
      }),
  )
  await Promise.all(exits)
  await rm(stateDirectory, {recursive: true, force: true})
  process.exit(exitCode)
}

process.on("SIGINT", () => void shutdown(0))
process.on("SIGTERM", () => void shutdown(0))

try {
  start(8130, "architect-a")
  await waitUntilHealthy(8130)
  start(8131, "requester-a")
  process.stdout.write(`governed E2E state: ${stateDirectory}\n`)
  await new Promise(() => {})
} catch (error) {
  process.stderr.write(`${error instanceof Error ? error.message : String(error)}\n`)
  await shutdown(1)
}
