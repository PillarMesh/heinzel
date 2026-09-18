import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs"
import { tmpdir } from "node:os"
import { join, sep } from "node:path"
import { afterEach, describe, expect, test } from "vitest"

import {
  buildThirdPartyLicenseReport,
  collectBundledPackageRoots,
  findLicenseFile,
  readPackageMetadata,
  resolvePackageRoot,
  type BundleLike,
} from "./third-party-licenses"

let workspaceDir: string | undefined

afterEach(() => {
  if (workspaceDir) {
    rmSync(workspaceDir, { recursive: true, force: true })
    workspaceDir = undefined
  }
})

/** A fake `node_modules/<name>` directory with a `package.json` and, unless omitted, a LICENSE. */
function fakePackage(
  root: string,
  name: string,
  options: { version?: string; license?: string; licenseText?: string | null } = {},
): string {
  const packageDir = join(root, "node_modules", ...name.split("/"))
  mkdirSync(packageDir, { recursive: true })
  writeFileSync(
    join(packageDir, "package.json"),
    JSON.stringify({
      name,
      version: options.version ?? "1.0.0",
      license: options.license ?? "MIT",
    }),
  )
  if (options.licenseText !== null) {
    writeFileSync(join(packageDir, "LICENSE"), options.licenseText ?? `${name} licence text`)
  }
  return packageDir
}

describe("resolvePackageRoot", () => {
  test("finds the package directory for a plain package's deep module", () => {
    const moduleId = `${sep}repo${sep}node_modules${sep}scheduler${sep}cjs${sep}scheduler.js`

    expect(resolvePackageRoot(moduleId)).toBe(`${sep}repo${sep}node_modules${sep}scheduler`)
  })

  test("keeps both segments of a scoped package name", () => {
    const moduleId = `${sep}repo${sep}node_modules${sep}@scope${sep}pkg${sep}index.js`

    expect(resolvePackageRoot(moduleId)).toBe(`${sep}repo${sep}node_modules${sep}@scope${sep}pkg`)
  })

  test("resolves the innermost package for a nested node_modules install", () => {
    const moduleId = `${sep}repo${sep}node_modules${sep}outer${sep}node_modules${sep}inner${sep}index.js`

    expect(resolvePackageRoot(moduleId)).toBe(
      `${sep}repo${sep}node_modules${sep}outer${sep}node_modules${sep}inner`,
    )
  })

  test("returns null for application source outside node_modules", () => {
    expect(resolvePackageRoot(`${sep}repo${sep}web${sep}src${sep}app.tsx`)).toBeNull()
  })
})

describe("readPackageMetadata and findLicenseFile", () => {
  test("reads the declared name, version and license", () => {
    workspaceDir = mkdtempSync(join(tmpdir(), "third-party-licenses-"))
    const packageDir = fakePackage(workspaceDir, "left-pad", {
      version: "9.9.9",
      license: "WTFPL",
    })

    expect(readPackageMetadata(packageDir)).toEqual({
      name: "left-pad",
      version: "9.9.9",
      license: "WTFPL",
    })
  })

  test("returns null when the package has no package.json", () => {
    workspaceDir = mkdtempSync(join(tmpdir(), "third-party-licenses-"))

    expect(readPackageMetadata(join(workspaceDir, "node_modules", "missing"))).toBeNull()
  })

  test("finds a LICENSE spelled with the British suffix", () => {
    workspaceDir = mkdtempSync(join(tmpdir(), "third-party-licenses-"))
    const packageDir = fakePackage(workspaceDir, "brolly", { licenseText: null })
    writeFileSync(join(packageDir, "LICENCE.md"), "brolly licence text")

    expect(findLicenseFile(packageDir)).toBe(join(packageDir, "LICENCE.md"))
  })

  test("returns null when no licence file exists under any spelling", () => {
    workspaceDir = mkdtempSync(join(tmpdir(), "third-party-licenses-"))
    const packageDir = fakePackage(workspaceDir, "unlicensed-thing", { licenseText: null })

    expect(findLicenseFile(packageDir)).toBeNull()
  })
})

describe("collectBundledPackageRoots and buildThirdPartyLicenseReport", () => {
  test("collects one root per package name, deduplicating repeated module ids", () => {
    workspaceDir = mkdtempSync(join(tmpdir(), "third-party-licenses-"))
    const reactDir = fakePackage(workspaceDir, "react")
    fakePackage(workspaceDir, "@fontsource/ibm-plex-mono", { license: "OFL-1.1" })

    const bundle: BundleLike = {
      "index.js": {
        type: "chunk",
        moduleIds: [
          join(reactDir, "index.js"),
          join(reactDir, "cjs", "react.production.js"),
          join(workspaceDir, "node_modules", "@fontsource", "ibm-plex-mono", "index.css"),
          join(workspaceDir, "web", "src", "app.tsx"),
        ],
      },
      "font.woff2": { type: "asset", fileName: "font.woff2", source: "" } as BundleLike[string],
    }

    const roots = collectBundledPackageRoots(bundle)

    // The font package is excluded: its OFL-1.1 notice is pinned and tested separately.
    expect([...roots.keys()]).toEqual(["react"])
    expect(roots.get("react")).toBe(reactDir)
  })

  test("builds one sorted section per bundled package with its licence text", () => {
    workspaceDir = mkdtempSync(join(tmpdir(), "third-party-licenses-"))
    const reactDir = fakePackage(workspaceDir, "react", {
      version: "19.2.8",
      license: "MIT",
      licenseText: "MIT licence body",
    })
    const ajvDir = fakePackage(workspaceDir, "ajv", {
      version: "8.20.0",
      license: "MIT",
      licenseText: "ajv licence body",
    })

    const bundle: BundleLike = {
      "index.js": {
        type: "chunk",
        moduleIds: [join(ajvDir, "dist", "ajv.js"), join(reactDir, "index.js")],
      },
    }

    const report = buildThirdPartyLicenseReport(bundle)

    expect(report.missingLicenseFor).toEqual([])
    const ajvIndex = report.text.indexOf("ajv@8.20.0")
    const reactIndex = report.text.indexOf("react@19.2.8")
    expect(ajvIndex).toBeGreaterThanOrEqual(0)
    expect(reactIndex).toBeGreaterThan(ajvIndex)
    expect(report.text).toContain("SPDX-License-Identifier: MIT")
    expect(report.text).toContain("MIT licence body")
    expect(report.text).toContain("ajv licence body")
  })

  test("reports a bundled package with no licence file as missing, rather than dropping it silently", () => {
    workspaceDir = mkdtempSync(join(tmpdir(), "third-party-licenses-"))
    const packageDir = fakePackage(workspaceDir, "no-license-here", {
      version: "2.0.0",
      licenseText: null,
    })

    const bundle: BundleLike = {
      "index.js": { type: "chunk", moduleIds: [join(packageDir, "index.js")] },
    }

    const report = buildThirdPartyLicenseReport(bundle)

    expect(report.missingLicenseFor).toEqual(["no-license-here@2.0.0"])
  })
})
