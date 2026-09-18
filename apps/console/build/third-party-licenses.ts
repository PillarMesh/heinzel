// Minification strips the licence banners that react, react-dom, react-router, scheduler, and
// ajv would otherwise carry in the bundle. This plugin restores that notice out-of-band: it
// inspects the modules Rolldown actually bundled, resolves each one back to its owning
// `node_modules` package, and emits the concatenated licence texts as a build artifact. Hand
// rolled rather than pulled from `rollup-plugin-license`, because that plugin targets Rollup's
// module-graph APIs and Vite 8 bundles through Rolldown -- pulling it in would add a dependency
// on faith that its internals still line up, for something this small to write directly.
import { existsSync, readFileSync } from "node:fs"
import { join, sep } from "node:path"
import type { Rollup } from "vite"

type OutputAsset = Rollup.OutputAsset
type OutputChunk = Rollup.OutputChunk
type Plugin = Rollup.Plugin

export interface ThirdPartyPackageMetadata {
  readonly name: string
  readonly version: string
  readonly license: string
}

export type BundleLike = Record<string, Pick<OutputChunk, "type" | "moduleIds"> | OutputAsset>

// Font packages ship their OFL-1.1 notice through a separate, independently pinned and tested
// mechanism -- see THIRD_PARTY_NOTICES.md and tests/release/test_third_party_notices.py. Listing
// them again here would assert nothing further and would need its own pin to stay in sync.
const EXCLUDED_PACKAGES = new Set<string>([
  "@fontsource-variable/ibm-plex-sans",
  "@fontsource/ibm-plex-mono",
])

const LICENSE_FILENAMES = [
  "LICENSE",
  "LICENSE.md",
  "LICENSE.txt",
  "LICENCE",
  "LICENCE.md",
  "LICENCE.txt",
  "COPYING",
  "COPYING.md",
  "COPYING.txt",
]

const OUTPUT_FILE_NAME = "licenses/THIRD_PARTY.txt"

/**
 * The directory of the `node_modules` package that owns a bundled module id, or null when the
 * module id was not loaded from `node_modules` (application source, virtual modules, and so on).
 * Handles scoped packages (`@scope/name`) by keeping both path segments after `node_modules`.
 */
export function resolvePackageRoot(moduleId: string): string | null {
  const marker = `${sep}node_modules${sep}`
  const markerIndex = moduleId.lastIndexOf(marker)
  if (markerIndex === -1) {
    return null
  }

  const afterMarker = moduleId.slice(markerIndex + marker.length)
  const segments = afterMarker.split(sep).filter((segment) => segment.length > 0)
  if (segments.length === 0) {
    return null
  }

  const packageSegments = segments[0]?.startsWith("@") ? segments.slice(0, 2) : segments.slice(0, 1)
  if (packageSegments.length === 0 || packageSegments.some((segment) => !segment)) {
    return null
  }

  return moduleId.slice(0, markerIndex + marker.length) + packageSegments.join(sep)
}

/** The package name, version, and licence identifier declared by a package's own `package.json`. */
export function readPackageMetadata(packageRoot: string): ThirdPartyPackageMetadata | null {
  const packageJsonPath = join(packageRoot, "package.json")
  if (!existsSync(packageJsonPath)) {
    return null
  }

  const raw = JSON.parse(readFileSync(packageJsonPath, "utf-8")) as {
    name?: string
    version?: string
    license?: string | { type?: string }
    licenses?: Array<{ type?: string }>
  }
  if (!raw.name || !raw.version) {
    return null
  }

  let license = "UNKNOWN"
  if (typeof raw.license === "string") {
    license = raw.license
  } else if (raw.license && typeof raw.license.type === "string") {
    license = raw.license.type
  } else if (raw.licenses?.[0]?.type) {
    license = raw.licenses[0].type
  }

  return { name: raw.name, version: raw.version, license }
}

/** The path to a package's licence file, trying every filename spelling npm packages actually use. */
export function findLicenseFile(packageRoot: string): string | null {
  for (const filename of LICENSE_FILENAMES) {
    const candidate = join(packageRoot, filename)
    if (existsSync(candidate)) {
      return candidate
    }
  }
  return null
}

/**
 * Every distinct `node_modules` package backing a module in the bundle's chunks, keyed by
 * package name, first package root seen wins. Assets (fonts, images, the emitted licence file
 * itself) carry no module ids and are not inspected.
 */
export function collectBundledPackageRoots(bundle: BundleLike): Map<string, string> {
  const roots = new Map<string, string>()

  for (const item of Object.values(bundle)) {
    if (item.type !== "chunk") {
      continue
    }
    for (const moduleId of item.moduleIds) {
      const packageRoot = resolvePackageRoot(moduleId)
      if (!packageRoot) {
        continue
      }
      const metadata = readPackageMetadata(packageRoot)
      if (!metadata || EXCLUDED_PACKAGES.has(metadata.name) || roots.has(metadata.name)) {
        continue
      }
      roots.set(metadata.name, packageRoot)
    }
  }

  return roots
}

export interface ThirdPartyLicenseReport {
  readonly text: string
  readonly missingLicenseFor: readonly string[]
}

/**
 * The concatenated third-party licence text for a bundle, one section per package sorted by
 * name, plus the `name@version` of every package whose owning directory has no licence file.
 * A non-empty `missingLicenseFor` means the caller must fail the build: shipping code without
 * its licence text is not a state to emit output for and move on from.
 */
export function buildThirdPartyLicenseReport(bundle: BundleLike): ThirdPartyLicenseReport {
  const roots = collectBundledPackageRoots(bundle)
  const sections: string[] = []
  const missingLicenseFor: string[] = []

  for (const name of [...roots.keys()].sort((a, b) => a.localeCompare(b))) {
    const packageRoot = roots.get(name)
    if (!packageRoot) {
      continue
    }
    const metadata = readPackageMetadata(packageRoot)
    if (!metadata) {
      continue
    }
    const licenseFile = findLicenseFile(packageRoot)
    if (!licenseFile) {
      missingLicenseFor.push(`${metadata.name}@${metadata.version}`)
      continue
    }

    const licenseText = readFileSync(licenseFile, "utf-8").trimEnd()
    const divider = "=".repeat(80)
    sections.push(
      `${divider}\n${metadata.name}@${metadata.version}\nSPDX-License-Identifier: ${metadata.license}\n${divider}\n\n${licenseText}\n`,
    )
  }

  return { text: sections.join("\n"), missingLicenseFor }
}

/**
 * Emits `licenses/THIRD_PARTY.txt` into the build output with the licence text of every bundled
 * `node_modules` package, and fails the build if any bundled package has no licence file to
 * ship -- an omission here is a licence-compliance gap, not a warning.
 */
export function thirdPartyLicensesPlugin(): Plugin {
  return {
    name: "heinzel:third-party-licenses",
    generateBundle(_outputOptions, bundle) {
      const report = buildThirdPartyLicenseReport(bundle)

      if (report.missingLicenseFor.length > 0) {
        this.error(
          `third-party-licenses: no licence file found for: ${report.missingLicenseFor.join(", ")}`,
        )
      }

      this.emitFile({
        type: "asset",
        fileName: OUTPUT_FILE_NAME,
        source: report.text,
      })
    },
  }
}
