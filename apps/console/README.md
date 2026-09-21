# Heinzel Console

The console is the operator-facing Heinzel product application. Its browser application is a
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
uv run uvicorn heinzel_console:create_app --factory --host 127.0.0.1 --port 8000
```

## Exposure

The console server has no authentication. Unless a caller of `create_app` supplies its own backend
and actor context, it serves the in-memory fixture backend and treats every request as one fixed
actor, a data architect in `tenant-primary` (see `server/src/heinzel_console/app.py`). The
commands in this README and the npm scripts bind it to `127.0.0.1`, which is also uvicorn's
default, and the Vite development server listens on `localhost`. Keep it on loopback: do not bind
it to another interface or expose it to a network.

## Local demonstration

One command starts the loopback API and the Vite development server together, prints the local URL,
and names the backend mode so a demonstration can never be mistaken for a governed environment:

```sh
cd apps/console
npm run demo
```

The demonstration is fixture-backed. Every route carries a persistent `Demo scenario - no managed
effects` banner, and no command in it produces a managed effect.

## The `heinzel-console` command

`heinzel-console serve` runs a demonstration against a state directory that persists between
runs, without Node.js:

```sh
uv run heinzel-console serve --state-dir ./console-state
uv run heinzel-console serve --state-dir ./console-state --port 8731 --dist apps/console/dist
```

It has no authentication: whoever reaches it acts as the data architect. It therefore binds
`127.0.0.1` and refuses any other host. `--container` binds every interface instead, and is
meant only for a container image whose published port is the network boundary.

Because it has no authentication, a command is accepted only from the one origin the console is
configured with, given by `--origin` or `HEINZEL_CONSOLE_ALLOWED_ORIGIN`. That origin is the one
the browser will use, which is not the address the server binds: under `--container` the bind
address is `0.0.0.0`, which no browser sends, and the reader opens the published port rather than
the bound one. A container given neither setting refuses to start rather than serve a console
that renders every page and refuses every command. Give the origin exactly as the browser sends
it — no trailing slash, path, surrounding space, uppercase, or default port, because a browser
leaves `:80` and `:443` out of the header it sends, and writes an address literal one way — for
example `http://127.0.0.1:8731`, or `http://127.0.0.1` for a console published on port 80. Any
other spelling is refused rather than silently adjusted: where the value differs only in one of
those correctable ways the refusal names the spelling to use instead, and where no browser sends
the value in any spelling — a credential, a scheme other than `http` or `https`, no host, a
non-ASCII name a browser would send as punycode, or a port no browser sends — the refusal says
which of those it is. The accepted origin is printed at startup, and that line is the one to
read when a browser opened on another port has every command refused `same_origin_required`.

`--dist` serves a compiled bundle alongside the API from this one origin, and follows
`HEINZEL_CONSOLE_DIST` when it is omitted. `--no-seed` starts with an empty inbox instead of the
demonstration's own question.

## Governed local UI testing

For separate requester and architect browser sessions backed by the owning services
and disposable SQLite state, run:

```sh
cd apps/console
npm run test:e2e:governed
```

This builds the bundle and exercises persisted intake, conversation, review, approval, and
admission.

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
`HEINZEL_CONSOLE_ALLOWED_ORIGIN` to the origin the browser will actually use — the uvicorn
factory takes no arguments, so the environment is how the port reaches it, and a mismatch turns
every command into `same_origin_required`.

Managed-service links are disabled unless `HEINZEL_CONSOLE_MANAGED_LINK_ORIGIN` names one exact
HTTPS origin. Configure its canonical ASCII hostname, including an explicit non-default port when
needed. Internationalized hostnames must use their ASCII `xn--` form. The server reauthorizes an
opaque link reference before redirecting and rejects every absolute target outside that origin;
configuring the origin alone does not enable dashboard links or supply managed-service SSO.

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
test tooling, and its file-level terms remain attached to the installed package. The two IBM Plex
packages are SIL Open Font License 1.1; only their latin `woff2` files are referenced, so the build
copies three font files and no stylesheet from either package. They are bundled rather than fetched
because the server sends `default-src 'self'`, and so that the interface does not depend on a
platform-specific system font.

| Dependency | License | Purpose |
| --- | --- | --- |
| `pydantic` | MIT | Strict server-side presentation contracts |
| `starlette` | BSD-3-Clause | ASGI routes and responses |
| `uvicorn` | BSD-3-Clause | Loopback development ASGI server |
| `ajv` | MIT | Runtime validation of server responses |
| `@fontsource-variable/ibm-plex-sans` | OFL-1.1 | Self-hosted interface typeface |
| `@fontsource/ibm-plex-mono` | OFL-1.1 | Self-hosted typeface for identifiers and digests |
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
