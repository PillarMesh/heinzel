#!/usr/bin/env node
import {spawn} from "node:child_process"
import {mkdtemp, rm} from "node:fs/promises"
import {tmpdir} from "node:os"
import {dirname, join, resolve} from "node:path"
import process from "node:process"
import {fileURLToPath} from "node:url"

const consoleRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..")
const repositoryRoot = resolve(consoleRoot, "..", "..")
const stateDirectory = await mkdtemp(join(tmpdir(), "pillarmesh-native-e2e-"))
const readyFile = join(stateDirectory, "ready.json")
let stopping = false

const child = spawn(
  "uv",
  [
    "run",
    "python",
    "-m",
    "tests.acceptance.run_native_answer_console",
    "--directory",
    stateDirectory,
    "--ready-file",
    readyFile,
    "--dist",
    resolve(consoleRoot, "dist"),
    "--architect-port",
    "8230",
    "--requester-port",
    "8231",
  ],
  {cwd: repositoryRoot, env: process.env, stdio: "inherit"},
)

async function shutdown(exitCode) {
  if (stopping) return
  stopping = true
  if (child.exitCode === null && child.signalCode === null) {
    await new Promise((resolveExit) => {
      child.once("exit", resolveExit)
      child.kill("SIGTERM")
    })
  }
  await rm(stateDirectory, {recursive: true, force: true})
  process.exit(exitCode)
}

child.on("exit", (code, signal) => {
  if (!stopping) {
    process.stderr.write(
      `native console exited unexpectedly: code=${String(code)} signal=${String(signal)}\n`,
    )
    void shutdown(1)
  }
})

process.on("SIGINT", () => void shutdown(0))
process.on("SIGTERM", () => void shutdown(0))

await new Promise(() => {})
