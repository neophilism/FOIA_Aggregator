# FOIA Aggregator — complete development, corpus-expansion, and independent-account handoff plan

**Product:** neophilism/FOIA_Aggregator  
**Authoritative scope:** original October 5–10, 2026 FOIA archive development decisions, subsequent B2/Render operations, and the October 10 instruction to aggregate **all or very many more available FOIA records across all accessible sources** before prioritizing downstream analytical/request projects.  
**Plan baseline:** 2026-10-10. This is a plan, not a claim of deployment, corpus size, passed CI, or approved spending. **Do not mark work completed from the presence of this document.**

## 1. Mission, outcome, and scope boundaries

Build and operate a comprehensive, independently searchable, provenance-preserving archive of **publicly available U.S. federal FOIA disclosure materials**. Systematically discover as many eligible sources as practicable, collect or index their published records, extract searchable text, preserve originals when storage and usage rights permit, and grow the corpus incrementally with transparent source and record coverage.

This is **not** merely a small working demonstration, a set of bookmarks, or a crawler that touches each government website once. The eventual objective is deep coverage of federal agencies/components and other lawful public release repositories, across many years and administrations. The initial Backblaze B2 free tier is an operating phase, **not a final corpus-size target**.

**Required source classes:** FOIA.gov directory/component metadata (source discovery, not a promise that FOIA.gov itself hosts all releases); federal agency and component reading rooms; FOIA libraries, proactive disclosure pages, frequently requested and historically released files; agency FOIA request/release logs; agency public search/API endpoints and verified official downloads; all 18 Intelligence Community elements with accurate component attribution; public releases and associated request metadata on MuckRock or comparable lawfully accessible repositories; and publicly indexed supplementary historical release collections. New source classes are additive, not excuses to stop crawling the originals.

**Archive outputs:** durable source census; document catalog/metadata and release/request relationships; content-addressed originals and versioned provenance; normalized/extracted text including scans and legacy office formats; searchable agency/office, source, file type and date filters; integrity/recovery; an honest, data-backed public site and protected operator console; automated resumable ingestion; evidence of coverage gaps, source blocks and failures.

**Non-goals for this repository:** filing FOIA requests at scale, partisan-bias classification, claims to have every government record, accessing unpublished/classified/private systems, bypassing CAPTCHA/login/rate/robots controls, and routine scraping of private personal data. FOIA_Bias_Analysis and Send_FOIA_Log_Requests are separate dependent projects and **not the current priority** until this archive has substantial, geographically/institutionally/temporally diverse coverage. International freedom-of-information expansion is deferred.

A public record link or metadata row is **discovered**, not **archived**; an archived binary is not automatically **text-indexed**; a source visited once is not **fully covered**. Preserve these distinctions everywhere.

## 2. Baseline code, design, and source of truth

Current repository already provides: Python CLI (main.py); FOIA.gov/curated/IC source discovery; bounded generic and PAL/agency crawling; source census scripts; safe downloader; SQLite WAL metadata and FTS5 indexing; native text/PDF, scanned-PDF/image OCR, DOC/DOCX, XLS, PPT and text extraction; content-addressed private B2 storage; B2-aware compressed SQLite backup/recovery; FastAPI public search, record pages and admin dashboard; Docker/Render presentation; corpus seeding, expansion and opt-in refresh GitHub Actions; and a broad Python test suite. **Verify actual functioning, do not rewrite without evidence.**

Read first: README.md; docs/CORPUS_EXPANSION.md; docs/SOURCE_ACCESS.md; docs/ADMIN_DASHBOARD.md; docs/PRESENTATION_DEPLOYMENT.md; docs/LEGACY_OFFICE_EXTRACTION.md; config/settings.yaml; .env.example; .exechub/project.yml; .github/workflows/*; foia_archive/{discovery,source_census,scraper_core,archive_storage,database_backup,storage,text_extraction,text_index}.py; scripts/*.py and tests/*.py.

**Operational architecture as currently committed:**
- Metadata and search: SQLite on one writer's local disk. WAL works only with carefully coordinated local access; do not concurrently modify copies of the same logical database from Render and independent GitHub runners.
- Document objects: private B2, content-addressed by hash. Actual B2 billing/storage includes all object versions, database snapshots and incidental objects.
- Free B2 policy: **8,500,000,000-byte document cap**, **9,000,000,000-byte actual usage stop target**, within nominal 10 GB free storage. Keep meaningful headroom; no silent paid overage.
- **Class B exhaustion must not recur:** normal ingestion avoids per-file B2 HEAD and trusts durable SQLite upload results; use **paginated Class C list-object manifest reconciliation**, plus verified read/restore and checksum checks at the appropriate gates.
- The Render free web service is an **ephemeral, read-only presentation replica** that restores a verified B2 SQLite snapshot at startup. A sleeping web process is not a durable ingestion runner. The planned authoritative writer is serialized GitHub Actions or another genuinely persistent, singular scheduler. Do not activate concurrent Render-background ingestion.
- Errors, retries, source blocks, license/access restrictions and budget ceilings must be visible instead of silently skipped. An object-upload 200 is not enough to mark checkpoint/data recovery verified; prove retrieval, decompression, integrity and monotonicity.

**Current independent-account collision watch, as observed 2026-10-10:** PR #41 (single-writer scheduled crawling), #47 (recovery checkpoint promotion), and #48 (B2 native upload fallback after confirmed S3 TLS failures) were OPEN. They may change before execution. Always refresh PR/head/check/merge/deployment state first. Do not duplicate or prematurely merge dependent work. Old Oct 6 recovery artifacts and their 90-day preservation should be checked before expiration. Never promote a less-complete database over a more-complete verified snapshot.

## 3. Approved ten-unit operational expansion, preserving the existing denominator

Existing .exechub/project.yml defines **10 approved planned units**, arranged as 2 each across five themes. The following IDs are **plan work IDs**, not numeric GitHub PR #s. Maintain exact evidence mapping and do not automatically count earlier, unrelated PRs toward these units. More than one PR may fulfill a unit, or one PR may serve multiple units with explicit acceptance evidence.

### FA-01 — Recovery baseline, database/manifest inventory
**Prerequisites:** access to current B2 bucket and the available recovery snapshots, read-only first.
**Implement:** inventory all authoritative SQLite checkpoint candidates; compare schema versions, document URLs, B2 object keys, content hashes, source rows, FTS rows and backups. Establish monotonic subset/superset rules and checkpoint lineage. Validate SQLite quick_check/integrity_check; publish audit report without secrets or private signed URLs.
**Acceptance:** recovery selection proves it will not drop unique archived documents, URLs, source links or text rows; audit detects missing objects, corrupted backups and divergent restore states. Record chosen authoritative snapshot and restore evidence.
**Relevant existing work:** scripts/audit_recovery_database.py, existing artifact workflow, PR #47; do not overwrite a verified newer snapshot.

### FA-02 — Durable B2 checkpointing, restore and billing-safe reconciliation
**Prerequisites:** FA-01.
**Implement:** bounded robust uploads (including PR #48 review), reliable native-B2 fallback only for defined ambiguous S3 TLS failures, exact-key list reconciliation before retrials, separate restore verification, retention/version deletion and failure-safe staging. Distinguish content-addressed document keys, database snapshots and diagnostic backups. Single checkpoint authority.
**Acceptance:** simulated EOF/unknown upload status, 403, duplicate retry, stale manifest, partial multi-part, restore corruption, Class B quota-exhaustion and interrupted job cases pass; no silent duplicate versions, no disabled TLS, no key leakage. Demonstrate real B2 upload/list/restore through the manual acceptance workflow when credentials permit.

### FA-03 — One authoritative scheduled corpus writer
**Prerequisites:** FA-01 and FA-02, CI and checkpoint evidence.
**Implement:** reconcile PR #41 with the current scheduled expansion/seed/refresh workflows; put all mutating GitHub Actions workflows in one effective concurrency/lease domain. Avoid two writers by default, including manual workflows and web startup. Add run IDs, crash-safe resume, mutual-exclusion assertions, timeout behavior, lock expiry and a stopped-at-cap result.
**Acceptance:** parallel scheduler dispatches never commit conflicting database checkpoints, timed-out runs resume with unchanged evidence, and Render cannot silently start a second crawler. After merging, prove at least two real, sequential production ingestion runs and resulting verified backups.

### FA-04 — Self-healing recurring ingestion and operations
**Prerequisites:** FA-03.
**Implement:** checkpoint between crawl waves, source/cursor progress, rate-limited host queues, source retry/cooldowns, outage isolation, backlog fairness, failure escalation and run completion receipts. Separate aggressive catch-up from affordable normal refresh. Do not enable paid storage or raise caps without permission.
**Acceptance:** repeated bounded runs add new archived records without duplicate bytes; killed/rerun worker skips already completed units; scheduled refresh resumes following network/B2 failures. Each run reports examined sources, records discovered/archived/indexed, bytes, costs and stop reason.

### FA-05 — Comprehensive official source census and correction register
**Prerequisites:** FA-03 or read-only source discovery can begin earlier.
**Implement:** use FOIA.gov agency/component directory, all 18 IC sources, cabinet/independent and subordinate components, official FOIA page redirects, historic source URLs, proactive disclosure collections and agency FOIA logs. Inventory every candidate source with root, legal/public access mode, parent agency, last verified URL, adapter type, published item estimate when available, and crawlability. Review components missing FOIA pages instead of silently treating them as negative.
**Acceptance:** versioned census with attempted/eligible/blocked/manual/not-yet-reviewed counts; no artificial claim that metadata-only FOIA.gov is a universal disclosed-document corpus; all named agencies and all 18 IC elements accounted for; incremental reruns preserve and audit source changes.

### FA-06 — Repair difficult sources and complete extraction/backfill
**Prerequisites:** FA-05 and FA-04 for large runs.
**Implement:** source-specific parsers for JavaScript/API/search/PAL/paginated archives, date filters, nested release directories and request logs; add backfills for existing B2 records; improve PDF OCR and legacy DOC/XLS/PPT/EML/CSV/XML extraction with labeled failure states. Categorize persistent 403 as unavailable, not a candidate for evasion. Preserve metadata when documents cannot be mirrored.
**Acceptance:** fixture-backed valid cases and adversarial parser tests per adapter, integration run on approved public source; measurable decreases in access/parsing/index failure queues; no large source silently returns only page one.

### FA-07 — Evidence-backed Engine Room progress and ingestion signals
**Prerequisites:** FA-01–06 evidence conventions.
**Implement:** reconcile approved ten-unit manifest with actual PR(s) and additions; expose counts, latest run/restore/backup freshness, verified archive size, source completion and crawler blockers in structured machine-readable status. Pull or publish metrics using authenticated connectors; no guessed percentages.
**Acceptance:** Engine Room shows exact plan denominator, real merged mapped units, and separate deployed/synchronized/recovered states. Stale, empty, unauthenticated or failed feeds show UNVERIFIED/DEGRADED, never healthy or 100%.

### FA-08 — Protected operator dashboard, actionable alerting
**Prerequisites:** FA-04–07.
**Implement:** extend /admin to source and adapter queues, HTTP 403/429 classes, never-crawled/never-archived sources, object/checkpoint reconciliation, OCR failures, source-vs-object coverage, ingestion throughput and cost/headroom forecasts. Add alert thresholds and safe manual retry/quarantine controls with permission checks.
**Acceptance:** test access denial when unconfigured and auth failures; demonstrate one known bad source and failed checkpoint surfaced with enough evidence to resolve; administrator actions cannot bypass storage or source controls; no secrets/exposed signed links.

### FA-09 — Public archive and research-readiness acceptance
**Prerequisites:** FA-04–08.
**Implement:** search with body snippets and provenance, source/agency/office/type/year filters and stable pagination; record/source link integrity; dates (publication, request, release, crawl) labeled separately; accessibility, robots/canonical/sitemap, document-download authorization, bounded query protections, clear indexed-vs-not text badges and methodology coverage page.
**Acceptance:** real title-only/body-only/OCR/legacy-text queries find expected archived records; agency/date predicates, pagination and downloads work on production; sample of 100 stratified records manually audited for attribution, broken links, false date association, text extraction and corruption. No demo/synthetic corpus misrepresented as complete.

### FA-10 — Production release and costed scale architecture
**Prerequisites:** FA-01–09.
**Implement:** production release/rollback, image pinning, database restore rehearsal, monitoring and incident playbooks; partition planning for eventual 1 TB+ B2 objects and durable multi-host metadata/search, with staged migration from SQLite to PostgreSQL or another reviewed production store **only once warranted**. Explicit cost and user authorization gate for storage, compute or transcriptions beyond configured free services.
**Acceptance:** measured restore time, data monotonicity, read/search performance, successful public site checks, service freshness, cost projection and owner-access list. Document next upper limit without claiming 1 TB allocated or purchasing it.

## 4. Additional high-coverage program — explicitly added October 10, 2026

The original ten-unit manifest above is **not** the full end state for the newly emphasized requirement: **aggregating all or very many more available documents across all sources**. The following are additive, sequenced deliverables (coverage IDs **FC-01..FC-14**) that belong in the engineering backlog **in addition to** the ten-unit denominator. They are not retroactive original PRs or automatically mapped to arbitrary GitHub PR numbers. On approval/versioning in Engine Room, add explicit planned-work items and maintain an additions denominator.

Every FC item must be implemented in PR-sized increments with clear source inventory, observable acceptance and tests.

### FC-01 — Universal federal source inventory
**Dependencies:** FA-05. Assemble canonical component/agency inventory and links to all known official current/historical FOIA repositories; include independent agencies, sub-agencies and each IC element. Preserve redirects, retired aliases, date spans and last verification. **Done:** no unclassified U.S. federal component silently omitted from the census; each row has a verification/search status, including UNKNOWN.

### FC-02 — Source enumeration and full pagination
**Dependencies:** FA-06, FC-01. For each source, identify sitemap, API, browse pagination, result counts and date coverage; implement persistent source cursor/window checkpoints, date slicing and anti-duplication. **Done:** fixture and live sampled source establish full known page/window traversal rather than top-page-only collection, with documented unenumerable cases.

### FC-03 — FOIA request/release log normalization
**Dependencies:** FC-01–02. Ingest official CSV, XLS, PDF and scanned request logs as structured request records (agency, tracking ID when public, request/closure dates, disposition, disclosed record references). Do not confuse a log's row with the underlying released document. **Done:** every log adapter preserves raw source, row-level provenance, failed-row queue and per-column confidence; matches to binaries must be evidence-backed.

### FC-04 — Third-party released FOIA records and MuckRock integration
**Dependencies:** FC-01–03. Build lawful public-source adapters for MuckRock request communications/files/release attachments plus other recognized public release archives. Attribute original agency, requester/publication service, original request and file hashes separately. Honor published endpoints, rate limits, rights and removal/access obligations; do not assume a third-party request page contains a released binary. **Done:** official-vs-third-party crosslinks, sample ingestion, nonduplicate archive objects, and accurate request-to-response provenance.

### FC-05 — Historical and deep-archive backfill
**Dependencies:** FC-02–04. Build age-bucketed backfill for pre-existing published releases and discontinued reading rooms, with manual source repair where needed. Track years and administrations as metadata, not political inference. **Done:** per-source oldest/newest verified release windows, expected vs seen item counts where knowable, missed-period queue and replayable backfill checkpoints.

### FC-06 — Adaptive adapters for API and dynamic portals
**Dependencies:** FC-02. Extend site-type registry and source-specific read-only adapters for public JavaScript/API/ASP/PAL platforms and downloadable results lists. Use permitted public interfaces, minimal compatible headers and legitimate documentation. **Done:** fixtures plus audited field sample; persistent 403/CAPTCHA is BLOCKED with source URL and date, **not evaded**.

### FC-07 — Content-type completeness and enrichment
**Dependencies:** FA-06, FC-04–06. Extend bounded text extraction for multi-part zip/bundles where safe, mixed scanned PDFs, encoded archives, legacy formats, metadata-only images and optional public audio/video transcript metadata. Use antivirus/resource limits and extraction provenance. **Done:** MIME signature tests, decompression bomb rejection, percentage indexed by format, distinct UNSUPPORTED/FAILED/EMPTY statuses.

### FC-08 — Cross-source identity and dedupe provenance
**Dependencies:** FC-03–07. Distinguish URL duplicates, identical bytes, different redactions/versions, multiple FOIA releases of same record, log request IDs and agency changes. Retain all source associations and immutable content hashes. **Done:** collisions and redacted revisions do not destroy original or combine unrelated records; search displays related public versions separately when materially different.

### FC-09 — Metadata-first growth past binary cap
**Dependencies:** FA-02, FC-01–08. While free storage is full, keep discovering **record metadata and source pointers** under separate safe quota and dedupe controls; enqueue original bytes for future acquisition with priority/sizes/availability. Public UI labels remote-only vs preserved local/B2 records and broken remote links. **Done:** census and catalog can grow even with zero B2 binary headroom, without claiming remote-only records are preserved or searchable inside.

### FC-10 — Massive corpus worker throughput and backpressure
**Dependencies:** FA-03–04, FC-09. Add work scheduling prioritizing untouched agencies and deep source pagination, per-host ceilings, retry state, durable queues, idempotent object/catalog writes and worker crash recovery. Benchmark in bounded safe tests; scale horizontally **only** after migrating away from one SQLite writer. **Done:** throughput and recovery measured, no double-charged/persisted bytes, source fairness and cap stops proven.

### FC-11 — Paid storage and metadata search migration, contingent gate
**Dependencies:** FA-10, FC-09–10; **requires explicit spending/credentials authorization**. Produce a tested plan for ~1 TB+ B2 and appropriate metadata/search architecture (PostgreSQL/FTS and/or dedicated index if justified), batch migration, backfill, shadow-read comparison, rollback and accountable monthly price. **Done:** approve no costs without owner authorization; no data loss on migration, verified object-key continuity, public query parity before cutover.

### FC-12 — Publication quality and corpus completeness dashboard
**Dependencies:** FC-01–11 as applicable. Display distinct denominators: discovered agencies/components, official source URLs verified, sources enumerated, items listed by source, records cataloged, binaries preserved, bytes, text indexed, OCR, verified archives, inaccessible/unresolved/unknown. Show coverage by agency/year/source-type and last verified crawl. **Done:** the public and owner can see precisely which portion is unknown; do not substitute data volume for federal coverage.

### FC-13 — Research-ready public exports/API
**Dependencies:** FC-03, FC-08, FC-12. Add paginated versioned catalog exports, source/legal provenance, stable IDs, repeatable snapshot manifests and documented fields for downstream FOIA bias analyses/log-request research; API rate limits, private-field redaction and licensing. **Done:** downloaded snapshot replays, consistent counts, no hidden synthetic enrichment, a consumer can join records to source/release/agency/year reliably.

### FC-14 — Broad corpus acceptance and downstream unblock decision
**Dependencies:** FC-01–13 to the extent budget/allocation allows. Publish an evidence-backed corpus review stratified by agency, official/third-party source, IC coverage, time period, document type, disclosure status and extraction method. Prove representative full-source pagination, archive hashes, query results, and recovery from loss. Quantify missing known files and blocked sources. **Done:** meaningful many-source and many-year coverage, reproducible source inventory and public audit, independently verified catalog/index quality. **Do not invent a record-count or percentage of all public FOIA files when the denominator cannot be known.**

**Downstream gating:** FOIA_Bias_Analysis may conduct design/prototype work but should not make broad partisan statistical claims until FC-14 confirms representative coverage across administrations, agencies and disclosure categories. Send_FOIA_Log_Requests may prepare formats/directory data but should not execute large campaigns before the archive source census and log dedupe clarify what public records are already available. These are intentional priority dependencies, not claims those projects are already implemented.

## 5. Implementation discipline and data contracts

**Source record:** canonical URL/aliases, FOIA component, agency, source kind, official/third-party flag, access classification, crawl strategy, discovered/last checked/last success times, last enumeration cursor/window, expected-public item count when knowable, failure/error detail and provenance.

**Document record:** stable catalog ID, public record identifier when published, canonical source and all secondary sources, request/release references, request/publication/release/crawl dates as distinct typed fields, MIME and filename, source URL, original-content SHA-256, B2 key/size/version status, text extraction/index state and timestamp, preservation/access classification, optional relationship to alternative redacted versions. Unknown fields remain null, never fabricated.

**Run evidence:** workflow/run/commit ID, exclusive-writer lease, start/end, source cursor, source count, pagination, discovered/cataloged/archived/indexed deltas, by-source/format errors, verified checkpoint ID, actual B2 versioned bytes, archive safety cap and stop reason.

**Data safety:** safe public HTTP(S) URLs only, redirect revalidation, local/private-address rejection, bounded responses/decompression, cautious User-Agent, no secret logging; malware/content validation where applicable; backups tested through a separate restore target. Legal access restrictions are hard stops. Never expose nonpublic personal/requester data merely because a source published operational metadata.

**When refactoring:** retain SQLite schema migration/version procedures, the public /archive and /record URLs where possible, existing tests, signed private B2 download behavior, de-duplication and lineage. Save source fixtures (or synthetic redacted fixtures), not real secret tokens or sensitive nonpublic records.

## 6. Test matrix and concrete commands

Baseline local preparation (inspect README and pinned requirements first):

    python -m pip install -r requirements.txt
    python -m unittest discover -s tests -v

The current tests use Python's standard unittest module; use the existing discovery command above. If a later CI revision changes the runner, follow its authoritative tested invocation instead. Additional commands exposed by main.py:

    python main.py run --dry-run true --max-docs-per-source 10
    python main.py run --dry-run false --max-docs-per-source 10
    python main.py reindex-text --limit 100
    python main.py reconcile-b2
    python main.py backup-db --force
    python main.py restore-db
    python scripts/source_census.py --probe --output-dir source-census-results

Use bounded fixtures for CI. Real network/B2 and external-site tests belong in explicit credentialed/manual acceptance jobs. Mandatory negative tests: no double-write across schedulers; B2 403/TLS EOF; versioned backups; no DB regression; Class C paging at >1,000 objects; cap at 8.5/9 GB; HTTP 403/429, redirect to private network, CAPTCHA/access restriction; broken source pagination; duplicate/re-redacted records; huge PDF/ZIP and OCR timeout; empty OCR search; corrupt document; update-after-deletion; stale search row; HTML-as-PDF spoofing; concurrent worker retry. Every new source adapter requires a reproducible parser fixture and one documented read-only live verification.

## 7. Deployment, monitoring, cost and blockers

The Render Free app may be live with stale contents. **DO NOT infer continuous ingestion from public /healthz, green CI, or presence of a cron expression.** Verify commit, current verified B2 snapshot and newest complete scheduler run. If the web process restores the last checkpoint, a newly created record is not public until next verified snapshot and presentation refresh/redeploy. Avoid any unrelated Render workspace.

Pre-release signoff: unique authoritative writer; two successful serialized runs; no missing archive keys; verified B2 database restore; source coverage report; public text/OCR searches; downloadable archived copy; source and access safety; storage headroom and cost; admin health/status and Engine Room telemetry; disaster-recovery runbook; documented outstanding sources and failure classes.

**External access gates**: authorized B2 bucket-scoped key, FOIA.gov key (or its published testing option if appropriate), GitHub Actions write/run permissions, Render deploy permission, app-admin password and any later paid-host or storage authorization. Do not expose values in GitHub. Existing production credentials and B2 storage usage must be checked afresh, not assumed from past chats. Never purchase storage, transcription or other paid services without explicit approval.

## 8. Autonomous second-account operating instructions

1. Check main, this plan, README, existing changes/branches/PRs and actual Render/GitHub Actions/B2 health. Specifically inspect current equivalents of PR #41, #47 and #48; review CI and merge order. The latest current PR state overrides this October 10 snapshot.
2. Preserve the 10 approved FA IDs and the explicitly added FC coverage work as **separate lineages**. Maintain exact mapping of work IDs to actual GitHub PR numbers/merged commits in a durable status file or approved Engine Room mapping. A GitHub PR number does not equal a roadmap ordinal.
3. Claim the next unowned ready unit with no conflicting PR. Work in small, tested PRs with fixture coverage, reviewed migrations, clear deployment effects, and user-visible status. Chain green, conflict-free PRs where authorized; do not pause just to request routine implementation decisions.
4. Never run two mutating ingestion jobs concurrently. Make read-only audit and backup verification precede any live recovery promotion. Never lower corpus counts to make production appear healthy.
5. After merge, validate actual scheduled run, B2 checkpoint, Render search and Engine Room status. Mark states separately: planned / in progress / PR merged / deployed / corpus-synchronized / validated / field-verified. If blocked by permission, API limit, source denial or storage/budget, record precise evidence and the minimal owner action. Continue independent tasks.
6. Do not begin big FOIA Bias Analysis or mass FOIA request operations merely because the UI works. Prioritize FC-14 broad-corpus coverage evidence first.
7. Refresh this document when user-approved product constraints change; never overwrite original FA 10 milestones silently. The completeness claim is measured by verified documents from varied primary/third-party sources, not by a declaration that software is done.

## 9. Release definition of done

**Operational archive ready:** 10 FA units evidenced; canonical single-writer recovery/scheduling verified; public metadata and document search reproducible; source-health, budget and restore visible and green.

**Broad-source archive ready:** FC-01..14 delivered or explicitly and accurately blocked, a versioned exhaustive source census, deep enumeration/backfill from as many legally accessible sources as possible, a large verified cross-agency/cross-period dataset, trustworthy confidence/coverage reporting, automated refresh, and a costed path beyond the 10 GB tier. A file cap or external blocked source must never be labeled fully covered.

**Handoff ready:** the repo holds this plan, current work-to-PR mapping, actual runtime/credential gates (without secrets), test/run instructions, active PR and failure queue, and a reproducible next-unblocked unit. At no point may a planning document alone set a project to 100% complete.
