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

Run a discovery-only crawl. Dry-run records discovered document metadata without downloading files and limits newly discovered documents per reading room:

```bash
python main.py run --dry-run true --max-docs-per-source 10
```

Run a live crawl to download documents. Documents previously discovered during a dry run remain eligible for download:

```bash
python main.py run --dry-run false
```

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

## Database durability

The archive uses SQLite WAL mode, a busy timeout, and enforced foreign keys on application connections. Schema upgrades are recorded in `schema_migrations` and applied automatically by `init_db()`; existing archives do not need to be deleted when new migrations are added.

A document URL remains unique in `documents`, while `document_sources` records every reading room that publishes the same document. The original `documents.reading_room_id` is retained as the first/primary source for backward compatibility. Agency and office filters also match secondary source relationships.

Indexes cover the primary browse/filter fields and crawler status fields so archive growth does not require full-table scans for ordinary queries.

## Web UI

Start the FastAPI server (e.g., with uvicorn):

```bash
uvicorn ui.server:app --reload
```

Then open http://127.0.0.1:8000/ to filter and download stored documents.
