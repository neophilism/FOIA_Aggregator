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

FOIA.gov component metadata is treated conservatively. Only fields explicitly describing FOIA reading rooms, FOIA libraries, proactive disclosures, or frequently requested records become crawl targets. General agency websites, request forms, generic resource fields, and unrelated links are not guessed into the source set.

Each discovered source tracks whether it is active, when it was last seen in a complete metadata refresh, its last successful crawl, and its most recent crawl error. A complete refresh marks previously known but unseen sources inactive; empty or unrecognized metadata refreshes preserve the existing active set as a safety measure.

## Bounded reading-room crawling

Reading rooms are crawled breadth-first within a strict boundary: the same hostname and the source URL's path subtree. The crawler follows ordinary internal page links and `rel="next"` pagination, but stops at configurable page, depth, and document ceilings. It never recursively follows an obvious document link as a page.

The generic crawler also:
- canonicalizes URLs and removes fragment-only duplicates
- recognizes common released-record formats including PDF, Office files, CSV, text, XML/JSON, email, images, audio, and video
- detects extensionless records from response MIME type or `Content-Disposition`
- applies a shared per-host request delay across reading rooms
- honors numeric `Retry-After` responses while retrying 429/5xx requests
- bounds HTML/page response size separately from archived document size

Crawl limits and request pacing are configured under `crawler` in `config/settings.yaml`. Difficult JavaScript/search-driven reading rooms remain candidates for later site-specific adapters rather than being crawled without bounds.

## Download safety

Document downloads are limited to public HTTP(S) destinations. Redirects are revalidated, private/loopback/link-local destinations are rejected, files are streamed to temporary files before atomic placement in the archive, and configurable size/retry limits live under `downloader` in `config/settings.yaml`.

Successful downloads record MIME type, file size, and SHA-256 integrity metadata. Failed attempts retain a classified status and error message for later inspection/retry.

## Web UI

Start the FastAPI server (e.g., with uvicorn):

```bash
uvicorn ui.server:app --reload
```

Then open http://127.0.0.1:8000/ to filter and download stored documents.
