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

## Download safety

Document downloads are limited to public HTTP(S) destinations. Redirects are revalidated, private/loopback/link-local destinations are rejected, files are streamed to temporary files before atomic placement in the archive, and configurable size/retry limits live under `downloader` in `config/settings.yaml`.

Successful downloads record MIME type, file size, and SHA-256 integrity metadata. Failed attempts retain a classified status and error message for later inspection/retry.

## Web UI

Start the FastAPI server (e.g., with uvicorn):

```bash
uvicorn ui.server:app --reload
```

Then open http://127.0.0.1:8000/ to filter and download stored documents.
