# Security Policy

## Reporting a Vulnerability

Do not open a public issue for a suspected vulnerability or include secrets, customer data, credentials, or exploit details in public discussions.

Use GitHub's private vulnerability-reporting feature for this repository. If that feature is unavailable, contact the repository owners privately through the organization before sharing details.

Include the affected component, impact, reproduction conditions, and any safe mitigation you have identified. Allow the maintainers time to investigate before public disclosure.

## Security Boundaries

Heinzel must preserve tenant isolation, opaque connection handles, short-lived capability grants, signed execution graphs, single-writer epoch fencing, and attributable evidence. Raw secrets must never appear in MCP results, logs, evidence payloads, or committed repository files.
