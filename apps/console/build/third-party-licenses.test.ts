import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { afterEach, describe, expect, test, vi } from "vitest"

import {
  buildThirdPartyLicenseReport,
  collectBundledPackageRoots,
  createAssetsInlineLimit,
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
  options: {
    version?: string
    license?: string
    licenseText?: string | null
    packageJson?: Record<string, unknown> | null
  } = {},
): string {
  const packageDir = join(root, "node_modules", ...name.split("/"))
  mkdirSync(packageDir, { recursive: true })
  if (options.packageJson !== null) {
    const packageJson = options.packageJson ?? {
      name,
      version: options.version ?? "1.0.0",
      license: options.license ?? "MIT",
    }
    writeFileSync(join(packageDir, "package.json"), JSON.stringify(packageJson))
  }
  if (options.licenseText !== null) {
    writeFileSync(join(packageDir, "LICENSE"), options.licenseText ?? `${name} licence text`)
  }
  return packageDir
}

/** A minimal chunk entry for BundleLike, the shape collectBundledPackageRoots reads. */
function chunk(moduleIds: string[]): BundleLike[string] {
  return { type: "chunk", moduleIds }
}

/** A minimal asset entry for BundleLike, the shape collectBundledPackageRoots reads. */
function asset(originalFileNames: string[]): BundleLike[string] {
  return { type: "asset", originalFileNames }
}

describe("resolvePackageRoot", () => {
  test("finds the package directory for a plain package's deep module", () => {
    const moduleId = "/repo/node_modules/scheduler/cjs/scheduler.js"

    expect(resolvePackageRoot(moduleId)).toBe("/repo/node_modules/scheduler")
  })

  test("keeps both segments of a scoped package name", () => {
    const moduleId = "/repo/node_modules/@scope/pkg/index.js"

    expect(resolvePackageRoot(moduleId)).toBe("/repo/node_modules/@scope/pkg")
  })

  test("resolves the innermost package for a nested node_modules install", () => {
    const moduleId = "/repo/node_modules/outer/node_modules/inner/index.js"

    expect(resolvePackageRoot(moduleId)).toBe("/repo/node_modules/outer/node_modules/inner")
  })

  test("returns null for application source outside node_modules", () => {
    expect(resolvePackageRoot("/repo/web/src/app.tsx")).toBeNull()
  })

  test("resolves a Windows-style backslash path the same as its forward-slash equivalent", () => {
    const moduleId = "C:\\repo\\node_modules\\@scope\\pkg\\dist\\index.js"

    expect(resolvePackageRoot(moduleId)).toBe("C:/repo/node_modules/@scope/pkg")
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

  test("returns null when package.json has no name or version", () => {
    workspaceDir = mkdtempSync(join(tmpdir(), "third-party-licenses-"))
    const packageDir = fakePackage(workspaceDir, "half-described", {
      packageJson: { license: "MIT" },
    })

    expect(readPackageMetadata(packageDir)).toBeNull()
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

describe("collectBundledPackageRoots", () => {
  test("collects one root per package, deduplicating repeated module ids", () => {
    workspaceDir = mkdtempSync(join(tmpdir(), "third-party-licenses-"))
    const reactDir = fakePackage(workspaceDir, "react")
    fakePackage(workspaceDir, "@fontsource/ibm-plex-mono", { license: "OFL-1.1" })

    const bundle: BundleLike = {
      "index.js": chunk([
        join(reactDir, "index.js"),
        join(reactDir, "cjs", "react.production.js"),
        join(workspaceDir, "node_modules", "@fontsource", "ibm-plex-mono", "index.css"),
        join(workspaceDir, "web", "src", "app.tsx"),
      ]),
    }

    const roots = collectBundledPackageRoots(bundle, workspaceDir)

    // The font package is excluded: its OFL-1.1 notice is pinned and tested separately.
    expect([...roots]).toEqual([reactDir])
  })

  test("keeps two different install locations of the same package name as two roots", () => {
    workspaceDir = mkdtempSync(join(tmpdir(), "third-party-licenses-"))
    const hoistedDir = fakePackage(workspaceDir, "left-pad", { version: "1.0.0" })
    const nestedDir = join(workspaceDir, "node_modules", "consumer", "node_modules", "left-pad")
    mkdirSync(nestedDir, { recursive: true })
    writeFileSync(
      join(nestedDir, "package.json"),
      JSON.stringify({ name: "left-pad", version: "2.0.0", license: "MIT" }),
    )
    writeFileSync(join(nestedDir, "LICENSE"), "left-pad 2.0.0 licence text")

    const bundle: BundleLike = {
      "index.js": chunk([join(hoistedDir, "index.js"), join(nestedDir, "index.js")]),
    }

    const roots = collectBundledPackageRoots(bundle, workspaceDir)

    expect(roots.size).toBe(2)
    expect(roots.has(hoistedDir)).toBe(true)
    expect(roots.has(nestedDir)).toBe(true)
  })

  test("resolves an asset's originalFileNames against the project root and finds its package", () => {
    workspaceDir = mkdtempSync(join(tmpdir(), "third-party-licenses-"))
    const fontDir = fakePackage(workspaceDir, "some-font-package", {
      version: "3.0.0",
      license: "OFL-1.1",
    })
    const fontFile = join(fontDir, "files", "some-font.woff2")
    mkdirSync(join(fontDir, "files"), { recursive: true })
    writeFileSync(fontFile, "binary-ish font bytes")

    const bundle: BundleLike = {
      "assets/some-font.woff2": asset([fontFile]),
    }

    const roots = collectBundledPackageRoots(bundle, workspaceDir)

    expect([...roots]).toEqual([fontDir])
  })

  test("still excludes a font asset whose originalFileNames points into an excluded package", () => {
    workspaceDir = mkdtempSync(join(tmpdir(), "third-party-licenses-"))
    const fontDir = fakePackage(workspaceDir, "@fontsource/ibm-plex-mono", {
      version: "5.3.0",
      license: "OFL-1.1",
    })
    const fontFile = join(fontDir, "files", "ibm-plex-mono.woff2")
    mkdirSync(join(fontDir, "files"), { recursive: true })
    writeFileSync(fontFile, "binary-ish font bytes")

    const bundle: BundleLike = {
      "assets/ibm-plex-mono.woff2": asset([fontFile]),
    }

    const roots = collectBundledPackageRoots(bundle, workspaceDir)

    expect(roots.size).toBe(0)
  })
})

describe("buildThirdPartyLicenseReport", () => {
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
      "index.js": chunk([join(ajvDir, "dist", "ajv.js"), join(reactDir, "index.js")]),
    }

    const report = buildThirdPartyLicenseReport(bundle, workspaceDir)

    expect(report.missingLicenseFor).toEqual([])
    const ajvIndex = report.text.indexOf("ajv@8.20.0")
    const reactIndex = report.text.indexOf("react@19.2.8")
    expect(ajvIndex).toBeGreaterThanOrEqual(0)
    expect(reactIndex).toBeGreaterThan(ajvIndex)
    expect(report.text).toContain("SPDX-License-Identifier: MIT")
    expect(report.text).toContain("MIT licence body")
    expect(report.text).toContain("ajv licence body")
  })

  test("reports both install locations of a duplicated package, each with its own version", () => {
    workspaceDir = mkdtempSync(join(tmpdir(), "third-party-licenses-"))
    const hoistedDir = fakePackage(workspaceDir, "left-pad", {
      version: "1.0.0",
      licenseText: "left-pad 1.0.0 licence text",
    })
    const nestedDir = join(workspaceDir, "node_modules", "consumer", "node_modules", "left-pad")
    mkdirSync(nestedDir, { recursive: true })
    writeFileSync(
      join(nestedDir, "package.json"),
      JSON.stringify({ name: "left-pad", version: "2.0.0", license: "MIT" }),
    )
    writeFileSync(join(nestedDir, "LICENSE"), "left-pad 2.0.0 licence text")

    const bundle: BundleLike = {
      "index.js": chunk([join(hoistedDir, "index.js"), join(nestedDir, "index.js")]),
    }

    const report = buildThirdPartyLicenseReport(bundle, workspaceDir)

    expect(report.missingLicenseFor).toEqual([])
    expect(report.text).toContain("left-pad 1.0.0 licence text")
    expect(report.text).toContain("left-pad 2.0.0 licence text")
    expect(report.text.match(/left-pad@1\.0\.0/g)).toHaveLength(1)
    expect(report.text.match(/left-pad@2\.0\.0/g)).toHaveLength(1)
  })

  test("reports a bundled package with no licence file as missing, rather than dropping it silently", () => {
    workspaceDir = mkdtempSync(join(tmpdir(), "third-party-licenses-"))
    const packageDir = fakePackage(workspaceDir, "no-license-here", {
      version: "2.0.0",
      licenseText: null,
    })

    const bundle: BundleLike = { "index.js": chunk([join(packageDir, "index.js")]) }

    const report = buildThirdPartyLicenseReport(bundle, workspaceDir)

    expect(report.missingLicenseFor).toEqual(["no-license-here@2.0.0"])
  })

  test("reports a bundled package with no package.json as missing, naming the package root", () => {
    workspaceDir = mkdtempSync(join(tmpdir(), "third-party-licenses-"))
    const packageDir = join(workspaceDir, "node_modules", "no-metadata-here")
    mkdirSync(packageDir, { recursive: true })
    writeFileSync(join(packageDir, "index.js"), "module.exports = {}")

    const bundle: BundleLike = { "index.js": chunk([join(packageDir, "index.js")]) }

    const report = buildThirdPartyLicenseReport(bundle, workspaceDir)

    expect(report.missingLicenseFor).toEqual([packageDir])
    expect(report.text).toBe("")
  })

  test("reports a bundled package with an incomplete package.json as missing, not silently dropped", () => {
    workspaceDir = mkdtempSync(join(tmpdir(), "third-party-licenses-"))
    const packageDir = fakePackage(workspaceDir, "half-described", {
      packageJson: { license: "MIT" },
    })

    const bundle: BundleLike = { "index.js": chunk([join(packageDir, "index.js")]) }

    const report = buildThirdPartyLicenseReport(bundle, workspaceDir)

    expect(report.missingLicenseFor).toEqual([packageDir])
  })
})

describe("createAssetsInlineLimit", () => {
  test("never inlines a node_modules asset, no matter how generous the user's own limit is", () => {
    const limit = createAssetsInlineLimit(Number.MAX_SAFE_INTEGER)

    expect(limit("/repo/node_modules/some-pkg/icon.svg", Buffer.from("x"))).toBe(false)
  })

  test("never inlines a node_modules asset even when the user's own limit is a function that says yes", () => {
    const limit = createAssetsInlineLimit(() => true)

    expect(limit("/repo/node_modules/some-pkg/icon.svg", Buffer.from("x"))).toBe(false)
  })

  test("falls back to the user's numeric limit for a path outside node_modules", () => {
    const limit = createAssetsInlineLimit(10)

    expect(limit("/repo/web/src/logo.svg", Buffer.alloc(5))).toBe(true)
    expect(limit("/repo/web/src/logo.svg", Buffer.alloc(20))).toBe(false)
  })

  test("delegates to the user's function limit for a path outside node_modules", () => {
    const userLimit = vi.fn(() => true)
    const limit = createAssetsInlineLimit(userLimit)
    const content = Buffer.from("abc")

    expect(limit("/repo/web/src/logo.svg", content)).toBe(true)
    expect(userLimit).toHaveBeenCalledWith("/repo/web/src/logo.svg", content)
  })

  test("returns undefined outside node_modules when the user set no limit, deferring to Vite's default", () => {
    const limit = createAssetsInlineLimit(undefined)

    expect(limit("/repo/web/src/logo.svg", Buffer.from("x"))).toBeUndefined()
  })

  test("matches a node_modules path using Windows-style backslash separators", () => {
    const limit = createAssetsInlineLimit(undefined)

    expect(limit("C:\\repo\\node_modules\\some-pkg\\icon.svg", Buffer.from("x"))).toBe(false)
  })
})
