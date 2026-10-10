# FOIA Aggregator — verified handoff status

**Snapshot:** October 10, 2026 (repository and PR API review). Refresh all claims before execution; this document is a recorded checkpoint, not an automatically updated live dashboard.

## Authoritative documents
- [Full development plan](DEVELOPMENT_PLAN.md): 10 approved operational FA units plus 14 added FC broad-coverage deliverables.
- [Agent operating rules](../AGENTS.md).
- [README](../README.md), [corpus expansion/storage](CORPUS_EXPANSION.md), [source access](SOURCE_ACCESS.md), [deployment](PRESENTATION_DEPLOYMENT.md), [admin](ADMIN_DASHBOARD.md).
- [Engine Room certification audit](https://github.com/neophilism/Engine-Room/issues/71).

## Baseline findings
- Code already has source discovery, curated and IC coverage, PAL/generic reading-room crawler, safe downloader, OCR/legacy Office text extraction, SQLite FTS5, B2 storage/recovery, FastAPI public search/admin, and manual/scheduled GitHub workflows. **Functionality/production success were not retested in this documentation change.**
- The approved .exechub/project.yml remains a **10-unit** operating expansion. Additional FC-01..14 scope is explicitly subsequent user-requested expansion; track its count separately with audited PR mappings.
- Repository source files include .github/workflows/{expand-corpus,refresh-corpus,preserve-recovery-artifact,audit-recovered-database}.yml. Autonomous scheduled refresh is intentionally gated by repository variable FOIA_AUTONOMOUS_REFRESH_ENABLED. Do not interpret the presence of a schedule as proof runs execute.
- Current documented safety caps: 8,500,000,000 bytes archived originals, 9,000,000,000 bytes actual versioned B2 target, within the nominal free 10 GB. Actual consumption/latest verified checkpoint at this review **not independently measured**, so do not assert corpus size, download count or headroom.
- Presentation Render free service is ephemeral/read-only and must load a separately verified B2 metadata checkpoint. Web heartbeat does not mean authoritative crawling happened.
- The paid future ~1 TB tier requires explicit user approval; meanwhile metadata/source catalog coverage should expand without mislabeling remote-only records as archived.

## Open PR collision check (2026-10-10)
1. [#48 — B2 native upload fallback for repeated S3 TLS failures](https://github.com/neophilism/FOIA_Aggregator/pull/48), OPEN when checked. Review security and tests first.
2. [#47 — promote recovered database to a new verified B2 checkpoint](https://github.com/neophilism/FOIA_Aggregator/pull/47), OPEN when checked; confirm preservation/monotonic audit evidence and dependency on upload fix.
3. [#41 — serialized scheduled crawling outside ephemeral Render](https://github.com/neophilism/FOIA_Aggregator/pull/41), OPEN when checked; review stale branch/conflict after others land. Never activate until reliable recovery/checkpoint is proven.

The merge and operations order is **evidence-driven**, not an automatic authorization to merge an untested or conflicting PR. Validate CI/statuses anew and perform a read-only B2/SQLite audit before any actual write. The script can operate with 0 new files if the cap is hit; distinguish graceful storage stop from a broken ingestion system.

## Next unblocked implementation work
- **Without access or spend:** run full unittest locally; verify source census across FOIA.gov, official current/historic reading rooms and all 18 IC elements; document exact gaps. Implement FC-01/02 source inventory and pagination fixtures, then public request-log/MuckRock adapters with permissions and provenance. Review PR changes but do not replicate them.
- **With existing B2/GitHub permissions:** resolve and test #48/#47; audit and restore checkpoint to separate SQLite file; then enable a single serialized crawler via #41 only after acceptance.
- **With later explicit paid-tier approval:** scale objects, metadata DB and public search after proving first-tier recovery and a monthly budget.

## Runbook
    python -m pip install -r requirements.txt
    python -m unittest discover -s tests -v
    python scripts/source_census.py --probe --output-dir source-census-results
    python main.py run --dry-run true --max-docs-per-source 10
    python main.py reconcile-b2
    python main.py restore-db

B2 commands require correctly configured server-side environment/secrets and must never print keys. Do not initiate a live archiving wave without an exclusive writer, actual remaining capacity and verified checkpoint.

## Release/handoff exit criteria
Every plan unit has explicit actual PR mapping and test evidence; one authoritative writer; two observed successful serialized scheduled runs; non-regressing B2 DB checkpoints and full object manifest; diverse primary/third-party FOIA source census and historical backfill status; date/format/body/OCR search and signed downloads on the deployed site; honest current storage/coverage dashboard; cost and API restrictions documented. Until then, do not certify broad nationwide FOIA archive coverage or unblock major dependent FOIA Bias Analysis / Send FOIA Log Requests buildout.
