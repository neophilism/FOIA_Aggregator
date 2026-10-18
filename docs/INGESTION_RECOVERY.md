# FA-004 durable ingestion recovery

The crawler records run and source receipts in the authoritative SQLite database
(schema migration 7). `python main.py ingestion-status` reads the latest receipt
without contacting B2 or creating a missing database. The output distinguishes
completed, interrupted, failed and degraded waves; planned, attempted and skipped
sources; metadata failures; checkpoint observation; and catalog/index deltas.
Actual versioned B2 bytes and costs remain unknown unless independently measured.
Cataloged file bytes are not a billable-storage measurement.

A nonblocking OS file lock protects the resolved database path before schema or
crawler writes. Seed and expansion runners hold it over restore, all waves and
checkpoint publication. Nested engine calls are allowed only in the same thread
and process. Never remove a lock file to recover it: process termination releases
the lock; deleting it could create two lock inodes. This is a same-host safeguard,
not a distributed lease. Independent Actions runners must still share the
FA-003 concurrency domain. No workflow activation or production dispatch is
performed by this change.

Interrupted waves retain their original source plan and policy fingerprint.
Restart retries unfinished sources and skips source receipts already committed
as completed. Changed bounds or backend start a distinct plan. Source selection
prioritizes never-crawled and least-recently-attempted rooms before cross-agency
breadth, preventing a low source limit from repeatedly picking the first IDs.
Existing per-host limits and failure cooldowns remain in force. Binary quota
stops explicitly identify unvisited sources rather than counting them completed.

Seed/expansion restart preserves a valid existing SQLite database before querying
cloud backups. A corrupt existing file is retained and requires explicit recovery;
it is never deleted merely to restore an older cloud snapshot. Existing document
and object-ledger deduplication remains authoritative on source replay.

Checkpoint failures are DEGRADED and require escalation; they are not a successful
publication. The legacy boolean run_once API still reports metadata success only.
A receipt inside the newly uploaded snapshot necessarily predates observation of
that upload: PENDING/EXTERNAL_CHECKPOINT_REQUIRED inside that snapshot is not proof
of a failed upload. Verify the actual backup object and checksum separately.

## Current verification and remaining acceptance

Local credential-free tests cover writer contention and process termination,
restart source skipping, changing policy, source outages, quota stops, unknown
external costs/usage, receipt privacy, and preservation of local/corrupt databases.
The full unittest suite and exact-head GitHub CI must pass before readiness.

PR #53 remains draft while page/cursor integration and complete acceptance are
unfinished. Live FA-003 serialization, B2 idempotence/restore and two sequential
production ingestion runs still require existing authorized operational access.
Do not enable production, change storage caps or claim live acceptance from these
offline tests. Preserve the ten-unit approved plan denominator.
