# Dependency license report

Private. Produced while implementing the dependency license policy test,
`tests/release/test_dependency_licenses.py`. Not part of the public release tree.

## 1. Container images

Heinzel does not redistribute any container image; every image below is pulled from its
upstream registry at run time, never bundled or re-pushed. The digest-pinned reference is a
reproducibility control, not a distribution. Several of these images are pinned and started by
product code under `providers/`, not only by the `tests/emulators/` test harnesses -- corrected
from an earlier draft of this report, which said only test emulators use them.

Found with:

```sh
git grep -hoE '"[a-z0-9./-]+:[0-9][^"@]*@sha256:[0-9a-f]{64}"' -- '*.py' | sort -u
```

plus a manual check of `*.yml`/`*.yaml`/`Dockerfile*` for image references without a leading
digit in the tag, or without an `@sha256:` pin.

| Image | Used by | Upstream licence | Heinzel redistributes it? |
|---|---|---|---|
| `postgres:18.6-bookworm@sha256:33c86c9c...` | `providers/postgresql/src/pillarmesh_provider_postgresql/warehouse_settings.py:8` (`POSTGRESQL_WAREHOUSE_IMAGE`, the runtime warehouse image); also `tests/emulators/superset/compose.yaml`, `tests/emulators/warehouses/postgresql/compose.yaml` | PostgreSQL License (permissive, OSI-approved) | No |
| `clickhouse/clickhouse-server:25.8.32.4@sha256:7c39abeb...` | `providers/clickhouse/src/pillarmesh_provider_clickhouse/settings.py:8` (`CLICKHOUSE_WAREHOUSE_IMAGE`), started at `providers/clickhouse/src/pillarmesh_provider_clickhouse/warehouse.py:3694`; also `tests/emulators/warehouses/clickhouse/compose.yaml` | Apache-2.0 | No |
| `apache/superset@sha256:1d1fdaae...` | `tests/emulators/superset/Dockerfile` | Apache-2.0 | No |
| `docker.getcollate.io/openmetadata/server@sha256:6c878281...` | `providers/openmetadata/src/pillarmesh_provider_openmetadata/client.py:49-57` (`CORE_UPSTREAM_IMAGES`/`UPSTREAM_IMAGES`, started by the runtime provider path); also `tests/emulators/openmetadata/compose.yaml` | Apache-2.0 | No |
| `docker.getcollate.io/openmetadata/ingestion@sha256:fe5effad...` | `providers/openmetadata/src/pillarmesh_provider_openmetadata/client.py:49-57` (`UPSTREAM_IMAGES`); also `tests/emulators/openmetadata/compose.yaml` | Apache-2.0 | No |
| `docker.getcollate.io/openmetadata/db@sha256:8a77669a...` | `providers/openmetadata/src/pillarmesh_provider_openmetadata/client.py:49-57` (`CORE_UPSTREAM_IMAGES`); also `tests/emulators/openmetadata/compose.yaml` | Wraps MySQL Community Server, GPL-2.0 | No |
| `docker.elastic.co/elasticsearch/elasticsearch@sha256:4f6bdcb7...` | `providers/openmetadata/src/pillarmesh_provider_openmetadata/client.py:49-57` (`CORE_UPSTREAM_IMAGES`) -- **started by a runtime provider code path, not only by a test harness**; also `tests/emulators/openmetadata/compose.yaml` | **Elastic License 2.0, binary image (flagged below)** | No |
| `localstack/snowflake:2026.06.0` | `tests/emulators/localstack-snowflake/compose.yaml` -- **not pinned by digest** (tag only, no `@sha256:`), unlike every other image in this table | **Proprietary / commercial LocalStack terms (requires `LOCALSTACK_AUTH_TOKEN`; flagged below)** | No |

### Flags for the future quickstart plan

- **`docker.elastic.co/elasticsearch/elasticsearch`** is started by a runtime provider path
  (`providers/openmetadata/src/pillarmesh_provider_openmetadata/client.py`), not only a test
  harness. The *binary* image is offered under the Elastic License 2.0 (ELv2), which is
  source-available but not OSI-approved and forbids offering it as a hosted/managed service.
  Confusingly, Elasticsearch's *source* is separately available under both AGPL-3.0 and SSPL --
  neither of those applies to this pulled binary image, but a future contributor reading
  Elasticsearch's own repository should not confuse the source licence with what this image
  ships under. A future one-command quickstart that boots this image for an end user should say
  so explicitly, or substitute an Apache-2.0/ELv2-free search engine.
- **`localstack/snowflake`** is a paid LocalStack Pro image gated behind
  `LOCALSTACK_AUTH_TOKEN`, and unlike every other image above it is **not pinned by digest** --
  only by a mutable tag (`2026.06.0`). A public quickstart cannot assume every user holds that
  token; the Snowflake-emulation test path needs a documented opt-out or a free substitute for
  anyone outside this team, and pinning by digest is a separate follow-up worth doing regardless.
- **`docker.getcollate.io/openmetadata/db`** wraps MySQL Community Server (GPL-2.0). Running it
  as a separate service is not itself a redistribution problem, but it is GPL, so any future
  packaging that bundles or forks this image (rather than pulling it from upstream) would need
  a fresh review.
- More generally, the userland inside any of these base images (each one's OS layer) contains
  GPL-licensed software (coreutils, bash, etc.). That does not matter today: nothing here is
  redistributed, only pulled and run, so no distribution obligation is triggered. It would matter
  the moment Heinzel starts shipping or re-hosting any of these images itself.

No image here is under SSPL or AGPL as the licence of what Heinzel actually pulls and runs.

## 2. Reviewed dependencies

Entries in `REVIEWED_PYTHON` and `REVIEWED_NPM` in
`tests/release/test_dependency_licenses.py`, each hand-checked because automatic
classification could not resolve them to "permitted" from installed metadata alone:

| Namespace | Name | Why it needed review | Resolution |
|---|---|---|---|
| Python | `text-unidecode` | PyPI classifiers list `GNU General Public License (GPL)` / `GPLv2+` alongside `Artistic License`; the installed `LICENSE.txt` states the package is dual-licensed "GPL or GPLv2+, or Artistic License" | OSI-approved Artistic-1.0 elected; not modified or redistributed. It arrives transitively as a runtime dependency (`text-unidecode` <- `python-slugify` <- `agate` <- the dbt stack), not a direct or optional one, so dropping or replacing it is impractical without replacing `agate` itself |
| npm | `@fontsource-variable/ibm-plex-sans` | `license: "OFL-1.1"` (SIL Open Font License) is not on the generic permitted-terms list | Confirmed via the package's own `LICENSE` file; OFL-1.1 is a permissive, OSI/FSF-approved font licence with no copyleft effect on surrounding code |
| npm | `@fontsource/ibm-plex-mono` | Same as above | Same as above |
| npm | `caniuse-lite` | `license: "CC-BY-4.0"` is a data/content licence, not a generic code-license term | Confirmed via the package's own `LICENSE` file; CC-BY-4.0 only requires attribution, is a `devDependency` (browserslist data, not shipped in the built console), and imposes no copyleft on Heinzel's own code |

No `REVIEWED` entry hides a genuinely GPL-only, AGPL, or SSPL dependency; every entry above
resolves to an OSI-approved, non-copyleft licence option once the actual grant (not just the
PyPI/npm classifier shorthand) is read.

## 3. Locked but not installed

Distributions that are present in `uv.lock` for another platform but not installed in this
`uv sync`'d environment, each hand-checked and recorded in `NOT_INSTALLED_HERE` in the test
module with its own upstream licence:

| Distribution | Licence | Reason |
|---|---|---|
| `pywin32` | PSF | Windows-only wheels (`win32`/`win_amd64`/`win_arm64`); this environment is not Windows |
| `tzdata` | Apache-2.0 | Every `uv.lock` marker for it reads `sys_platform == 'win32'` -- it is a Windows-only backport of the IANA tz database (Unix already ships one), not "not needed on this platform's Python" as an earlier draft of this report said |
| `httpx2-jsfetch` | BSD-3-Clause (per PyPI project metadata) | Every `uv.lock` marker for it reads `sys_platform == 'emscripten'` (Pyodide/browser-only transport) |

Each is a legitimate platform-marker gap, not a missing dependency. `uv.lock` itself does not
carry license metadata for any platform -- that correction replaces an earlier, inaccurate
claim in this report that it does. The licence for each package above was instead confirmed by
hand, by reading the package's own PyPI project metadata / classifiers, exactly as for every
other reviewed entry in this report.

## 4. IBM Plex fonts (OFL-1.1) and NOTICE obligations

`@fontsource-variable/ibm-plex-sans` and `@fontsource/ibm-plex-mono` (see the `REVIEWED_NPM`
table above) are runtime npm dependencies -- listed in `apps/console/package.json`'s
`dependencies`, not `devDependencies` -- and their font files are bundled into
`apps/console/dist` by the production build.

OFL-1.1 condition 2 requires that the copyright notice and licence text travel with any
redistributed copy of the font. A built `dist` that leaves this repository -- as a release
asset, or baked into a container image -- is such a copy, so the OFL text and copyright notice
need to travel with it. That obligation is now met: `THIRD_PARTY_NOTICES.md` names both
packages and their licence, and the upstream `LICENSE` text for each is copied byte for byte
into `apps/console/web/public/licenses/` so the production build ships it at
`dist/licenses/ibm-plex-sans-OFL.txt` and `dist/licenses/ibm-plex-mono-OFL.txt`.
`tests/release/test_third_party_notices.py` pins each shipped file to its package's locked
version and to a sha256 of the shipped text, so an npm upgrade fails that suite until the new
upstream licence text is reviewed and re-copied.

The equivalent obligation for the bundled JavaScript dependencies (react, react-dom,
react-router, scheduler, ajv, and any other bundled `node_modules` package, all MIT) is also
met: a build-time Vite plugin (`apps/console/build/third-party-licenses.ts`) collects the
licence text of every package actually bundled and emits it to
`dist/licenses/THIRD_PARTY.txt`, failing the build if a bundled package has no licence file to
ship.

## 5. Totals

- Python packages checked (locked, non-workspace, non-editable/virtual source): **117**
  (113 classified automatically as permitted, 1 resolved by hand via `REVIEWED_PYTHON`,
  3 not installed here and accounted for in section 3, each with its own hand-checked licence).
- npm packages checked (`apps/console/package-lock.json`, excluding the root package and
  `link` entries): **286** (283 classified directly, 3 resolved via `REVIEWED_NPM` above).
