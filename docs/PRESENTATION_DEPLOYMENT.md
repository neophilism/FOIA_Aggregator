# Presentation deployment

This deployment profile is designed for a single-host public demo with:

- one public FastAPI web service
- one continuous crawler service
- one shared local SQLite data volume
- private Backblaze B2 document storage
- Tesseract OCR installed in the application image

The web and crawler containers share the SQLite volume on the **same host**. Do not place the SQLite database on NFS or another network filesystem. A later multi-host deployment should move metadata/search state to a database designed for that topology.

## 1. Configure secrets

Copy the example environment file:

```bash
cp .env.example .env
```

Fill in:

```text
FOIA_API_KEY
B2_KEY_ID
B2_APPLICATION_KEY
B2_BUCKET
B2_REGION
B2_ENDPOINT_URL
```

For a hosted deployment, set these as the platform's secret environment variables instead of storing a persistent `.env` file.

The repository ignores `.env`.

## 2. Start the demo

```bash
docker compose up -d --build
```

The public web service listens on:

```text
http://localhost:8000
```

Health check:

```bash
curl -fsS http://localhost:8000/healthz
```

Expected response:

```json
{"status":"ok","schema_version":6}
```

The schema version may increase in later releases.

## 3. Initial presentation corpus

The Compose profile intentionally starts conservatively:

```text
FOIA_CRAWLER_DRY_RUN=false
FOIA_MAX_DOCS_PER_SOURCE=1
FOIA_CRAWLER_INTERVAL_HOURS=6
```

The first crawler cycle therefore:

1. refreshes official source metadata from FOIA.gov;
2. visits active reading rooms;
3. archives at most one new/not-yet-archived record from each source;
4. extracts native text and applies bounded OCR where needed;
5. writes files to private B2;
6. keeps SQLite metadata/search state on the persistent local volume.

Already archived records do not consume the per-source quota. On a later cycle, the crawler can advance to the next not-yet-archived record for that source.

Once the initial breadth-first corpus is healthy, increase deliberately, for example:

```text
FOIA_MAX_DOCS_PER_SOURCE=3
```

Then restart the crawler service:

```bash
docker compose up -d crawler
```

The B2 application-level safety ceiling remains 9,000,000,000 bytes unless explicitly changed.

## 4. Watch ingestion

Crawler logs:

```bash
docker compose logs -f crawler
```

Web logs:

```bash
docker compose logs -f web
```

The homepage statistics provide a quick presentation-readiness check:

- records discovered
- full-text searchable records
- agencies represented
- active official sources

The About page provides additional archive/search counts.

## 5. Backup verification

The crawler automatically creates B2 SQLite snapshots according to the configured backup interval.

Force one before a presentation:

```bash
docker compose exec web python main.py backup-db --force
```

A recovery test can restore the newest backup to a separate database file:

```bash
docker compose exec web python main.py restore-db
```

Do not replace the live database during an ordinary demo rehearsal.

## 6. Presentation acceptance checklist

Before sharing the public URL, verify all of the following from the deployed site:

- homepage loads over HTTPS;
- live statistics are non-zero and plausible;
- a query matching only document body text returns the record with a snippet;
- an OCR-derived phrase is searchable;
- agency, file-type, and publication-date filters work;
- pagination works;
- a result opens its record-detail page;
- the detail page shows source provenance;
- the archived-copy link opens the private B2 object through a signed URL;
- the original-source link reaches the official government source;
- a nonsense query produces a clean empty state;
- mobile-width layout remains usable;
- `/healthz` returns HTTP 200.

## 7. Hosting requirements

A suitable demo host needs:

- Docker support, or the ability to run the same container image;
- persistent local disk for `/data`;
- outbound HTTPS access to FOIA.gov, agency sites, and Backblaze B2;
- enough CPU for occasional Tesseract OCR;
- an HTTPS public endpoint;
- secret environment-variable support.

For the current SQLite architecture, run **one crawler** and preferably one web replica on the same persistent local volume. Horizontal multi-host scaling is intentionally deferred.

## 8. Security after setup

Do not place B2 or FOIA.gov secrets in the image, repository, logs, or presentation materials.

Rotate any B2 application key that was exposed during development after the deployment and acceptance work is complete.
