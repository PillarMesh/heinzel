# Capability Providers

`providers/<provider>/` contains provider-specific capability declarations, implementations, configuration schemas, and conformance fixtures.

A provider may expose any combination of `READ`, `WRITE`, `CDC`, `QUERY`, `EVENT`, `TOOL`, `RESOURCE`, and `ACTION`. Do not divide providers into source and destination trees.

`providers/superset` implements the managed BI capability while dashboard desired state and
publication receipts remain authoritative in `services/bi-control`.
