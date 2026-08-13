# Applications

`apps/` contains operator-facing product entry points. The only declared application boundary is `console/`, created when its first substantive implementation arrives.

Control-plane capabilities belong in their named `services/` boundaries, not in a generic application backend.
