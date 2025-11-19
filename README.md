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

Run a single crawl (dry-run defaults to limiting downloads to the configured maximum per reading room):

```bash
python main.py run --dry-run true --max-docs-per-source 10
```

Disable the dry-run cap:

```bash
python main.py run --dry-run false
```

Continuous mode:

```bash
python main.py daemon
```

## Web UI

Start the FastAPI server (e.g., with uvicorn):

```bash
uvicorn ui.server:app --reload
```

Then open http://127.0.0.1:8000/ to filter and download stored documents.
