# FA-008 alert policy increment

Local fail-closed policy evaluates bounded crawl, source freshness, quota
and backup conditions. Each returned decision carries the exact source SHA
and observation time; no network notification, dashboard write, or production
alert delivery occurs.

Remaining: trusted source metrics, authenticated incident dashboard, durable
deduplication/acknowledgment, rate limits, configuration storage, external
alert delivery and replay/recovery tests. Neither a green unit test nor an
alert projection establishes production operational coverage.
