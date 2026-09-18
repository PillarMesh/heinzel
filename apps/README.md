# Applications

`apps/` contains operator-facing product entry points. The only declared application boundary is `console/`: the operator console, a React and TypeScript single-page application with a Starlette server, documented in [console/README.md](console/README.md).

Control-plane capabilities belong in their named `services/` boundaries, not in a generic application backend.
