# Legacy Microsoft Office text extraction

FOIA reading rooms still contain older binary Microsoft Office releases that
predate the XML-based DOCX/XLSX/PPTX formats. The archive preserves those
original files and can now extract bounded search text from:

- `.doc` using `antiword`
- `.xls` using `xls2csv`
- `.ppt` using `catppt`

The `xls2csv` and `catppt` utilities are provided by the Debian/Ubuntu
`catdoc` package.

## Safety model

Legacy Office files are treated as untrusted inputs.

Extraction:
- invokes the helper directly without `shell=True`;
- has a hard configurable subprocess timeout;
- captures stderr only up to a small diagnostic limit;
- writes stdout into a temporary file instead of buffering unbounded output;
- reads only a bounded output window;
- applies the same maximum indexed-character ceiling as every other extractor.

An extraction failure does not prevent the original released file from being
archived. The authoritative artifact remains the original binary; extracted
text is only a search aid.

## Configuration

```yaml
search:
  max_indexed_chars_per_document: 5000000
  legacy_office_timeout_seconds: 30
```

The timeout is additionally clamped by the extractor to a reasonable range.

## Existing archived files

After deploying support, run:

```bash
python main.py reindex-text
```

The normal non-force reindex path automatically revisits legacy DOC/XLS/PPT
records that were previously marked unsupported. It does not require another
crawl of the agency website.

## Runtime dependencies

Debian/Ubuntu:

```bash
sudo apt-get install antiword catdoc
```

The production Docker image and GitHub ingestion/test workflows install these
dependencies automatically. CI also verifies that all three command-line
extractors are present before running the test suite.

## Extraction metadata

Successful legacy extraction is recorded as:

```text
extraction_method = legacy_office
```

Record detail pages label this as **legacy Office text extraction** so users can
distinguish extracted search text from the original binary release.
