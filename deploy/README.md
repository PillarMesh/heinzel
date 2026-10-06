# Deployment

`deploy/` is the infrastructure-neutral home for packaging and deployment definitions after an ADR selects the relevant technology and topology.

`deploy/quickstart/` is the home of the local demonstration image and compose file decided in [ADR-0009](../docs/architecture/decisions/ADR-0009-container-packaging.md). They are built from source, published on `127.0.0.1` only, and never pushed to a registry. That directory holds the `Dockerfile`, the `compose.yaml` that builds and publishes it, and a [README](quickstart/README.md) saying what the demonstration shows and where it stops.

`deploy/quickstart/warehouse-control/compose.yaml` is a second, separate project: the warehouse the demonstration creates on its opt-in path, where warehouse-control provisions one instead of being given one. Nothing starts it by hand — the PostgreSQL warehouse provider drives it — and it is opt-in because driving Compose means the console needs a reachable Docker daemon, and from inside a container that daemon's socket. The [quickstart README](quickstart/README.md) says what that costs.

Nothing else belongs here yet. Production deployment is undecided, so do not pre-create Docker, Kubernetes, Terraform, cloud, or environment subdivisions for it; add them when an ADR says what they are.
