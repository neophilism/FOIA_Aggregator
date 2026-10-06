# Corpus expansion and autonomous refresh

The archive has two distinct ingestion modes:

1. **Capacity expansion** — an explicitly triggered, aggressive crawl that grows the B2-backed corpus toward the current storage allowance.
2. **Autonomous refresh** — a conservative scheduled crawl intended for long-term operation after storage is upgraded.

## Current free-tier storage policy

The current Backblaze account has a 10 GB free-storage allowance. The archive therefore uses two independent limits:

- **Document archive cap:** 8,500,000,000 bytes
- **Actual B2 target:** 9,000,000,000 bytes

The document cap is enforced by the normal archive path. The expansion runner separately measures actual billable B2 bytes across **all concrete object versions**, including:

- content-addressed document objects
- verified compressed SQLite snapshots
- older retained object versions
- any miscellaneous objects

The 9.0 GB target leaves about 1 GB of nominal headroom for storage-accounting differences and operational safety. The lower 8.5 GB document cap reserves room for metadata/database backups.

Do not raise either limit merely to squeeze out the last fraction of the free allowance.

## Manual capacity expansion

Use the GitHub Actions workflow:

**Expand corpus toward B2 capacity**

Defaults:

- actual B2 target: 9.0 GB
- document cap: 8.5 GB
- all active official sources
- 20 new/not-yet-archived records per source per wave
- 25 pages/resources per source
- depth 2
- at most 12 waves
- at most 330 minutes

Each wave:

1. restores the newest verified SQLite snapshot;
2. measures actual B2 usage;
3. runs one bounded live crawl across eligible sources;
4. writes a forced verified SQLite checkpoint to B2;
5. measures storage again;
6. stops at capacity or when a wave makes no archival/storage progress.

If the Actions time limit ends first, progress is still durable because every completed wave writes a verified B2 database checkpoint. A later expansion run resumes from the latest checkpoint.

## Autonomous refresh

The workflow:

**Autonomous corpus refresh**

contains a twice-weekly schedule, but scheduled jobs are deliberately disabled unless the repository variable below is explicitly set:

```text
FOIA_AUTONOMOUS_REFRESH_ENABLED=true
```

Until the storage upgrade, leave that variable absent or set to anything other than `true`.

A manual `workflow_dispatch` remains available for deliberate one-off refreshes.

When scheduled refresh is enabled, its default behavior is deliberately conservative:

- one wave
- all active official sources
- one new/not-yet-archived record per source
- five pages/resources per source
- depth 1
- same capacity safeguards as the expansion workflow

## After the storage upgrade

When the archive has approximately 1 TB available:

1. raise `B2_MAX_ARCHIVE_BYTES` and the expansion/refresh target after confirming the paid storage policy;
2. set `FOIA_AUTONOMOUS_REFRESH_ENABLED=true`;
3. adjust the scheduled cadence and per-source document limits based on observed ingestion velocity;
4. retain a meaningful percentage of total capacity as operational headroom rather than targeting 100% utilization.

The administrator dashboard should expose the current cap, actual billable B2 usage, remaining headroom, backup bytes, document bytes, recent ingestion velocity, and whether autonomous refresh is enabled.
