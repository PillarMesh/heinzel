# Gate A Completion Plan: PostgreSQL-First Path to a Witnessed Journey

- Date: 2026-09-15
- Branch: `feat/request-data-product-delivery`
- Baseline commit: `af8cf90` (offline gates green: ruff, mypy 282 files, 5093 tests, structure check)
- **Scope decision (locked 2026-09-15): PostgreSQL first, ClickHouse parity after.**
- Supersedes for sequencing purposes: the remaining-work view of
  `docs/superpowers/plans/2026-09-11-request-to-data-product-delivery.md` (Tasks 1-16).
  That plan remains the authority for per-task file lists and terminal acceptance text.
- Authority for status: `docs/delivery/request-to-data-product-acceptance.md`.
- Design authority: `docs/architecture/specifications/managed-data-engineering-platform-addendum-v0.1.md`.

This document is written to be picked up by another agent or engineer with no prior
session context. Every milestone states its goal, the exact code that must change, the
command that proves it, and the thing a human can look at.

---

## 1. The finding that reorders everything

The platform is far more complete than the 2026-09-11 plan's estimates imply. Almost every
capability in the acceptance ledger is `DELIVERED` or `PARTIAL` with live provider evidence.
One thing is not, and it blocks everything:

**`compile_product_iir` cannot succeed. Its return type is `NoValidPlan`.**

`services/compiler/src/pillarmesh_compiler/product_compiler.py:71` declares `-> NoValidPlan`,
and all three `return` statements in the function body return `NoValidPlan`. There is no
success path in the function at all.

The reason is governance, not an oversight. The legality rule
`PRODUCT-SQL-V2-PROJECT-SUM-001-CANDIDATE` is in state **Changes requested**
(`services/compiler/legality/product-sql/reviews/PRODUCT-SQL-V2-PROJECT-SUM-001-CANDIDATE-review.md`),
and three of its eighteen preconditions are hardcoded `unsatisfied` in the compiler:

| # | Precondition text in code | What actually closes it |
|---|---|---|
| 15 | enforce checked Decimal(57,9) result magnitude on every engine at runtime | PostgreSQL is already done and live-proved. Under this plan "every engine" means every **admitted** engine, so PostgreSQL-only activation closes it. |
| 17 | review live cross-engine checked SUM equivalence on both pinned engines | Must be **reworded** for single-engine activation, then satisfied by a live PostgreSQL equivalence run. See M3 and finding R2. |
| 18 | independent legality review has not approved this rule | A human reviewer approving the exact implementation revision. |

Everything downstream of compilation -- acquisition, LAND, transform, materialization,
publication, governed answers, result tables, CSV, dashboards, access grants, the context
graph and the agent interface -- already exists and has passing tests. They have never run in
one correlated journey **because no product can be compiled**, so the journey has no product
to deliver. Adding features does not move the date; closing preconditions 15, 17 and 18 and
giving the compiler a success path converts a large amount of already-built, already-tested
machinery into a working product in one step.

**Precondition 18 is a human gate on the critical path.** `AGENTS.md` forbids AI being
authoritative for legality. No agent can close it. Book a named reviewer who did not implement
the rule, starting at M0.

---

## 2. What the PostgreSQL-first decision actually means

### This ships Gate A, not the MVP

This is the most important consequence of the decision and it is a naming decision with
governance teeth.

The addendum defines the MVP as a **two-engine portability proof**, in three places:

- §20.2 scope rule: the MVP contains "two customer-selectable warehouse engines behind one
  destination contract: PostgreSQL and ClickHouse";
- §20.9 exit criteria: "PostgreSQL and ClickHouse pass the common destination conformance suite
  and **produce equivalent canonical results** for the supported semantic subset";
- §22 success measure: passing the MVP proves operation "with **destination portability** across
  PostgreSQL and ClickHouse".

A PostgreSQL-only product engine does not satisfy those. There are two ways to handle that, and
the choice matters:

1. **Amend the addendum** so the MVP no longer requires portability. This rewrites the design
   authority's definition of the product, invalidates the portability claim that §12.4 and
   ADR-0004's per-engine pinning exist to support, and is hard to walk back.
2. **Do not call this the MVP.** The 2026-09-11 plan already defines the exact endpoint we are
   building: **Gate A, PostgreSQL internal alpha** -- "one fresh request produces a
   PostgreSQL-backed table, and one in-scope question is answered from its warehouse facts under
   an answer scope policy." The addendum MVP stays as written and is simply not yet passed; it
   is reached when ClickHouse parity lands at M8.

**This plan takes option 2.** No addendum amendment is required, the design authority stays
intact, the ledger stays honest, and nothing has to be un-said later. The cost is only that we
call the first release Gate A rather than MVP.

If the product owner wants the word "MVP" on this release, that is option 1 and it needs an
explicit addendum amendment to §20.2, §20.9 and §22, plus a conformance re-run. Do not let it
happen by drift.

### What is still needed: an ADR-0004 amendment

ADR-0004 already pins construct semantics **per engine**, so a per-engine activation is
consistent with it. It still needs a short amendment stating that the product SQL rule is
activated for `postgresql` only, that cross-engine equivalence is explicitly **not claimed** by
that activation, and that ClickHouse activation requires its own evidence and review.

### What ClickHouse still does in Gate A

"PostgreSQL first" is not "ClickHouse removed". Deferring only affects ClickHouse as a
**product/transform engine**. Still in scope and already proved:

- **ClickHouse LAND.** Live-proved on a fresh emulator: one committed batch, exact no-op replay,
  reconciled lost provider response. Tenants can land data in ClickHouse in Gate A.
- **ClickHouse governed query compilation.** `services/compiler/tests/test_governed_query.py`
  proves signed, generation-pinned plans with SQL-level suppression and scan routing for both
  engines.
- **ClickHouse access provisioning.** Fresh live transactions proved apply, replay, revocation
  and post-revocation denial.
- **The ClickHouse product-materialization provider** committed in `af8cf90` ships but stays
  inert: its `switch_consumption_view` fails closed with `permanent_configuration` because
  `MaterializationObservation` carries no exact ClickHouse commit reference. That is correct
  behaviour and it is pinned by a test. It is a head start on M8, not a live path.

Deferred to M8: ClickHouse product transform, publication, answer parity, and the cross-engine
equivalence claim.

---

## 3. Review findings applied to this plan

These are the defects the PostgreSQL-first decision introduced into the first draft of this
plan. Each is now fixed in the milestones below; they are listed here so the reasoning survives.

**R1 -- Scope: the decision changes the addendum's MVP definition, not just a legality rule.**
The first draft called for an ADR-0004 amendment only. Resolved by §2: ship Gate A, leave the
addendum MVP intact and unpassed.

**R2 -- Correctness: precondition 17's text asserts a cross-engine claim.** Its literal text is
"review live cross-engine checked SUM equivalence on both pinned engines". Marking it satisfied
after a single-engine run would make the compiler assert something false in its own decision
record. It must be reworded before it can be satisfied, and **the reviewer must approve the
reworded precondition**, not the original. Added to M3 and M4.

**R3 -- Governance: admission must be engine-scoped or ClickHouse is admitted by accident.**
M5 flips preconditions from hardcoded `unsatisfied` to evaluated checks. "Fail closed when
evidence is absent" is not sufficient: the evaluation must be keyed on the `engine` argument and
on a per-engine activation record, or a ClickHouse compile can pass on PostgreSQL's evidence.
Added an explicit negative test to M5.

**R4 -- Design: keep every ClickHouse-motivated restriction in the approved rule.** Several
constraints are inadmissible *because* the engines diverge -- D2's excess-scale case (PostgreSQL
rounds, ClickHouse truncates) and D7's numeric-JSON coercion. With only PostgreSQL admitted
there is a standing temptation to relax them, since PostgreSQL alone is well-defined. Do not.
Widening an approved rule later is ordinary governance; **narrowing one after activation is much
harder and invalidates the evidence already accepted for it.** Recorded as a constraint in M2.

**R5 -- Estimate: M2 is cheaper than the first draft claimed.** Provider-pair fixtures and the
provider-pair regression shrink to one engine. Corrected from 8-12 to 5-8 engineer-days. It does
not shrink proportionally, because per R4 the fixtures must still encode the ClickHouse-motivated
exclusions, and all eight dimensions still apply per engine.

**R6 -- Ledger: the acceptance row is named for both engines.** The row "Complete live PostgreSQL
and ClickHouse journey" cannot go `DELIVERED` under this decision. It must split into a
PostgreSQL row that Gate A can close and a ClickHouse parity row that stays open. Added to M7.

**R7 -- Convergence: deferred parity tends never to land.** Mitigations: the ClickHouse
fail-closed tests stay in CI, the split ledger row stays visibly open, and ClickHouse parity is
scheduled as M8 work with a named trigger rather than "later".

---

## 4. Milestones

Each milestone ends with something a human can look at. No milestone is complete on green tests
alone -- the ledger's own rule applies: a rendered page, a passing suite or an HTTP 200 is not
delivery.

### M0 -- Stage the decision and the review (1 day)

- [ ] Amend ADR-0004 with the single-engine activation scope and the explicit non-claim of
      cross-engine equivalence (§2).
- [ ] Record the Gate A framing in `docs/delivery/request-to-data-product-acceptance.md`: this
      release targets Gate A; the addendum MVP remains unpassed pending ClickHouse parity.
- [ ] Split the "Complete live PostgreSQL and ClickHouse journey" ledger row in two (R6).
- [ ] Name the independent legality reviewer and book their window for the end of M3. They must
      not have implemented the rule.
- [ ] Merge the docs PR ([#60](https://github.com/PillarMesh/pillarmesh/pull/60)) so the addendum
      this branch is written against is on `main`.
- [ ] Run the conformance suite after the ADR and ledger edits.

**Visible output:** the ledger names Gate A as the target, shows two journey rows, and names a
reviewer with a date.

---

### M1 -- ClickHouse magnitude binding **(deferred to M8)**

Precondition 15 reads "on every engine at runtime". Under single-engine activation that means
every *admitted* engine, and PostgreSQL's magnitude enforcement is already built and live-proved:
it re-observes signed Decimal57/9 checks, rejects null or out-of-range values, binds zero
violations into its commit reference, and the answer reader rechecks that authority before
returning rows.

No work here for Gate A. The full task description now lives in M8.

---

### M2 -- Complete the D1-D8 evidence set for PostgreSQL

**Current state:** the proof note
(`services/compiler/legality/product-sql/proof-notes/PRODUCT-SQL-V2-PROJECT-SUM-001-CANDIDATE.md`)
argues all eight dimensions with fresh pinned-engine fixtures. Two gaps are named in its own
text: D7 says "Mutation regression is still required", and the JSON-decoding proof and fixtures
are incomplete.

**Constraint (R4): do not relax any restriction that exists because ClickHouse diverges.**
Every exclusion in the current proof note stays an exclusion, whether or not PostgreSQL alone
would permit it. The rule we activate must be the rule ClickHouse can later join without
renegotiation.

- [ ] Complete the D1-D8 JSON-decoding proof. Both engines currently coerce a numeric JSON value
      into the declared decimal although the contract requires a JSON string; that case is
      recorded invalid. Prove the compiler and the PostgreSQL provider boundary actually reject
      it rather than relying on the note's claim.
- [ ] Build positive and negative PostgreSQL conformance fixtures for every dimension. Keep the
      D2 excess-scale case inadmissible and add a fixture proving it is rejected -- it is
      inadmissible because PostgreSQL rounds where ClickHouse truncates, and that reason survives
      single-engine activation.
- [ ] Run focused mutation testing on the legality, admission, signature and magnitude branches.
      Inspect every survivor and either kill it with a test or record why it is not observable.
- [ ] Record mutation results and survivor analysis in the proof note.
- [ ] Mark each dimension's ClickHouse column "not claimed under PostgreSQL activation" rather
      than deleting it, so M8 resumes from a visible gap.
- [ ] Commit: `test(compiler): complete D1-D8 PostgreSQL fixtures and mutation regression`

**Exit criteria:** every D1-D8 row cites a positive and a negative fixture by test id, and the
mutation report has no unexplained survivor in a legality, signing or magnitude branch.

**Visible output:** the D1-D8 table with each row naming its fixtures, plus the mutation survivor
report. This is the document the reviewer reads first.

**Estimate:** 5-8 engineer-days.

---

### M3 -- Close precondition 17: reword, then prove

- [ ] **Reword precondition 17 (R2)** from cross-engine equivalence on both pinned engines to
      single-engine admission for PostgreSQL that explicitly does not claim cross-engine
      equivalence. The wording change lands with the ADR amendment from M0, and the reviewer
      approves the reworded text at M4.
- [ ] Stand up PostgreSQL at its exact pinned image and build digests.
- [ ] Run the semantic fixture and capture results: zero, negative, exact scale-nine, maximum
      positive and negative inputs, the safe two-row maximum sum, overflow at the exclusive
      bounds, empty group, and excluded generation.
- [ ] Prove the widened Decimal(57,9) cast behaves as the proof note argues, against the real
      engine. The note's integer proof establishes the bound; this run is the observation.
- [ ] Write the sanitized evidence bundle: engine version, image and build digests, canonical
      input digests, result digests.
- [ ] Commit: `test(compiler): record live checked SUM evidence for postgresql activation`

**Exit criteria:** a reviewer can read one document and see what PostgreSQL returned per case,
and the decision record no longer claims anything about ClickHouse.

**Visible output:** the per-case results report with real numbers from a real engine.

**Estimate:** 3-4 engineer-days.

---

### M4 -- Close precondition 18: independent legality review

**This is a human gate on the critical path. It cannot be closed by an agent.**

- [ ] Hand the reviewer the exact implementation revision, the D1-D8 proof, the PostgreSQL
      fixture set, identifier cases, the mutation results, the provider-owned evidence and the
      M3 live observations.
- [ ] The review must explicitly cover **the reworded precondition 17 and the single-engine
      activation scope** (R2), not only the SQL shape. An approval of the original cross-engine
      wording would not authorise what we are shipping.
- [ ] The review must confirm the activation record is engine-scoped (R3).
- [ ] On approval, update the review file with the approved revision and the reviewer's identity.
- [ ] Commit: `docs(compiler): record independent approval of postgresql product SQL activation`

**Exit criteria:** the review file says Approved, names the revision it approved, and names the
engine scope it approved.

**Visible output:** the signed review document.

**Estimate:** 3 engineer-days to prepare and respond; 5-10 business days elapsed. Budget a
change-request round trip -- the first review already rejected activation.

---

### M5 -- Give the compiler a success path

The milestone where the product starts existing. Do not start before M4 approves: the shape of
what is admitted is what the reviewer approves.

Blast radius, counted on `af8cf90`: `compile_product_iir` has **26 references across 6 files**
(`product_compiler.py`, `__init__.py`, three compiler test modules, and
`tests/acceptance/run_request_to_product.py`). Re-count before starting; if it has grown a lot,
this estimate is stale.

- [ ] Change the return type from `NoValidPlan` to a union of an admitted plan and `NoValidPlan`.
      Every caller must handle both.
- [ ] Flip preconditions 15, 17 and 18 from hardcoded `unsatisfied` to evaluated checks reading
      the recorded approval, the equivalence evidence and the runtime magnitude authority.
- [ ] **Key every one of those checks on the `engine` argument and a per-engine activation
      record (R3).** PostgreSQL's evidence must never satisfy a ClickHouse compile.
- [ ] Emit the admitted plan as a `ProductPhysicalPlan` bound to its legality decision, and sign
      the `SignedProductExecutionAuthorization` whose verifier `af8cf90` already added.
- [ ] Compose the signed cardinality authority into the request journey so precondition 14 is
      satisfied from live owning-service evidence rather than in tests only.
- [ ] Write the negative tests first. Each must still produce `No Valid Plan`: an unapproved
      rule; missing equivalence evidence; a stale approval; a mismatched revision; and
      **`engine="clickhouse"` after PostgreSQL activation**.
- [ ] Commit: `feat(compiler): admit and sign postgresql product physical plans`

**Exit criteria:** an approved request compiles to a signed execution authorization on
PostgreSQL; the same request compiled for ClickHouse still refuses; every absent-evidence path
still refuses.

**Visible output:** in the governed console, submit a request that previously terminated at
`No Valid Plan` and watch it reach a compiled, admitted plan. First moment the product visibly
does its job.

**Estimate:** 10-15 engineer-days.

---

### M6 -- Compose the end-to-end journey

Close the `PARTIAL` composition gaps in the order the journey runs.

- [ ] **Typed intent activation (ledger rows 2-3).** Build the composed interpreter resolving
      candidate constraints from live catalog and source authority into one activated intent.
      Prove replay stability; deny invalid and cross-tenant bindings.
- [ ] **Acquisition (row 4).** Drive one fresh live PostgreSQL transaction across the
      already-composed path: observations, artifacts, receipts, checkpoint, replay, denied
      credentials. Stripe is out of Gate A scope by the 2026-09-15 decision and moves to M8.
- [ ] **LAND composition (row 5).** Close the acquisition-checkpoint journey. Provider halves are
      already live-proved.
- [ ] **Transform and publication (row 7).** Run M5's admitted plan through dbt, magnitude checks,
      commit, catalog publication round-trip and the answer reader.
- [ ] **Run lifecycle (row 8).** Close durable-boundary resume and the operator projection.
- [ ] Commit each boundary separately, smallest diff first.

**Exit criteria:** one fresh request reaches a published PostgreSQL product with no pre-created
product anywhere in the system.

**Visible output:** the governed console showing a request go from submitted to a published data
product, and the result table rendering rows that reconcile to source rows you inserted minutes
earlier.

**Estimate:** 12-18 engineer-days.

---

### M7 -- Gate A acceptance: the witnessed journey

Follow `Task 16` in the 2026-09-11 plan, minus the ClickHouse repeat.

- [ ] One correlated transaction: fresh source cohort, fresh request, approval, acquisition,
      LAND, transform, publication, governed answer, result table, CSV, dashboard, access expiry.
- [ ] The same question asked through the agent interface returns the same result digest.
- [ ] Approval attempted through the agent interface records nothing.
- [ ] Every refused-question case from addendum §20.8 step 13.
- [ ] Fault injection at every durable boundary, recovering without duplicate effects.
- [ ] Backup and restore, reconciled against warehouse, OpenMetadata and Superset.
- [ ] Actively invoke every cold or scale-to-zero service; health-at-rest proves nothing.
- [ ] **Close the PostgreSQL journey ledger row only.** The ClickHouse parity row stays open with
      its M8 reference (R6).
- [ ] Confirm the ledger still shows the addendum MVP as unpassed (§2).
- [ ] Commit: `test: prove postgresql request-to-data-product delivery`

**Exit criteria:** a reviewer who starts with a new business request and no pre-created product
reaches a correct table and dashboard whose values reconcile to the cohort they inserted, and
after grant expiry that same user cannot query, download or obtain a usable dashboard link.

**Visible output:** the witnessed journey itself, plus the sanitized evidence bundle.

**Estimate:** 8-12 engineer-days.

---

### M8 -- Usable-release remainder, then ClickHouse parity

The first group does not block the witnessed journey but does block calling Gate A usable. Run
in parallel with M5-M7 where file ownership is disjoint.

- [ ] **Superset SSO.** Tenant Superset SSO identity binding is the only thing between the current
      state and an authenticated dashboard launch. Validate the exact tenant Superset origin and
      prove a requester loses launch access immediately after revocation. 5-8 days.
- [ ] **Access-control Superset provisioning.** Own grant-scoped Superset role and principal
      membership through a lifecycle rather than assuming it. 4-6 days.
- [ ] **Operations recovery producers.** Wire the remaining incident producers and recovery
      actions into the console; only native query incidents are wired today. 4-6 days.
- [ ] **Context-graph production adapters.** Replace governed-local adapters with production
      authority sources; retain the correlated evidence package. 3-5 days.
- [ ] **Agent-interface production adapters.** Configure current-authority, catalog, answer and
      impact adapters so the stdio entrypoint starts with full authority. 3-5 days.

- [ ] **Stripe acquisition**, descoped from Gate A on 2026-09-15. Drive one fresh live Stripe
      transaction across the composed acquisition path with the same evidence as PostgreSQL. 3-5 days.

**Then ClickHouse parity (R7), which is what turns Gate A into the addendum MVP:**

- [ ] Extend `MaterializationObservation` so a ClickHouse commit reference (part, partition and
      mutation identity) is carried as first-class evidence, not a string.
- [ ] Bind it through `services/runtime/src/pillarmesh_runtime/product_materialization.py` into
      the receipt, giving the consumption switch the evidence it currently lacks.
- [ ] Replace the fail-closed test with a positive test proving the switch happens only on exact
      commit evidence, and a negative test proving it still fails closed when absent or
      mismatched. **Do not delete the negative case.**
- [ ] Bind ClickHouse publication and the answer reader to that authority, matching PostgreSQL's
      `providers/postgresql/.../answer_generation.py`.
- [ ] Add `tests/integration/test_clickhouse_product_materialization_live.py`.
- [ ] Complete the ClickHouse D1-D8 columns left open at M2, and run the live **cross-engine**
      equivalence that the original precondition 17 described.
- [ ] Second independent review for ClickHouse activation.
- [ ] Repeat M7's journey on ClickHouse; close the ClickHouse ledger row; the addendum MVP is now
      passed.
- [ ] Commit: `feat(clickhouse): activate the product SQL rule for clickhouse`

**Visible output:** an SSO-authenticated dashboard opening from a governed launch link and denied
immediately after revocation; then the same witnessed journey running on ClickHouse.

**Estimate:** usable-release remainder 19-30 engineer-days, substantially parallelisable.
ClickHouse parity 20-30 engineer-days including its own review gate.

---

## 5. Estimates

Engineer-days, critical path only unless noted.

| Milestone | Gate A | Notes |
|---|---:|---|
| M0 stage decision and review | 1 | |
| M1 ClickHouse magnitude | 0 | deferred to M8 |
| M2 D1-D8 and mutation | 5-8 | corrected from 8-12 per R5 |
| M3 live evidence and reword | 3-4 | |
| M4 independent review | 3 | 5-10 business days elapsed |
| M5 compiler success path | 10-15 | |
| M6 end-to-end composition | 9-14 | Stripe descoped 2026-09-15 |
| M7 witnessed acceptance | 8-12 | |
| **Gate A total** | **39-56** | |
| M8 usable-release remainder | 19-30 | parallelisable |
| M8 ClickHouse parity to reach addendum MVP | 20-30 | includes a second review gate |
| **Addendum MVP total** | **78-116** | |

Calendar, assuming the review gates add elapsed time no staffing compresses:

| Staffing | Gate A | Addendum MVP |
|---|---|---|
| 1 engineer | 2.5-4 months | 6-8 months |
| 3 engineers, disjoint boundaries | 7-9 weeks | 13-18 weeks |
| First visible end-to-end product (through M6) | 5-8 weeks | -- |

The three-engineer number is not one third of the one-engineer number. M4 is elapsed time no
staffing compresses, and M5 is a single narrow boundary that does not parallelise.

Excluded, per the 2026-09-11 plan's correct scoping: production deployment, cloud secret
management, high availability and provider account procurement.

---

## 6. Verification at every commit

Run the focused failing test first and keep its failure reason in the task notes. Before each
milestone merge:

```sh
uv sync --locked --all-packages
uv lock --check
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest -m "not live"
./tests/repository-structure/test.sh
```

For console changes also run type-check, unit tests, the production build and the relevant
Playwright spec. For authorization, signature, recovery and legality branches run focused
mutation testing and inspect survivors.

When a change implements an artifact, principal class or table the addendum defines, update the
addendum in the same change and extend its pin in
`tests/conformance/test_specification_conformance.py`. §20.9 is pinned by
`test_clickhouse_exclusions_match_section_20_9`; that pin checks the ClickHouse exclusion
substrings, so run the conformance suite after any §20.9 edit.

Three planned items are deliberately outside their pinned blocks today and must join them in the
change that implements them: `answer_runtime` in the §18.1 class list and
`WarehousePrincipalClass`; the `investigating -> executing` transition for policy admission; and
the plan-digest citation on `StakeholderAnswerDraft`.

Adding a top-level area or governed component requires a repository-layout update, an ADR and a
structure-validator update in the same change. `services/bi-control` and
`services/access-control` already exist in the tree -- confirm both carry their ADR and layout
rows before M7 closes.

---

## 7. Risks

- **The reviewer requests changes again.** The first review already rejected activation and found
  real problems. Budget one round trip; do not plan as though approval is a formality.
- **Narrowing after activation is expensive (R4).** If M2 relaxes a ClickHouse-motivated
  restriction because PostgreSQL alone permits it, adding ClickHouse at M8 requires narrowing an
  approved rule and re-justifying evidence already accepted. Keep the exclusions.
- **Accidental ClickHouse admission (R3).** The single highest-severity failure mode of this
  decision: a precondition evaluated without the engine key admits ClickHouse on PostgreSQL's
  evidence. The M5 negative test is the guard; do not remove it when parity lands, change it.
- **Parity never lands (R7).** Deferred work decays. The split ledger row and the inert
  fail-closed ClickHouse provider keep the gap visible; schedule M8 parity explicitly.
- **"Gate A" drifts into being called "the MVP" (R1).** The addendum still defines the MVP as
  two-engine. If marketing or the ledger starts calling Gate A the MVP without the §20.2/§20.9/§22
  amendment, the product is claiming portability it has not proved.
- **ClickHouse's scan estimator only works for MergeTree-family reads.** Consumption objects it
  cannot estimate fail closed to per-question review, so more ClickHouse questions reach the inbox
  until publications use estimable tables. Matters at M8.
- **Superset stable-key reconciliation and embedded authorization** may add 2-3 weeks. Spike
  before committing M8's Superset estimate.
- **Scope creep from Scouts.** Post-MVP (Task 17). Do not start before M7 passes.

---

## 8. Open decisions for the product owner

1. ~~Track A or Track B~~ **Decided 2026-09-15: PostgreSQL first.**
2. **Who is the independent legality reviewer, and when are they available?** On the critical
   path; book now. They must review the reworded single-engine precondition (R2).
3. ~~Is Stripe acquisition in Gate A~~ **Decided 2026-09-15: dropped.** PostgreSQL source
   acquisition carries the first witnessed journey; Stripe returns with ClickHouse parity at M8.
4. **Does Gate A need the agent interface against production adapters**, or is the governed-local
   proof enough for the first release? Deferring removes 3-5 days from M8.
5. ~~Is "Gate A" an acceptable name~~ **Decided 2026-09-15: Gate A.** The addendum MVP stays as
   written and unpassed. Sections 20.2, 20.9 and 22 are not amended.

---

## 9. How to pick this up cold

1. Read `docs/delivery/request-to-data-product-acceptance.md` for current capability status.
2. Read §1 to understand why nothing downstream matters until the compiler can succeed, and §2
   to understand why this ships Gate A rather than the addendum MVP.
3. Confirm the baseline is still green with the §6 verification block.
4. Start at M0. Do not start M5 before M4 approves.
5. Promote an acceptance-ledger row only with a fresh transaction identifier and terminal
   evidence, never on a passing test alone.
