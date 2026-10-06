# Administrator dashboard

The public web application includes a read-only operational dashboard at:

```text
/admin
```

It is intentionally hidden until an administrator password is configured.

## Authentication

Configure:

```text
FOIA_ADMIN_USERNAME=admin
FOIA_ADMIN_PASSWORD=<strong unique password>
```

The dashboard uses HTTP Basic authentication. When `FOIA_ADMIN_PASSWORD` is absent, `/admin` behaves like a normal 404 rather than exposing an unauthenticated operations page.

No credentials are stored in the SQLite database or rendered into HTML.

## Dashboard sections

### Archive overview

- files archived
- full-text searchable records
- active official sources
- active source errors
- recent discovery/archive/extraction velocity
- OCR-assisted record count

### B2 storage capacity

The dashboard measures actual stored bytes across all concrete object versions and shows:

- actual B2 stored bytes
- document-version bytes
- database-backup bytes
- other bytes
- remaining headroom to the configured target
- current object count
- object-version count
- document safety cap
- total expansion target

B2 telemetry is cached for five minutes to avoid repeatedly enumerating the bucket.

### Source health

- active sources
- sources that have never completed successfully
- sources stale for more than seven days
- current source errors
- 403 / Forbidden failures
- 429 / rate-limited failures
- recent failing source URLs and error text

This is the primary queue for source-adapter and anti-blocking work.

### Ingestion pipeline

- download status distribution
- extraction status distribution
- extraction method distribution
- file-type distribution
- recent successfully archived records
- seven-day archived byte volume

### System state

- local SQLite size
- schema version
- latest verified B2 database backup
- retained backup count
- web-process start time
- Render commit when available
- autonomous-refresh enabled/disabled state

## Presentation use

The dashboard is useful after the public search demonstration because it shows that the platform is an operating archival system rather than a static mockup.

A concise presentation sequence is:

1. show current corpus/storage utilization;
2. show recent ingestion velocity;
3. show the source-health queue, especially blocked sources;
4. show extraction/OCR status;
5. show backup freshness and autonomous-refresh state.

Do not expose the dashboard password in presentation materials.
