# PillarMesh Console

The console is the operator-facing PillarMesh product application. Its browser application is a
React and TypeScript single-page application, and its Python server is a thin Starlette adapter.
Services remain authoritative for semantic validity, lifecycle state, execution, and evidence.

Node.js is pinned to `24.20.0` in `.node-version`. On a host with another Node.js version, run npm
through the pin:

```sh
npx --yes node@24.20.0 "$(command -v npm)" install
npx --yes node@24.20.0 "$(command -v npm)" run test -- --run
```

Run the Python shell from the repository root:

```sh
uv run uvicorn pillarmesh_console:create_app --factory --host 127.0.0.1 --port 8000
```

## Local demonstration

One command starts the loopback API and the Vite development server together, prints the local URL,
and names the backend mode so a demonstration can never be mistaken for a governed environment:

```sh
cd apps/console
npm run demo
```

The demonstration is fixture-backed. Every route carries a persistent `Demo scenario - no managed
effects` banner, and no command in it produces a managed effect.

## Production same-origin build

```sh
cd apps/console
npm run build
npm run serve:built            # serves the compiled bundle and the API from one origin
npm run serve:built -- --check # starts, asserts both, and exits
```

The check proves that a single Starlette origin returns the compiled application and
`GET /api/v1/session`. `create_app` admits a command only from the origin it was configured with,
which defaults to `http://127.0.0.1:8000`. To serve another port, set
`PILLARMESH_CONSOLE_ALLOWED_ORIGIN` to the origin the browser will actually use — the uvicorn
factory takes no arguments, so the environment is how the port reaches it, and a mismatch turns
every command into `same_origin_required`.

The compiled bundle executes under the Content-Security-Policy the server sends
(`default-src 'self'`). Response validation does not compile schemas in the browser: Ajv is run
at build time and `scripts/generate-contracts.mjs` writes standalone validator modules to
`web/src/api/generated-validators.js`, so nothing on the page needs `unsafe-eval`. `npm run
check:contracts` fails if those modules drift from `schema/console-api-v1.json`, and the browser
suite runs against the real policy rather than bypassing it.

## Browser acceptance, accessibility, and screenshots

```sh
cd apps/console
npx playwright install chromium   # once per machine
npm run test:e2e                  # journeys, axe audits, keyboard paths, screenshots
npm run test:e2e -- screenshots   # regenerate docs/screenshots/ only
```

The suite builds and serves the production bundle itself, so it exercises what the same-origin
server returns rather than the development proxy. Generated images and their known gaps are
documented in [`docs/screenshots/README.md`](docs/screenshots/README.md).

## Direct dependency review

The direct dependency versions and release ranges were reviewed against npm and PyPI metadata on
2026-09-01. None was marked deprecated, and each project had a current maintained release. The
licenses below are permissive or Apache-compatible. `@axe-core/playwright` is MPL-2.0; Mozilla's
license FAQ explicitly permits combining MPL-2.0 and Apache-licensed code. It is used unmodified as
test tooling, and its file-level terms remain attached to the installed package.

| Dependency | License | Purpose |
| --- | --- | --- |
| `pydantic` | MIT | Strict server-side presentation contracts |
| `starlette` | BSD-3-Clause | ASGI routes and responses |
| `uvicorn` | BSD-3-Clause | Loopback development ASGI server |
| `ajv` | MIT | Runtime validation of server responses |
| `react` | MIT | Browser component model |
| `react-dom` | MIT | Browser DOM renderer |
| `react-router-dom` | MIT | Explicit product route handling |
| `@axe-core/playwright` | MPL-2.0 | Browser accessibility verification |
| `@eslint/js` | MIT | Core JavaScript lint rules |
| `@playwright/test` | Apache-2.0 | Browser acceptance testing |
| `@testing-library/jest-dom` | MIT | Accessible DOM assertions |
| `@testing-library/react` | MIT | React behavior testing |
| `@testing-library/user-event` | MIT | User interaction simulation |
| `@types/node` | MIT | Node 24 declarations for Vite and Vitest configuration |
| `@types/react` | MIT | React TypeScript declarations |
| `@types/react-dom` | MIT | React DOM TypeScript declarations |
| `@vitejs/plugin-react` | MIT | React transform for Vite |
| `eslint` | MIT | Browser source linting |
| `eslint-plugin-react-hooks` | MIT | React Hooks lint rules |
| `eslint-plugin-react-refresh` | MIT | Safe React refresh exports |
| `jsdom` | MIT | DOM environment for unit tests |
| `json-schema-to-typescript` | MIT | Deterministic browser contract generation |
| `typescript` | Apache-2.0 | Static browser type checking |
| `typescript-eslint` | MIT | TypeScript-aware ESLint integration |
| `vite` | MIT | Browser development and production builds |
| `vitest` | MIT | Browser unit test runner |

Registry references: [npm](https://www.npmjs.com/), [PyPI](https://pypi.org/), and the
[MPL 2.0 FAQ](https://www.mozilla.org/en-US/MPL/2.0/FAQ/).
