# FA-010 — scale controls and incomplete acceptance

The current work branch adds fail-closed **application cycle** checks. These operate before
database initialization and FOIA.gov metadata refresh:

- Maximum of 10,000 selected reading rooms per cycle by default. An explicit
  `crawler.source_limit` must be an integer 1–10,000; the engine additionally
  slices a nonconforming selector result to its validated limit.
- `crawler.max_docs_per_source` defaults to 10 and is validated 1–10,000.
  This is passed into the source crawler; per-source integration must enforce it.
- `crawler.maximum_cycle_seconds` defaults to 3,600, maximum 86,400.
  The orchestrator stops **starting new sources** after the limit. It cannot
  interrupt a source already running or limit the independent metadata refresh.
- `crawler.dry_run` must parse as an explicit boolean. A live (non-dry-run)
  crawl must have a positive per-host delay; malformed or nonfinite delays
  are rejected before any external work.
- Page count, discovered document count, depth, timeout, and page-byte limits
  are checked before a crawl. These prevent invalid settings from being
  silently treated as unlimited access.
- `ScaleBudget.assess_candidate` and `assess_storage_headroom` are
  **offline, non-authorizing** checks. The latter models the 8.5-billion-byte
  archived-object and 9-billion-byte versioned-B2 ceilings independently.
  Actual ingestion must still consult trustworthy local ledgers and provider
  storage accounting and enforce existing single-writer, object and cost gates.

## Acceptance evidence and remaining work

The relevant negative and positive tests are in
`tests/test_engine_resilience.py` and `tests/test_scale_budget.py`.
Inspect **GitHub's exact current PR head**, not a previous green SHA.
Passing synthetic tests does not establish an actual stress-tested production
deployment, current B2 usage, read-only restore, public privacy/accessibility
acceptance, or owner-authorized production release.

Outstanding: profile representative multi-agency loads in an isolated runtime;
measure source and total cycle capacity; verify independent B2 version usage,
single-writer lease and recovery; check live read-only health and public
accessibility; reconcile actual provider rules before merging. Keep the PR
draft while these acceptance requirements remain unmet. No live crawler
schedules or billable calls are authorized by these checks.
