# Console demonstration screenshots

These images are generated, never hand-edited. None are committed at the moment; run the
suite below to produce them locally.

## Regenerating

```sh
cd apps/console
npm run test:e2e -- screenshots
```

The suite builds the browser bundle, serves it and the API from the single Starlette
origin (`scripts/serve-built.mjs`), resets the demo fixture before every capture, and
overwrites every file in this directory.

Determinism comes from `playwright.config.ts` and from the fixture itself: a 1440x1024
desktop viewport (1024x768 for the medium-width drawer), `en-US`, UTC, the light colour
scheme, reduced motion, disabled animations, a hidden caret, and a fixture clock pinned
to `2026-09-01T16:00:00Z`. Reference identifiers such as `operation-0001` come from a
monotonic sequence that the demo reset restores, so repeated runs produce byte-identical
files.

Before each capture the suite asserts that the visible text contains no filesystem path,
CSRF token, bearer token, password, secret, or e-mail address. All content is synthetic:
the tenant is `Northwind Demo`, the workspace is `Revenue to cash`, and every route
carries the persistent `Demo scenario - no managed effects` banner.

## What the suite captures

| File | Screen | Viewport |
| --- | --- | --- |
| `warehouse-foundation.png` | Warehouse foundation selection, before confirmation | 1440x1024 |
| `provisioning-progress.png` | Accepted provisioning operation, deliberately not shown as complete | 1440x1024 |
| `command-center.png` | Returning-user command center with the prioritized decision queue | 1440x1024 |
| `stakeholder-answer.png` | Stakeholder-answer decision with the reviewed digest confirmed | 1440x1024 |
| `access-preview.png` | Least-privilege access preview: requested fields, effective scope, exclusions | 1440x1024 |
| `no-valid-plan.png` | `No Valid Plan` outcome with no admissible action | 1440x1024 |
| `evidence-drawer-medium.png` | The evidence drawer as a dialog at medium width | 1024x768 |

`command-center.png` and `stakeholder-answer.png` share a route by design: the console
places the decision inside the command center rather than on a separate page. They differ
in what the architect has done — the second shows the exact reviewed digest confirmed and
the admissible actions enabled.

## What is missing, and why

Two further screens cannot be produced from the committed fixtures. The corresponding
tests in `e2e/screenshots.spec.ts` are marked `fixme` rather than approximated with a
different screen.

- **`meaning-review.png`** — the meaning approval gate has no reachable browser state.
  `SetupWorkbench` renders strictly by `SetupView.active_stage`, and
  `FixtureConsoleBackend` never advances that field past `foundation`: confirming the
  warehouse binding and submitting the process package both record their result and leave
  the stage unchanged. `/reviews/review-meaning` renders the foundation stage for the same
  reason.
- **`activation-review.png`** — as above, and additionally `FixtureConsoleBackend.get_review`
  admits a review only to a role listed in its `required_authorities`. `review-data-product`
  requires `data_owner` and `review-activation` requires `budget_approver`, while the
  trusted context the server issues holds `data_architect` alone, so both answer `404`.

Both gaps are in the fixture backend, not in the browser application. They will close when
the fixture advances the setup stage and the console can present more than one role.
