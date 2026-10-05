# Presentation demo script

This script is based on the real presentation corpus seeded on October 5, 2026.

Current corpus snapshot:

- 129 agencies catalogued
- 635 offices/components
- 363 active official sources
- 65 records discovered
- 62 files archived
- 59 full-text searchable
- 6 searchable with OCR assistance

The goal of the demo is to show three distinct capabilities in under two minutes.

## Demo 1 — Cross-agency conceptual search

Search:

```text
financial stability
```

Why this is useful:

The phrase is found in body text across materially different records, including:

- Federal Deposit Insurance Corporation — procedures for crypto-related activities
- Federal Open Market Committee — released meeting-transcript material
- Federal Reserve System — released material concerning the September 2012 FOMC meeting

The phrase is not simply a shared filename or title. This demonstrates that the platform can search concepts across agency boundaries even when the user does not know which agency or reading room contains the relevant record.

Talking point:

> A normal agency-by-agency search requires you to know where to look first. Here the search starts with the subject and crosses the government archive for you.

## Demo 2 — Find a buried phrase inside a large release

Search:

```text
Medley Global Advisors
```

Expected result:

Federal Reserve System — *FOIA Response Regarding September 2012 Federal Open Market Committee Meeting*.

Why this is useful:

The result contains roughly half a million indexed characters and includes investigative material concerning the alleged release of confidential FOMC information. The search phrase does not appear in the document title.

Talking point:

> This is the difference between indexing a list of files and actually indexing the records. The title does not tell you this phrase is inside the release.

Open the record detail page and show:

- the extracted-text snippet
- original government source
- archived copy
- source provenance
- extraction method

## Demo 3 — OCR makes a scanned release searchable

Search:

```text
nominee filers
```

Expected result:

Office of Government Ethics — *OGE FOIA FY 26-103 Responsive records_final_Redacted_.pdf*.

Why this is useful:

This record is indexed through OCR rather than a usable native PDF text layer. The OCR text includes internal FOIA-processing guidance discussing nominee filers, searches, redactions, referrals, and review procedures.

Talking point:

> The original document was effectively an image for search purposes. The archive preserved the source PDF and created searchable OCR text without replacing or modifying the original record.

On the detail page, point out:

- **Indexed via OCR**
- the archived source file remains authoritative
- OCR text is explicitly treated as a search aid

## Optional contemporary search

Search:

```text
artificial intelligence
```

Current corpus matches include records from:

- National Aeronautics and Space Administration
- Office of Science and Technology Policy

This is a useful backup demonstration when the audience is more interested in technology policy than financial or administrative records.

## Optional historical search

Search:

```text
Zapruder
```

Expected result:

National Archives and Records Administration — *NARA Inspection and Duplication Film Report (Zapruder) March 28, 2002*.

This is a useful demonstration that the platform is not limited to recent policy material.

## Suggested 90-second presentation sequence

1. Open the homepage and briefly point out the live corpus statistics.
2. Search **financial stability** and show that different agencies appear in one result set.
3. Search **Medley Global Advisors** and open the Federal Reserve result to show body-text discovery and provenance.
4. Search **nominee filers**, open the OGE result, and point out that it is OCR-derived.
5. Open **About & coverage** if the audience asks how sources, preservation, or OCR work.

## Before presenting

Confirm on the deployed public instance:

- the live corpus statistics are non-zero
- all three primary searches return the expected records
- snippets appear for body-text matches
- record detail pages load
- archived B2 copies open through signed URLs
- original government-source links work
- the OGE record shows OCR extraction
- mobile layout is usable
- `/healthz` returns HTTP 200
