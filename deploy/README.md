# Deployment

`deploy/` is the infrastructure-neutral home for packaging and deployment definitions after an ADR selects the relevant technology and topology.

`deploy/quickstart/` is the home of the local demonstration image and compose file decided in [ADR-0009](../docs/architecture/decisions/ADR-0009-container-packaging.md). They are built from source, published on `127.0.0.1` only, and never pushed to a registry. That directory holds the `Dockerfile`, the `compose.yaml` that builds and publishes it, and a [README](quickstart/README.md) saying what the demonstration shows and where it stops.

Nothing else belongs here yet. Production deployment is undecided, so do not pre-create Docker, Kubernetes, Terraform, cloud, or environment subdivisions for it; add them when an ADR says what they are.
