// The brand marks live in one place. Two surfaces need their own copy of some of them, because
// each is a separate deployment unit with its own delivery: the console bundles its copy into
// static output, and the dashboard chrome's copy is mounted into a container that serves no
// files of ours. A copy is a thing that drifts, so this fails when one has -- byte for byte,
// since artwork is not something to compare by eye.
import {readFileSync} from "node:fs"
import {fileURLToPath} from "node:url"
import {dirname, join, relative} from "node:path"

const repository = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "..")
const source = join(repository, "brand", "heinzel")

const copies = [
  ["apps/console/web/public/brand", ["apple-touch-icon.png", "favicon.ico", "favicon.svg", "heinzel-horizontal-color.svg", "heinzel-horizontal-reverse.svg", "heinzel-symbol-color.svg"]],
  ["deploy/quickstart/superset/branding", ["heinzel-horizontal-color.svg", "heinzel-horizontal-reverse.svg", "heinzel-symbol-color.svg"]],
]

const drifted = []
for (const [directory, names] of copies) {
  for (const name of names) {
    const expected = readFileSync(join(source, name))
    let actual = null
    try {
      actual = readFileSync(join(repository, directory, name))
    } catch {
      drifted.push(`${directory}/${name} is missing`)
      continue
    }
    if (!expected.equals(actual)) {
      drifted.push(`${directory}/${name} differs from brand/heinzel/${name}`)
    }
  }
}

if (drifted.length > 0) {
  process.stderr.write(`brand copies have drifted from ${relative(repository, source)}:\n`)
  for (const line of drifted) process.stderr.write(`  ${line}\n`)
  process.stderr.write("Copy from brand/heinzel/ rather than editing a copy.\n")
  process.exit(1)
}
process.stdout.write(`brand copies match brand/heinzel (${copies.reduce((n, [, names]) => n + names.length, 0)} files)\n`)
