# Independent Review Record: SNAPSHOT-POSTGRESQL-SNOWFLAKE-001

- Review status: Approved by independent re-review
- Rule-author role: Task 6 implementer and rule-evidence author
- Independent reviewer task: `/root/acceptance_task6_review`
- Prior dispositions:
  - `changes_required` for `317d2424530c333483b55fbb85edf62417724377`
  - `changes_required` for `8f440d1d222d25436a734d1e5d0c0b7889cef9b3`
  - `changes_required` for `48b400d9918a0335e0249abf9b3f9af7b6f9e303`
- Reviewer identity: `/root/acceptance_task6_review` — independent Task 6 legality reviewer; did not
  author the rule or implementation
- Re-review date: 2026-08-13
- Reviewed round-3 commit SHA: `ab41c3418135d833f897a2c5273de4a093ead227`
- Re-review disposition: `approved`

This packet was author-prepared and did not itself constitute approval. The first remediation resolved
post-commit recovery, tri-state facts, executable fixtures, independent freshness boundaries, and
governed-artifact rejection. The same reviewer required a second round for lossless text domains,
observed source read-only access, observed Snowflake object kinds, and corresponding limitations.
The second remediation resolved those findings. The same reviewer then found that verification
captured its evaluation timestamp before provider observation completed, and that the negative JSON
fixture's unconstrained-text pointer did not match its evidence claim. The author addressed those
findings below but did not rule on the remediation. Because a commit cannot contain its own SHA, the
independent reviewer recorded the exact round-3 implementation commit and disposition in this
subsequent review-only change.

## Required independent checks

| Review question | Author-prepared round-3 evidence | Independent reviewer result |
| --- | --- | --- |
| Is the fixed text correspondence lossless for every admitted value? | Both PostgreSQL and Snowflake require `VARCHAR(65535)` for customer reference/status; `OrderRow` accepts exactly 65,535 characters and rejects 65,536; the encoder and merge casts use the same bound; unconstrained source `TEXT` has executable/direct rejection cases | Approved |
| Does PostgreSQL observe effective read-only access rather than trust the handle label? | Provider queries effective `SELECT`, all table-level writes, and column-level INSERT/UPDATE/REFERENCES for the resolved relation; observations return `True`, `False`, or `None`; precondition 1 and executable false/Unknown cases fail closed | Approved |
| Does Snowflake observe both target and ledger object kinds? | Provider queries `information_schema.tables` separately for both objects and reports base table, view, or Unknown; precondition 5 and executable target/ledger view/Unknown cases fail closed | Approved |
| Does the fixed PostgreSQL-to-Snowflake mapping preserve exact numeric, currency, and timestamp representation? | Exact provider normalization tests; fixed constants; executable and direct narrowing cases | Approved |
| Does PostgreSQL observe the configured key's actual single-column PK/unique constraint, nullability, and stable btree order? | PostgreSQL catalog query and `test_observe_reports_actual_key_constraint_and_stable_order` | Approved |
| Does Snowflake observe exact target key and ledger columns/key declaration? | Snowflake information-schema queries, exact-schema provider test, and preconditions 5 and 6 | Approved |
| Do missing required observation facts remain explicit Unknown and produce `No Valid Plan`? | Required-field model test, Unknown legality parameterization, and executable Unknown cases | Approved |
| Are both observations bound to contract handles and independently fresh under the service clock boundary? | Preconditions 1/5/8; executable handle cases; exact zero/ten-minute and independent future/stale tests; `ContractService.verify` captures evaluation time after both observations; advancing-clock regression proves observations completed immediately beforehand are not misclassified as future | Approved |
| Is destination merge/ledger ambiguity bounded honestly? | One explicit transaction, batch/manifest resolution, lost-response tests, and offline/single-worker limitation | Approved within the stated offline, single-worker M0 boundary |
| Are fixtures executable and complete for the admitted provider-pair set? | Self-contained `positive.json`/`negative.json`, fixture executor, admitted-pair regression, and `source_text_is_unconstrained` changing the actual `customer_ref` column to `TEXT` | Approved |
| Does the requirement-to-mutant mapping below cover every precondition with no survivors? | Clean focused run: 383 generated, 383 killed, 0 survived; table below | Approved |
| Are graph evidence and governed artifact bindings enforced? | Evidence-set compiler-defect test and four rehashed governed-artifact package rejections | Approved |
| Are limitations complete and consistent with the rule? | Proof note states privilege-observation scope, exact bounded types, informational Snowflake keys, offline atomicity, evidence retry, and single-worker boundary | Approved |

## Requirement-to-mutant mapping

Each mapped mutant removes one required fact or turns a conjunction into an admission-capable
disjunction. The named parameter of `test_requirement_to_mutant_diagnostic_mapping_is_exact`
supplies exactly the removed fact and requires one attributed failed precondition, its evidence
identifiers, and its sole smallest-change diagnostic.

| Precondition | Mutant | Bypass or negation | Killing test parameter |
| --- | --- | --- | --- |
| 1 | `heinzel_compiler.legality.x_evaluate_legality__mutmut_39` | Removes observed source read-only access | `precondition-1-mutmut-39` |
| 2 | `heinzel_compiler.legality.x_evaluate_legality__mutmut_71` | Removes stable key order | `precondition-2-mutmut-71` |
| 3 | `heinzel_compiler.legality.x_evaluate_legality__mutmut_107` | Lets either source or destination schema mask the other | `precondition-3-mutmut-107` |
| 4 | `heinzel_compiler.legality.x_evaluate_legality__mutmut_120` | Lets fixed projection or unique names mask the other | `precondition-4-mutmut-120` |
| 5 | `heinzel_compiler.legality.x_evaluate_legality__mutmut_144` | Removes observed ledger base-table kind | `precondition-5-mutmut-144` |
| 6 | `heinzel_compiler.legality.x_evaluate_legality__mutmut_198` | Removes destination key non-nullability | `precondition-6-mutmut-198` |
| 7 | `heinzel_compiler.legality.x_evaluate_legality__mutmut_235` | Removes snapshot materialization | `precondition-7-mutmut-235` |
| 8 | `heinzel_compiler.legality.x_evaluate_legality__mutmut_260` | Removes source/destination freshness | `precondition-8-mutmut-260` |
| 9 | `heinzel_compiler.legality.x_evaluate_legality__mutmut_287` | Removes destination capabilities | `precondition-9-mutmut-287` |
| 10 | `heinzel_compiler.legality.x_evaluate_legality__mutmut_309` | Removes source evidence safety | `precondition-10-mutmut-309` |

## Survivor classification

The round-3 clean focused campaign has no survivors: all 383 generated mutants were killed. The
additional precondition-1 read-only and precondition-5 ledger-kind facts have direct atomic negative
tests as well as executable fixture cases. There are no equivalence classifications requiring a
reviewer waiver.

## Independent reviewer completion

The independent reviewer completed these fields after reviewing the exact round-3 commit and
independently rerunning or validating the cited evidence.

- Reviewer identity: `/root/acceptance_task6_review` — independent Task 6 legality reviewer; did not
  author the rule or implementation
- Re-review date: 2026-08-13
- Reviewed round-3 commit SHA: `ab41c3418135d833f897a2c5273de4a093ead227`
- Lossless fixed type correspondence: Approved; the admitted PostgreSQL and Snowflake domains use
  symmetric `VARCHAR(65535)` bounds, exact remaining physical types, bounded provider rows, and
  fail-closed narrowing/unconstrained-type cases.
- Source read-only observation: Approved; effective SELECT and relation/column write privileges are
  observed for the resolved source relation, with False and Unknown rejected.
- Snowflake target/ledger object-kind observation: Approved; both kinds are independently observed
  and view/Unknown results are rejected.
- Key and ledger facts: Approved; exact source PK/order, destination key, ledger schema, and declared
  ledger key are required.
- Tri-state Unknown behavior: Approved; missing required facts produce Unknown and `No Valid Plan`.
- Handle binding and freshness: Approved; both observations are bound to contract handles,
  independently freshness-checked, and evaluated against one timestamp captured after both
  synchronous observations.
- Destination-ledger atomicity: Approved within the stated offline, single-worker M0 boundary; this
  is not a live Snowflake transaction witness.
- Positive and negative fixtures: Approved; fixtures are self-contained, executable, cover the
  admitted provider pair, and include the corrected unconstrained-source-TEXT case.
- Mutation mapping and survivor result: Approved; all ten preconditions have mapped killing tests
  and the fresh campaign killed 383/383 mutants with zero survivors. The two prior Unknown-status
  mutants were non-equivalent and are now killed.
- Post-admission evidence-set invariant: Approved.
- Governed-artifact tamper rejection: Approved for contract, source observation, destination
  observation, and signed graph.
- Limitations: Approved; privilege scope, informational Snowflake keys, single-worker assumption,
  offline atomicity, evidence-outage recovery, and point-in-time metadata boundaries are stated.
- Disposition: `approved`
- Reviewer notes: This approval covers the offline M0 legality rule and Task 6 evidence at the exact
  reviewed commit. It does not constitute a live provider witness, authorize environment changes,
  or remove the documented single-worker and informational-key limitations.
