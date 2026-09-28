# Changelog

All notable changes to Heinzel are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Initial public release of Heinzel under the Apache License 2.0.
- A Docker Compose quickstart that builds one container and serves the demonstration console on
  `127.0.0.1:8000`, or on another port through `HEINZEL_PORT`.
- A `heinzel-console` command that serves the demonstration console from a state directory.

### Changed

- The declared minimum versions of the runtime dependencies now match what the lockfile actually
  resolves: `cryptography` 50.0.1, `pydantic` 2.13.5, `psycopg` 3.3.6 and `mcp` 2.2.0. Several had
  drifted several releases behind the versions every test ran against.

### Fixed

- The authoring and context-exposure MCP servers again report *why* they refused a result. Their
  leakage guards raised `RuntimeError`, which `mcp` 2.2.0 classifies as a crash: it replaces the
  message with a bare `Error executing tool <name>` and keeps the reason server-side. They now
  raise `ToolError`, the classification these deliberate refusals always warranted. Nothing about
  what is withheld changed -- the guard still refuses before any value is returned.
