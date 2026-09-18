// Minification strips the licence banners that react, react-dom, react-router, scheduler, and
// ajv would otherwise carry in the bundle. This plugin restores that notice out-of-band: it
// inspects the modules and assets Rolldown actually bundled, resolves each one back to its
// owning `node_modules` package, and emits the concatenated licence texts as a build artifact.
// Hand rolled rather than pulled from `rollup-plugin-license`, because that plugin targets
// Rollup's module-graph APIs and Vite 8 bundles through Rolldown -- pulling it in would add a
// dependency on faith that its internals still line up, for something this small to write
// directly.
import { existsSync, readFileSync } from "node:fs"
import { posix, resolve } from "node:path"
import type { Plugin, Rollup } from "vite"

type OutputAsset = Rollup.OutputAsset
type OutputChunk = Rollup.OutputChunk

export interface ThirdPartyPackageMetadata {
  readonly name: string
  readonly version: string
  readonly license: string
}

export type BundleLike = Record<
  string,
  Pick<OutputChunk, "type" | "moduleIds"> | Pick<OutputAsset, "type" | "originalFileNames">
>

// Font packages ship their OFL-1.1 notice through a separate, independently pinned and tested
// mechanism -- see THIRD_PARTY_NOTICES.md and tests/release/test_third_party_notices.py. Listing
// them again here would assert nothing further and would need its own pin to stay in sync. This
// is the only place a bundled package is skipped outright; every other package that resolves to
// a node_modules root is either reported with its licence or reported as missing one.
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

const NODE_MODULES_PATH_PATTERN = /[\\/]node_modules[\\/]/

type AssetsInlineLimit = number | ((filePath: string, content: Buffer) => boolean | undefined)

/**
 * The directory of the `node_modules` package that owns a bundled path (a module id, or an
 * asset's original file path), or null when the path was not loaded from `node_modules`
 * (application source, virtual modules, and so on).
 *
 * Normalises `\` to `/` and splits on `/` rather than the host `path.sep`, so a path recorded
 * with Windows separators (as Rolldown's `originalFileNames` can carry, and as a module id can
 * carry when this plugin runs on Windows) resolves the same way as its POSIX equivalent instead
 * of silently matching nothing. The returned root is itself `/`-joined for the same reason, and
 * every other lookup in this module builds on it with `path.posix`, never a bare OS `path.join`.
 * Handles scoped packages (`@scope/name`) by keeping both path segments after `node_modules`,
 * and a path with a nested `node_modules` resolves to the innermost package, since that is the
 * one actually bundled.
 */
export function resolvePackageRoot(rawPath: string): string | null {
  const segments = rawPath.replaceAll("\\", "/").split("/")
  const markerIndex = segments.lastIndexOf("node_modules")
  if (markerIndex === -1 || markerIndex === segments.length - 1) {
    return null
  }

  const nameStart = markerIndex + 1
  const firstNameSegment = segments[nameStart]
  if (!firstNameSegment) {
    return null
  }
  const nameSegments = firstNameSegment.startsWith("@")
    ? segments.slice(nameStart, nameStart + 2)
    : segments.slice(nameStart, nameStart + 1)
  if (nameSegments.length === 0 || nameSegments.some((segment) => !segment)) {
    return null
  }

  return [...segments.slice(0, nameStart), ...nameSegments].join("/")
}

/** The npm package name a resolved package root ends with, read back out of the path itself. */
function packageNameFromRoot(packageRoot: string): string | null {
  const segments = packageRoot.split("/")
  const markerIndex = segments.lastIndexOf("node_modules")
  if (markerIndex === -1 || markerIndex === segments.length - 1) {
    return null
  }
  return segments.slice(markerIndex + 1).join("/")
}

/** The package name, version, and licence identifier declared by a package's own `package.json`. */
export function readPackageMetadata(packageRoot: string): ThirdPartyPackageMetadata | null {
  const packageJsonPath = posix.join(packageRoot, "package.json")
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
    const candidate = posix.join(packageRoot, filename)
    if (existsSync(candidate)) {
      return candidate
    }
  }
  return null
}

/**
 * Every distinct `node_modules` package root backing the bundle: a module id in a chunk, or an
 * asset's `originalFileNames` (fonts, images, and other files pulled in through a CSS `url()`,
 * which carry no module ids and were previously never inspected) resolved against the project
 * root. Keyed by package root rather than package name, so two different install locations of
 * the same package -- and so, in practice, two different versions -- are both kept rather than
 * one silently shadowing the other.
 */
export function collectBundledPackageRoots(bundle: BundleLike, root: string): Set<string> {
  const packageRoots = new Set<string>()

  const consider = (rawPath: string): void => {
    const packageRoot = resolvePackageRoot(rawPath)
    if (!packageRoot) {
      return
    }
    const name = packageNameFromRoot(packageRoot)
    if (name && EXCLUDED_PACKAGES.has(name)) {
      return
    }
    packageRoots.add(packageRoot)
  }

  for (const item of Object.values(bundle)) {
    if (item.type === "chunk") {
      for (const moduleId of item.moduleIds) {
        consider(moduleId)
      }
    } else if (item.type === "asset") {
      for (const originalFileName of item.originalFileNames) {
        consider(resolve(root, originalFileName))
      }
    }
  }

  return packageRoots
}

/**
 * Wraps a user's `build.assetsInlineLimit` so nothing under `node_modules` is ever inlined as a
 * data URI. Vite's default limit is 4 KB: below that, a small icon or font pulled in through a
 * CSS `url()` from a package with no licence would be inlined straight into a chunk and never
 * appear in the bundle as an asset with `originalFileNames` -- the only place
 * `collectBundledPackageRoots` can see it -- so it would ship with no licence check at all.
 *
 * Delegates to the user's own setting for everything outside `node_modules`: calls it if it is a
 * function, compares the content length if it is a number, and returns `undefined` (Vite's own
 * default then applies) if the user set nothing.
 */
export function createAssetsInlineLimit(
  userLimit: AssetsInlineLimit | undefined,
): (filePath: string, content: Buffer) => boolean | undefined {
  return (filePath, content) => {
    if (NODE_MODULES_PATH_PATTERN.test(filePath)) {
      return false
    }
    if (typeof userLimit === "function") {
      return userLimit(filePath, content)
    }
    if (typeof userLimit === "number") {
      return content.length < userLimit
    }
    return undefined
  }
}

export interface ThirdPartyLicenseReport {
  readonly text: string
  readonly missingLicenseFor: readonly string[]
}

/**
 * The concatenated third-party licence text for a bundle, one section per package root sorted
 * by name (falling back to the root path when a package has no readable name), plus an entry
 * identifying every package that cannot ship a licence: the package root itself when its
 * `package.json` is missing or incomplete, or `name@version` when metadata was readable but no
 * licence file exists. Either case means the caller must fail the build -- shipping code with no
 * traceable licence, or with metadata too broken to identify, is not a state to emit output for
 * and move on from.
 */
export function buildThirdPartyLicenseReport(bundle: BundleLike, root: string): ThirdPartyLicenseReport {
  const packageRoots = collectBundledPackageRoots(bundle, root)
  const entries = [...packageRoots].map((packageRoot) => ({
    packageRoot,
    metadata: readPackageMetadata(packageRoot),
  }))
  entries.sort((a, b) =>
    (a.metadata?.name ?? a.packageRoot).localeCompare(b.metadata?.name ?? b.packageRoot),
  )

  const sections: string[] = []
  const missingLicenseFor: string[] = []

  for (const { packageRoot, metadata } of entries) {
    if (!metadata) {
      missingLicenseFor.push(packageRoot)
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
 * `node_modules` package (from chunks and from assets alike), and fails the build if any bundled
 * package cannot ship a licence -- an omission here is a licence-compliance gap, not a warning.
 * Also forces `build.assetsInlineLimit` to never inline a `node_modules` asset as a data URI, so
 * a small icon or font stays a real asset this plugin can inspect instead of disappearing into a
 * chunk unexamined.
 *
 * Only the main build is registered (see `vite.config.ts`); a `worker` build has its own,
 * separate `plugins` array (`build.rollupOptions`/`worker.plugins`), so this plugin must be added
 * there too if the console ever gains a web worker that bundles third-party code.
 */
export function thirdPartyLicensesPlugin(): Plugin {
  let projectRoot = process.cwd()

  return {
    name: "heinzel:third-party-licenses",
    config(config) {
      return {
        build: {
          assetsInlineLimit: createAssetsInlineLimit(config.build?.assetsInlineLimit),
        },
      }
    },
    configResolved(config) {
      projectRoot = config.root
    },
    generateBundle(_outputOptions, bundle) {
      const report = buildThirdPartyLicenseReport(bundle, projectRoot)

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
