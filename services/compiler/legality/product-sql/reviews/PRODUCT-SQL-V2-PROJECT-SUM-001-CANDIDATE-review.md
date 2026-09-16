# Independent review: PRODUCT-SQL-V2-PROJECT-SUM-001-CANDIDATE

- Status: Changes requested
- Reviewer: Independent review recorded outside this candidate file
- Implementation revision: `50a5b1d` plus pending live-semantics correction

The review rejected activation. It found an output-name collision and found that caller capability
claims did not establish provider authority or exact cross-engine decimal SUM semantics. The collision
is now rejected by the shared shape predicate and emitter boundary. A strict digest-bound observation
candidate now records engine-specific physical metadata, but it is not authenticated provider evidence.
Fresh pinned-engine probes subsequently established that PostgreSQL promotes the sum while ClickHouse
retains Decimal(38, 9) and wraps on overflow. This is evidence against cross-engine equivalence for
the current SQL shape, so the equivalence gate remains unsatisfied.

No approval is claimed. Re-review must cover the exact implementation revision, D1-D8 proof, both
engine fixtures, identifier cases, provider-pair regression, mutation results, provider-owned evidence,
and live engine result/overflow observations. Activation is prohibited while the provenance,
semantic-equivalence, and independent-review preconditions remain unsatisfied.
