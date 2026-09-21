# Deployment

`deploy/` is the infrastructure-neutral home for packaging and deployment definitions after an ADR selects the relevant technology and topology.

`deploy/quickstart/` will hold the local demonstration image and compose file decided in [ADR-0009](../docs/architecture/decisions/ADR-0009-container-packaging.md). They are built from source, published on `127.0.0.1` only, and never pushed to a registry. They are not written yet, so this directory currently holds only this README.

Nothing else belongs here yet. Production deployment is undecided, so do not pre-create Docker, Kubernetes, Terraform, cloud, or environment subdivisions for it; add them when an ADR says what they are.
