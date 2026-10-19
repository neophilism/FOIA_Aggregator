# FA-010 scale-budget foundation (partial)

The pure offline policy explicitly bounds sources, documents per source,
document bytes and maximum cycle duration. Its projections categorically
withhold authorization for crawling and paid operations. No crawler,
external transport or deployment is enabled.

Remaining: enforce budgets at every real crawler/storage transaction,
measure queue fairness and backpressure, validate backup/restore and B2
cost controls, run throughput/load tests with real fixtures, deploy under
appropriate approval and demonstrate public operational readiness.
