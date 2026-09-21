# Deployment

`deploy/` is the infrastructure-neutral home for packaging and deployment definitions after an ADR selects the relevant technology and topology.

`deploy/quickstart/` holds the local demonstration image and compose file decided in [ADR-0009](../docs/architecture/decisions/ADR-0009-container-packaging.md). It is built from source, published on `127.0.0.1` only, and never pushed to a registry.

Nothing else belongs here yet. Production deployment is undecided, so do not pre-create Docker, Kubernetes, Terraform, cloud, or environment subdivisions for it; add them when an ADR says what they are.
