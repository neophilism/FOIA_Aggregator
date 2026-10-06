# FOIA Aggregator

Minimal FOIA archive engine that discovers agency reading rooms from FOIA.gov (via the `agency_components` API), scrapes document links, and exposes a simple search UI.

## Setup

1. Install dependencies:

```bash
pip install -r requirements.txt
```

2. Export your FOIA.gov API key (or set `foia_hub.api_key` in `config/settings.yaml`):

```bash
export FOIA_API_KEY="MY_KEY"
```

3. Ensure the data directories exist (created automatically on first run) and adjust configuration in `config/settings.yaml` as needed.

## CLI Usage

Run a discovery-only crawl. Dry-run records discovered document metadata without downloading files:

```bash
python main.py run --dry-run true --max-docs-per-source 10
```

Run a bounded live crawl to download documents. Documents previously discovered during a dry run remain eligible for download:

```bash
python main.py run --dry-run false --max-docs-per-source 10
```

`max_docs_per_source` is a safety ceiling in both dry-run and live mode. In live mode it counts new or not-yet-archived candidates; already archived records do not consume the quota, so repeated bounded cycles continue advancing through a source.

Continuous mode:

```bash
python main.py daemon
```

## Source discovery

FOIA.gov component metadata is the primary discovery backbone and is treated conservatively. Explicit FOIA reading rooms, FOIA libraries, proactive disclosures, and frequently requested records become crawl targets. If a component has no explicit publication source, its component website is accepted only when the URL path itself is clearly FOIA-related and is not a request/status/submission endpoint.

Coverage is supplemented in three narrow ways:
- verified URL replacements move stale FOIA.gov reading-room URLs to current official agency URLs
- curated component-ID overrides fill current-agency gaps that FOIA.gov metadata does not expose
- an explicit Intelligence Community registry covers all 18 IC elements, including sub-elements that FOIA.gov collapses into a parent department or military service

Shared sources are deduplicated. For example, Air Force and Space Force intelligence share the Department of the Air Force electronic reading room, while Army INSCOM and the Office of Naval Intelligence also have dedicated supplemental roots.

Each discovered source tracks whether it is active, when it was last seen in a complete metadata refresh, its last successful crawl, and its most recent crawl error. A complete metadata refresh marks previously known but unseen sources inactive. Empty or structurally unrecognized metadata refreshes preserve the existing active set even when curated/supplemental sources are available, preventing an upstream schema change from mass-deactivating the archive.

Generate the complete federal source census with:

```bash
python scripts/source_census.py --probe --output-dir source-census-results
```

The report separates source identification from runner crawlability and emits JSON, component/source CSVs, an IC-element CSV, and Markdown. `scripts/ic_source_audit.py` performs a lighter live probe of just the 18 Intelligence Community elements.

## Bounded reading-room crawling

Reading rooms are crawled breadth-first within a strict boundary: the same hostname and the source URL's path subtree. The crawler follows ordinary internal page links and `rel="next"` pagination, but stops at configurable page, depth, and document ceilings. It never recursively follows an obvious document link as a page.

The generic crawler also:
- canonicalizes URLs and removes fragment-only duplicates
- recognizes common released-record formats including PDF, Office files, CSV, text, XML/JSON, email, images, audio, and video
- detects extensionless records from response MIME type or `Content-Disposition`
- applies a shared per-host request delay across reading rooms
- retries 429/500/502/503/504 and transient transport failures
- honors numeric or HTTP-date `Retry-After` and `X-RateLimit-Reset` with bounded waits
- bounds HTML/page response size separately from archived document size

Crawl limits and request pacing are configured under `crawler` in `config/settings.yaml`. Difficult JavaScript/search-driven reading rooms remain candidates for later site-specific adapters rather than being crawled without bounds.

## Download safety

Document downloads are limited to public HTTP(S) destinations. Redirects are revalidated, private/loopback/link-local destinations are rejected, files are streamed to temporary files before atomic placement in the archive, and configurable size/retry limits live under `downloader` in `config/settings.yaml`. Retry timing is capped so a hostile or malformed `Retry-After` cannot stall the crawler indefinitely.

Successful downloads record MIME type, file size, and SHA-256 integrity metadata. Failed attempts retain a classified status and error message for later inspection/retry.

## Archive storage

Local filesystem storage remains the default for development and tests. Production can use a private Backblaze B2 bucket through B2's S3-compatible API.

Set the non-secret bucket settings in `config/settings.yaml`, then provide credentials through environment variables:

```bash
export FOIA_STORAGE_BACKEND="b2"
export B2_KEY_ID="..."
export B2_APPLICATION_KEY="..."
export B2_BUCKET="..."
export B2_REGION="..."
export B2_ENDPOINT_URL="https://s3.<region>.backblazeb2.com"
```

The B2 application key should be scoped to the archive bucket with only the permissions required to read and write archive objects. Do not commit credentials to the repository.

B2 objects are content-addressed by SHA-256 under `documents/<hash-prefix>/<sha256>.<ext>`. If the same binary is encountered again, the existing object is reused rather than uploaded as another object. Uploads are verified by a follow-up object metadata request before the local staging file is removed.

The initial configuration sets `storage.b2.max_archive_bytes` to **9,000,000,000 bytes**. This is an application safety ceiling below the 10 GB free-storage allowance. When the cap is reached, metadata discovery continues but new document binaries are marked `storage_quota` and deferred. Raise or remove the ceiling when paid storage is available.

The SQLite database stores `storage_backend` and `storage_key` for each archived document. Existing local archive rows are automatically backfilled during schema migration. The web UI uses `/archive/{document_id}` for downloads: local records are served from the local archive and private B2 records receive a short-lived signed download URL.

### SQLite backups and recovery

When B2 storage is selected, the crawler also protects the metadata database with consistent compressed snapshots. The default policy is:
- create a backup at most once every 24 hours
- retain the newest 3 backups
- store them under `database-backups/`
- use SQLite's online backup API so WAL-mode writes do not produce an inconsistent copied database
- run SQLite `quick_check` before upload
- gzip the snapshot
- attach the uncompressed SQLite SHA-256 to B2 object metadata
- verify the uploaded object size before considering the backup successful

Backup failure does not terminate a successful crawl cycle; the next eligible cycle retries. If retention pruning fails after a successful upload, the new backup is preserved and the pruning failure is logged.

Create a backup manually:

```bash
python main.py backup-db --force
```

Restore the newest backup into a separate recovery database:

```bash
python main.py restore-db
```

By default this creates `data/foia_archive.restored.db` rather than replacing the live database. A specific backup or destination can be selected explicitly:

```bash
python main.py restore-db \
  --key database-backups/<backup>.sqlite.gz \
  --destination data/recovered.db
```

A restore verifies the B2 object metadata SHA-256 after decompression and then runs SQLite `quick_check` before making the recovery file available.

Backblaze B2 buckets are versioned by default. Backup retention therefore lists the concrete versions of expired backup objects and deletes them with explicit version IDs. A name-only S3 delete would create a delete marker while older object bytes remained stored. The B2 application key should therefore include `deleteFiles` in addition to list/read/write access. If pruning permissions are unavailable, backup creation still succeeds and the pruning failure is logged as a warning.

### Live B2 acceptance test

The repository includes a manual GitHub Actions workflow named **B2 live acceptance**. It does not contain credentials. Add these repository Actions secrets before running it:

- `B2_KEY_ID`
- `B2_APPLICATION_KEY`

The workflow defaults to region `us-east-005` and endpoint `https://s3.us-east-005.backblazeb2.com`. A bucket-scoped application key is preferred; when the key is scoped to exactly one bucket, the test discovers that bucket name from Backblaze authorization. An optional bucket-name input is available for keys that can access multiple buckets.

The live test validates the complete storage path:

1. application-key authorization and capabilities
2. content-addressed document upload and size verification
3. private signed download
4. SHA-256 deduplication without creating a second object version
5. two SQLite backups with retention set to one
6. permanent deletion of the expired backup version
7. verified SQLite restore
8. explicit version-ID cleanup of every acceptance-test object

The workflow never prints the application key or signed download URL.

## Full-text search

The archive can index extracted document body text in SQLite FTS5 while preserving the existing title/filename substring search.

Supported extractors:
- PDF text layers via `pypdf`
- OCR for image-only PDF pages via Tesseract, rendered with `pypdfium2`
- OCR for PNG, JPEG/JPG, TIFF/TIF images
- DOCX paragraphs and tables via `python-docx`
- legacy DOC via `antiword`
- legacy XLS via `xls2csv`
- legacy PPT via `catppt`
- text-like formats including TXT, CSV, JSON, XML, RTF, and EML

Extraction runs **before** a newly downloaded file is committed to its archive backend. This matters for B2 because the verified upload path removes the temporary local staging file after upload. Extraction failures do not block archival of the original record.

Each archived document receives explicit extraction state in `document_text`, including:
- `indexed`
- `indexed_truncated`
- `empty` — extraction/OCR ran but found no searchable text
- `ocr_unavailable` — Tesseract is not installed or unavailable
- `ocr_failed` — OCR was attempted but failed
- `unsupported`
- `extraction_failed`

`document_text.extraction_method` records whether indexed text came from `native_text`, `ocr`, `mixed`, or `legacy_office`.

The FTS index is keyed by the document ID and is refreshed atomically whenever extracted text is replaced. If an archived file is later found missing and a re-download fails, its stale extracted text is removed from search as well.

Per-document indexed text is bounded by:

```yaml
search:
  max_indexed_chars_per_document: 5000000
```

This prevents a single unusually large release from dominating SQLite memory or disk. OCR output uses the same ceiling.

Existing archived documents can be indexed without re-crawling agency websites:

```bash
python main.py reindex-text
```

Optional controls:

```bash
python main.py reindex-text --limit 100
python main.py reindex-text --force
```

The backfill command reads local archive files directly. For B2-backed documents it downloads only the archived object into a temporary staging directory, extracts/indexes the text, and removes the temporary copy.

### OCR

OCR is enabled by default in `config/settings.yaml`:

```yaml
ocr:
  enabled: true
  languages: "eng"
  dpi: 200
  max_pages_per_document: 50
  page_timeout_seconds: 45
  max_image_megapixels: 20
```

Tesseract must be installed on the runtime host. On Debian/Ubuntu:

```bash
sudo apt-get install tesseract-ocr
```

The crawler does not OCR a whole PDF indiscriminately. It keeps native text on pages that already have a text layer and OCRs only pages where native extraction returns no text. Mixed PDFs therefore retain native text where possible while adding OCR text for scanned pages.

OCR work is bounded by page count, render DPI, per-page Tesseract timeout, image megapixels, and the existing indexed-character ceiling. If Tesseract is unavailable or OCR fails, the original file is still archived and the failure is recorded in extraction metadata rather than failing the document download.

Audio/video transcription remains a later phase. Legacy DOC/XLS/PPT extraction is supported with bounded subprocess timeouts and the same indexed-character ceiling used by other extractors.

## Continuous-operation resilience

The `daemon` command is designed to stay alive across ordinary application and network failures.

- startup/config errors are caught by the daemon supervisor and retried after `crawler.daemon_error_retry_seconds`
- one unexpected reading-room exception does not stop later sources
- unexpected per-source failures are persisted so the source enters cooldown
- recently failed sources are skipped for `crawler.failed_source_retry_minutes` before being tried again
- SQLite/database infrastructure failures abort only the current cycle and trigger daemon backoff instead of generating hundreds of repeated source failures
- source crawl database connections are closed in `finally` blocks even when parser/storage code raises
- failed document downloads have their own retry cooldowns; permanent-style failures such as blocked URLs, oversized files, and HTML/content mismatches use a longer cooldown
- FOIA.gov metadata refresh cadence is independent of crawl cadence, so rapid crawl cycles do not repeatedly hit FOIA.gov
- metadata failures use a shorter controlled retry cadence while the crawler continues with the last known source set
- malformed/non-finite cadence values fall back to safe defaults rather than terminating the process
- `KeyboardInterrupt` and normal process-termination semantics are intentionally not swallowed

Application-level resilience cannot recover from process-external failures such as SIGKILL, host reboot, kernel OOM termination, or catastrophic filesystem/database corruption. Production deployment should therefore also use an external process supervisor (for example systemd or a container restart policy) so the process itself is restarted if the operating system terminates it.

## Database durability

The archive uses SQLite WAL mode, a busy timeout, and enforced foreign keys on application connections. Schema upgrades are recorded in `schema_migrations` and applied automatically by `init_db()`; existing archives do not need to be deleted when new migrations are added.

A document URL remains unique in `documents`, while `document_sources` records every reading room that publishes the same document. The original `documents.reading_room_id` is retained as the first/primary source for backward compatibility. Agency and office filters also match secondary source relationships.

Indexes cover the primary browse/filter fields and crawler status fields so archive growth does not require full-table scans for ordinary queries.

## Web UI

Start the FastAPI server:

```bash
python -m uvicorn ui.server:app --reload --host 0.0.0.0 --port 8000
```

When running locally, open http://127.0.0.1:8000/. In GitHub Codespaces, open the **Ports** panel and use **Open in Browser** for forwarded port **8000**.

The default search spans all agencies and offices. Selecting an agency or office only narrows the result set.

The public web layer also includes launch-oriented behavior:
- canonical and Open Graph metadata for public/shareable URLs
- `robots.txt` and a record-aware `sitemap.xml`
- a branded HTML 404 page
- security headers for framing, MIME sniffing, referrer leakage, browser permissions, and HTTPS HSTS
- a live "archive updated" timestamp derived from the latest discovery/download/extraction activity

Set `FOIA_PUBLIC_BASE_URL` when a stable custom/public origin is known. If it is omitted, canonical URLs, robots, and sitemap derive their origin from the incoming request.

The public UI is presentation-oriented rather than a database table. It includes:
- a prominent cross-agency full-text search
- live archive statistics derived from the SQLite database
- collapsible agency, office, file-type, date, sorting, and page-size filters
- result cards with bounded body-text snippets when a query matched inside a document
- result counts and 25/50/100-row pagination
- direct links to the archived copy and original government source
- per-record detail pages at `/record/{document_id}`
- source provenance, integrity/file metadata, and extraction/OCR metadata on record pages
- a public `/about` page explaining scope, coverage, preservation, and search methodology

The homepage statistics are not hard-coded; they report the current database's agencies, active official sources, discovered records, archived records, full-text searchable records, and OCR-assisted records. Search snippets use SQLite FTS5's bounded `snippet()` function so a results page does not load entire indexed documents into memory.

Pagination removes the former silent 200-result ceiling. Full-text search uses SQLite FTS5 while the existing title/filename substring behavior remains available through the same search field.

## Containerized presentation deployment

The repository includes a provider-neutral Docker deployment with separate web and crawler services sharing a persistent local SQLite volume while document binaries live in private Backblaze B2.

Quick start:

```bash
cp .env.example .env
# Fill in FOIA_API_KEY and B2 credentials.
docker compose up -d --build
```

The web service exposes `/healthz` for deployment health checks. The default demo crawler is live but intentionally conservative: one new/not-yet-archived record per source per six-hour cycle. Already archived records do not consume that quota, so repeated cycles expand the corpus gradually.

See `docs/PRESENTATION_DEPLOYMENT.md` for deployment requirements, initial corpus guidance, backup checks, and the presentation acceptance checklist.

A fresh deployment can automatically restore the newest verified SQLite snapshot from B2 before the web process starts, so a previously seeded corpus appears immediately rather than beginning from an empty local database. Bucket-scoped B2 keys can also auto-discover their single allowed bucket when `B2_BUCKET` is not supplied.

For a short presentation sequence based on the real seeded corpus, see `docs/DEMO_SCRIPT.md`.

For a zero-cost public presentation deployment, the repository also includes `render.yaml` for a Render Free Docker web service. The free service can use ephemeral local storage because each cold start restores the newest verified SQLite snapshot from B2. See `docs/RENDER_DEMO.md`.

[Deploy FOIA Aggregator to Render](https://render.com/deploy?repo=https://github.com/neophilism/FOIA_Aggregator)

The Blueprint prompts for the two Backblaze secrets during initial creation and otherwise uses the repository-defined deployment configuration.

## Administrator dashboard

A protected read-only administrator dashboard is available at `/admin` when `FOIA_ADMIN_PASSWORD` is configured. It summarizes B2 capacity, archive growth, source health, 403/rate-limit failures, download/extraction pipeline status, backup freshness, database state, and recent ingestion activity.

The dashboard uses HTTP Basic authentication and behaves like a 404 when no administrator password is configured. See `docs/ADMIN_DASHBOARD.md`.

## Corpus growth and autonomous refresh

The current free-tier archive is intentionally capacity-aware. Manual expansion can grow the corpus aggressively toward the Backblaze allowance while measuring actual stored bytes across B2 object versions and preserving verified SQLite checkpoints after every crawl wave.

Use the GitHub Actions workflow **Expand corpus toward B2 capacity** for large one-off expansion runs.

The repository also contains a twice-weekly **Autonomous corpus refresh** workflow for long-term operation. Scheduled refresh is deliberately disabled until storage is upgraded; it only activates when the repository variable `FOIA_AUTONOMOUS_REFRESH_ENABLED` is explicitly set to `true`. Manual refresh runs remain available.

See `docs/CORPUS_EXPANSION.md` for the current 8.5 GB document cap, 9.0 GB total B2 target, safety rationale, and post-upgrade activation procedure.

## Future scope: international access-to-information systems

The MVP is focused on U.S. federal FOIA sources. A future platform expansion should add comparable public-records and access-to-information systems outside the United States, especially jurisdictions where released records or prior request/response logs are publicly searchable.

Initial candidates include Australia, Brazil, Mexico, France, Canada, Ireland, Norway, Sweden, New Zealand, and the United Kingdom. International coverage should preserve each jurisdiction's own terminology, provenance, disclosure rules, and legal context rather than assuming every system operates like U.S. FOIA.
