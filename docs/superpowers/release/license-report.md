# Dependency license report

Private. Produced while implementing the dependency license policy test,
`tests/release/test_dependency_licenses.py`. Not part of the public release tree.

## 1. Container images

Heinzel does not redistribute any container image; every image below is pulled from its
upstream registry at run time by test emulator harnesses under `tests/emulators/`, never
bundled or re-pushed. The digest-pinned reference is a reproducibility control, not a
distribution.

Found with:

```sh
git grep -hoE '"[a-z0-9./-]+:[0-9][^"@]*@sha256:[0-9a-f]{64}"' -- '*.py' | sort -u
```

plus a manual check of `*.yml`/`*.yaml`/`Dockerfile*` for image references without a leading
digit in the tag, or without an `@sha256:` pin.

| Image | Used by | Upstream licence | Heinzel redistributes it? |
|---|---|---|---|
| `postgres:18.6-bookworm@sha256:33c86c9c...` | `tests/emulators/superset/compose.yaml`, `tests/emulators/warehouses/postgresql/compose.yaml` | PostgreSQL License (permissive, OSI-approved) | No |
| `clickhouse/clickhouse-server:25.8.32.4@sha256:7c39abeb...` | `tests/emulators/warehouses/clickhouse/compose.yaml` (referenced by digest in several `providers/clickhouse` tests) | Apache-2.0 | No |
| `apache/superset@sha256:1d1fdaae...` | `tests/emulators/superset/Dockerfile` | Apache-2.0 | No |
| `docker.getcollate.io/openmetadata/server@sha256:6c878281...` | `tests/emulators/openmetadata/compose.yaml` | Apache-2.0 | No |
| `docker.getcollate.io/openmetadata/ingestion@sha256:fe5effad...` | `tests/emulators/openmetadata/compose.yaml` | Apache-2.0 | No |
| `docker.getcollate.io/openmetadata/db@sha256:8a77669a...` | `tests/emulators/openmetadata/compose.yaml` | Wraps MySQL Community Server, GPL-2.0 | No |
| `docker.elastic.co/elasticsearch/elasticsearch@sha256:4f6bdcb7...` | `tests/emulators/openmetadata/compose.yaml` | **Elastic License 2.0 (not OSI-approved; flagged below)** | No |
| `localstack/snowflake:2026.06.0` | `tests/emulators/localstack-snowflake/compose.yaml` | **Proprietary / commercial LocalStack terms (requires `LOCALSTACK_AUTH_TOKEN`; flagged below)** | No |

### Flags for the future quickstart plan

- **`docker.elastic.co/elasticsearch/elasticsearch`** ships under the Elastic License 2.0,
  which is source-available but not OSI-approved and forbids offering it as a hosted/managed
  service. A future one-command quickstart that boots this image for an end user should say so
  explicitly, or substitute an Apache-2.0/SSPL-free search engine.
- **`localstack/snowflake`** is a paid LocalStack Pro image gated behind
  `LOCALSTACK_AUTH_TOKEN`. A public quickstart cannot assume every user holds that token; the
  Snowflake-emulation test path needs a documented opt-out or a free substitute for anyone
  outside this team.
- **`docker.getcollate.io/openmetadata/db`** wraps MySQL Community Server (GPL-2.0). Running it
  as a separate service is not itself a redistribution problem, but it is GPL, so any future
  packaging that bundles or forks this image (rather than pulling it from upstream) would need
  a fresh review.

No image here is under SSPL or AGPL.

## 2. Reviewed dependencies

Entries in `REVIEWED_PYTHON` and `REVIEWED_NPM` in
`tests/release/test_dependency_licenses.py`, each hand-checked because automatic
classification could not resolve them to "permitted" from installed metadata alone:

| Namespace | Name | Why it needed review | Resolution |
|---|---|---|---|
| Python | `text-unidecode` | PyPI classifiers list `GNU General Public License (GPL)` / `GPLv2+` alongside `Artistic License`; the installed `LICENSE.txt` states the package is dual-licensed "GPL or GPLv2+, or Artistic License" | Heinzel takes the Artistic License option, which is a permissive OSI-approved licence; not a GPL obligation |
| npm | `@fontsource-variable/ibm-plex-sans` | `license: "OFL-1.1"` (SIL Open Font License) is not on the generic permitted-terms list | Confirmed via the package's own `LICENSE` file; OFL-1.1 is a permissive, OSI/FSF-approved font licence with no copyleft effect on surrounding code |
| npm | `@fontsource/ibm-plex-mono` | Same as above | Same as above |
| npm | `caniuse-lite` | `license: "CC-BY-4.0"` is a data/content licence, not a generic code-license term | Confirmed via the package's own `LICENSE` file; CC-BY-4.0 only requires attribution, is a `devDependency` (browserslist data, not shipped in the built console), and imposes no copyleft on Heinzel's own code |

No `REVIEWED` entry hides a genuinely GPL-only, AGPL, or SSPL dependency; every entry above
resolves to a permissive licence once the actual grant (not just the PyPI/npm classifier
shorthand) is read.

## 3. Locked but not installed

Distributions that are present in `uv.lock` for another platform but not installed in this
`uv sync`'d environment, listed in `NOT_INSTALLED_HERE` in the test module:

| Distribution | Reason |
|---|---|
| `pywin32` | Windows-only dependency; this environment is not Windows |
| `tzdata` | Platform-conditional tzdata backport; not needed on this platform's Python |
| `httpx2-jsfetch` | Pyodide/browser-only transport shim; not installable outside that runtime |

Each is a legitimate platform-marker gap, not a missing dependency: `uv.lock` records their
license-relevant metadata for the platforms where they do install, and none of the three
appears in any forbidden-license list on PyPI.

## 4. Totals

- Python packages checked (locked, non-workspace, non-editable/virtual source): **117**
  (113 classified automatically as permitted, 1 resolved by hand via `REVIEWED_PYTHON`,
  3 not installed here and accounted for in section 3).
- npm packages checked (`apps/console/package-lock.json`, excluding the root package and
  `link` entries): **286** (283 classified directly, 3 resolved via `REVIEWED_NPM` above).
