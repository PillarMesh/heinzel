# Changelog

All notable changes to Heinzel are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Initial public release of Heinzel under the Apache License 2.0.

### Changed (breaking)

- The product namespace is `heinzel`: distributions are named `heinzel-*`, Python imports
  `heinzel_*`, and environment variables `HEINZEL_*`. Code, configuration and deployments that use
  the earlier names must be updated.
- The snapshot contract's `evidence_retention` value is now `snapshot_30_days` and its `producer`
  is `heinzel-contract-service`. Its `schema_version` is still `1`, so contract records written
  with the earlier values no longer validate.
- Digest and signature domain tags now use the `heinzel` namespace. Digests and signatures stored
  before this release do not verify and must be recomputed.
- OpenMetadata descriptions now carry the marker `Heinzel metadata v1:`. Catalog entities written
  with the earlier marker are not recognised as Heinzel-managed.
