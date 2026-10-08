/**
 * Build the third-party notices from what the product actually ships.
 *
 * Written down by hand, a notices page is wrong the first time a lockfile moves, and a wrong
 * one is worse than none: it states, in the product's own voice, that it is built on something
 * it is not. So it is derived from the three manifests that decide what is distributed --
 * the resolved runtime closure of the console server, the production closure of the browser
 * bundle, and the images the deployment runs -- and `--check` fails the build when the
 * committed page no longer matches them.
 *
 * Every input is read offline, from the lockfile, the installed metadata and the Dockerfiles.
 */
import {execFileSync} from "node:child_process"
import {mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync} from "node:fs"
import {tmpdir} from "node:os"
import {dirname, join, resolve} from "node:path"
import {fileURLToPath} from "node:url"

const consoleRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..")
const repositoryRoot = resolve(consoleRoot, "../..")
const committed = join(consoleRoot, "web/src/notices/generated-notices.ts")
const checkOnly = process.argv.includes("--check")

/**
 * What a container image is licensed under.
 *
 * The only input that cannot be derived: an image carries no manifest the build can read. A
 * repository with no entry is a hard failure rather than an omission, so adding a service to
 * the deployment forces someone to say what it is licensed under instead of letting it appear
 * in the product unattributed.
 */
const IMAGE_LICENSES = {
  "apache/superset": {display: "Apache Superset", license: "Apache-2.0"},
  postgres: {display: "PostgreSQL", license: "PostgreSQL"},
  node: {display: "Node.js", license: "MIT"},
  python: {display: "Python", license: "PSF-2.0"},
}

/** Manifests the page is derived from, named on the page so a reader can check it. */
const SOURCES = [
  "uv.lock",
  "apps/console/package-lock.json",
  "deploy/quickstart/compose.yaml",
  "deploy/quickstart/Dockerfile",
  "deploy/quickstart/superset/Dockerfile",
]

function run(command, args, cwd) {
  return execFileSync(command, args, {cwd, encoding: "utf8", maxBuffer: 64 * 1024 * 1024})
}

/** The console server's resolved runtime closure, without the workspace's own members. */
function serverComponents() {
  const exported = run(
    "uv",
    ["export", "--package", "heinzel-console", "--no-dev", "--no-hashes", "--no-emit-project",
     "--format", "requirements-txt"],
    repositoryRoot,
  )
  const pinned = new Map()
  for (const line of exported.split("\n")) {
    const match = /^([A-Za-z0-9._-]+)==([^\s;]+)/.exec(line.trim())
    if (match && !match[1].startsWith("heinzel-")) {
      pinned.set(match[1], match[2])
    }
  }
  const licenses = pythonLicenses([...pinned.keys()])
  return [...pinned].map(([name, version]) => ({
    name,
    version,
    license: licenses[name] ?? "unknown",
    surface: "console server",
  }))
}

/**
 * Licences as each installed distribution declares them.
 *
 * Two shapes, because the metadata standard changed: a `License-Expression` on anything
 * recent, and an OSI classifier on everything else. Nothing is inferred from a package's
 * name -- what cannot be read is reported as unknown, which is a fact about the metadata
 * rather than a guess about the licence.
 */
function pythonLicenses(names) {
  const script = `
import importlib.metadata as md, json, sys
_CLASSIFIER = {
    "Apache Software License": "Apache-2.0",
    "BSD License": "BSD-3-Clause",
    "MIT License": "MIT",
    "Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0",
    "Python Software Foundation License": "PSF-2.0",
    "GNU Lesser General Public License v3 (LGPLv3)": "LGPL-3.0-only",
    "ISC License (ISCL)": "ISC",
}
out = {}
for name in json.load(sys.stdin):
    try:
        meta = md.metadata(name)
    except md.PackageNotFoundError:
        continue
    declared = meta.get("License-Expression")
    if not declared:
        for classifier in meta.get_all("Classifier") or []:
            if classifier.startswith("License :: "):
                declared = _CLASSIFIER.get(classifier.rsplit(" :: ", 1)[-1])
                if declared:
                    break
    if not declared:
        single_line = (meta.get("License") or "").strip()
        declared = single_line if single_line and "\\n" not in single_line and len(single_line) < 64 else None
    if declared:
        out[name] = declared
json.dump(out, sys.stdout)
`
  const interpreter = join(repositoryRoot, ".venv/bin/python")
  return JSON.parse(
    execFileSync(interpreter, ["-c", script], {
      cwd: repositoryRoot,
      encoding: "utf8",
      input: JSON.stringify(names),
    }),
  )
}

/** Everything npm would install for production, which is everything the bundle can carry. */
function bundleComponents() {
  const tree = JSON.parse(run("npm", ["ls", "--omit=dev", "--all", "--json"], consoleRoot))
  const found = new Map()
  const walk = (node) => {
    for (const [name, info] of Object.entries(node.dependencies ?? {})) {
      const key = `${name}@${info.version}`
      if (found.has(key)) continue
      found.set(key, {name, version: info.version})
      walk(info)
    }
  }
  walk(tree)
  return [...found.values()]
    .map(({name, version}) => ({
      name,
      version,
      license: packageLicense(name) ?? "unknown",
      surface: "console bundle",
    }))
    .sort((a, b) => a.name.localeCompare(b.name))
}

function packageLicense(name) {
  const manifest = JSON.parse(
    readFileSync(join(consoleRoot, "node_modules", name, "package.json"), "utf8"),
  )
  if (typeof manifest.license === "string") return manifest.license
  if (Array.isArray(manifest.licenses)) {
    return manifest.licenses.map((entry) => entry.type).filter(Boolean).join(" OR ") || null
  }
  return null
}

/** The images the deployment runs, as its Compose file and Dockerfiles pin them. */
function serviceComponents() {
  const pinned = new Map()
  const record = (reference) => {
    const [repository, rest] = reference.split("@")[0].split(":")
    const digest = reference.includes("@") ? reference.split("@")[1] : null
    const known = IMAGE_LICENSES[repository]
    if (known === undefined) {
      throw new Error(
        `no licence recorded for the image '${repository}'. Add it to IMAGE_LICENSES in ` +
          `scripts/generate-notices.mjs so it is attributed rather than shipped unnamed.`,
      )
    }
    pinned.set(repository, {
      name: known.display,
      version: rest ?? (digest ? digest.slice(0, 19) : "unpinned"),
      license: known.license,
      surface: "deployed service",
    })
  }
  for (const relative of ["deploy/quickstart/Dockerfile", "deploy/quickstart/superset/Dockerfile"]) {
    for (const line of readFileSync(join(repositoryRoot, relative), "utf8").split("\n")) {
      const match = /^FROM\s+(\S+)/.exec(line.trim())
      if (match) record(match[1])
    }
  }
  for (const line of readFileSync(join(repositoryRoot, "deploy/quickstart/compose.yaml"), "utf8").split("\n")) {
    const match = /^\s*image:\s*(\S+)/.exec(line)
    if (match) record(match[1])
  }
  return [...pinned.values()].sort((a, b) => a.name.localeCompare(b.name))
}

function render(components) {
  const rows = components
    .map(
      (c) =>
        `  {name: ${JSON.stringify(c.name)}, version: ${JSON.stringify(c.version)}, ` +
        `license: ${JSON.stringify(c.license)}, surface: ${JSON.stringify(c.surface)}},`,
    )
    .join("\n")
  return `// GENERATED by apps/console/scripts/generate-notices.mjs. Do not edit by hand.
//
// Derived from what the product ships: the resolved runtime closure of the console server,
// the production closure of the browser bundle, and the images the deployment runs.
// Regenerate with \`npm run generate:notices\`; \`npm run check:notices\` fails when this file
// and those manifests disagree.

/** Where in the product a component is distributed. */
export type NoticeSurface = "console server" | "console bundle" | "deployed service"

export interface ThirdPartyComponent {
  readonly name: string
  readonly version: string
  /** As the component declares it. \`unknown\` means its metadata did not say, not that it is unlicensed. */
  readonly license: string
  readonly surface: NoticeSurface
}

/** The manifests this was read from, named on the page so a reader can check it. */
export const NOTICE_SOURCES: readonly string[] = [
${SOURCES.map((s) => `  ${JSON.stringify(s)},`).join("\n")}
]

export const THIRD_PARTY_COMPONENTS: readonly ThirdPartyComponent[] = [
${rows}
]
`
}

const components = [...serverComponents(), ...bundleComponents(), ...serviceComponents()]
const rendered = render(components)

if (checkOnly) {
  const temporary = mkdtempSync(join(tmpdir(), "heinzel-notices-"))
  try {
    if (readFileSync(committed, "utf8") !== rendered) {
      const stale = join(temporary, "generated-notices.ts")
      writeFileSync(stale, rendered, "utf8")
      process.stderr.write(
        `the committed third-party notices no longer match the manifests they are derived ` +
          `from. Run 'npm run generate:notices'.\n`,
      )
      process.exitCode = 1
    }
  } finally {
    rmSync(temporary, {force: true, recursive: true})
  }
} else {
  writeFileSync(committed, rendered, "utf8")
  process.stdout.write(`wrote ${components.length} third-party components\n`)
}
