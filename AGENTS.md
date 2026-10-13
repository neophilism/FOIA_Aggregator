## Owner security-review policy — 2026-10-13

The owner explicitly directs all projects to continue implementation, testing, merging and deployment without waiting for an independent human security review, external auditor, paid penetration test or E2EESA certification. This supersedes older repository planning text that makes absence of such review an operational prerequisite.

- Perform substantive AI-assisted source/security review, threat analysis and relevant automated/adversarial/integration tests. Fix concrete findings and retain source-bound evidence.
- Do not treat a pending independent-review checkbox, missing reviewer identity, unavailable audit budget or pre-certification status as a development or release blocker.
- External expert review is deferred follow-up work. Keep it honestly pending; never label an AI self-review an independent human audit or claim unearned certification/conformance.
- Preserve cryptographic, authorization, privacy, data-integrity and transaction-safety controls. Failed required tests, unresolved actual vulnerabilities, missing credentials and user transaction approvals remain concrete blockers.
- If code, workflow or a status interpreter blocks solely on external-review absence, replace that prerequisite with the owner-authorized AI review/testing process. Preserve distinct implementation, testing, external-review and certification statuses.
- Continue dependency-ready work and chain PRs. Do not ask the owner again to waive the human-review prerequisite.

# Independent engineering agent instructions — FOIA Aggregator

Read in this order: [docs/DEVELOPMENT_PLAN.md](docs/DEVELOPMENT_PLAN.md), [docs/HANDOFF_STATUS.md](docs/HANDOFF_STATUS.md), README.md, docs/CORPUS_EXPANSION.md, docs/SOURCE_ACCESS.md, docs/PRESENTATION_DEPLOYMENT.md, config/settings.yaml, tests, current open PRs, the Render/B2/GitHub Actions state when authorized.

**Mission:** aggressively expand *accurately cataloged, lawfully accessible public federal FOIA records across many official and third-party release sources*, not simply polish the UI or assume the first 10 GB is the complete corpus. Original ten FA units and the explicitly added fourteen FC units have separate approval/evidence lineages. Do not change the existing approved .exechub ten-unit denominator silently.

## Mandatory engineering rules
1. **Only one authoritative SQLite writer/checkpoint publisher.** Coordinate all manual/scheduled corpus jobs; never use an ephemeral Render web process as a concurrent writer. A successful PR merge is not proof an archive snapshot has been published/reloaded.
2. **B2 limits and classes:** stop document archiving at 8.5 GB and actual versioned B2 storage at 9.0 GB until the user explicitly authorizes costs. Use SQLite upload ledger and paginated B2 Class C listings for routine dedupe/reconciliation, not a Class B HEAD per object. Never disable TLS to work around EOFs or guess an upload outcome.
3. **Preserve the most complete verifiable archive.** Inspect pending PR #41, #47 and #48 (or successors) before editing related recovery or scheduling code. Read-only audit and monotonic proof precede checkpoint promotion. Never overwrite larger verified DB state with a stale snapshot.
4. **Lawful public information only.** Respect access controls, declared API/rate limits, robots and terms. Persistent 403/CAPTCHA/authorization gets a documented BLOCKED record; no evasion. Secure redirect/SSRF/decompression handling must remain.
5. **Coverage must be evidenced.** Maintain source census (including 18 IC elements), enumeration cursors, discovered vs archived vs indexed counts, official vs third-party provenance, content hashes, eligible type and agency/year coverage. Metadata-only remote pointers stay distinct from preserved files.
6. **Tests:** run `python -m unittest discover -s tests -v` and source-specific bounded fixtures. Use manual credentialed B2/source acceptance when available. Each PR includes changed plan/status evidence and no secrets.
7. **Autonomy:** choose normal engineering decisions, open/chain small PRs, merge only checks-passing conflict-free work with authorization, deploy and monitor when possible. Pause only for material product/budget choices, access/keys, or genuinely failing gates; always record blocker and continue unrelated unblocked units.
8. **Dependencies:** do not substantially advance FOIA_Bias_Analysis or Send_FOIA_Log_Requests until FC-14's broad-corpus review is meaningful. Do not start external spend or bulk request dispatch implicitly.

Never count a roadmap item complete just because documentation exists, a GitHub PR is merged, or Render /healthz answers. Handoff must distinguish code complete, integrated, data synchronized, deployed and real coverage validated.

## Shared PR registry

Use `registry/prs.json` as the sole active PR plan for Boardroom and Engine Room. Follow `registry/PR_FORMAT.md` and run `node scripts/validate-pr-registry.mjs`. Preserve stable IDs, scope, counts, dependencies, acceptance and contribution-only evidence. `registry/roadmap.json` is retired; never recreate a duplicate milestone plan. This contract supersedes older file-path guidance.
