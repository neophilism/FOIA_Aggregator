# FA-002 checkpoint transport and recovery

This mapped follow-up resumes the native-upload source from PR #48 at
`1f3f950b3ab7203ec30e8d6d02730ffc909e7e93`, with additional integrity and
transport protections. The legacy PR remains open; reconcile its lineage after
this change merges rather than applying its older implementation over these
guards. The ten approved FA units and their scope are unchanged.

## Publication order

1. Create a consistent SQLite snapshot and validate it locally.
2. Compress it, calculate its full SHA-256 and enforce configured byte limits.
3. Choose a fresh UUID-bearing object key, even for identical same-second runs.
4. Upload through the existing bounded S3 path; reconcile the exact key before
   retrying ambiguous transport results. Authorization failures stop immediately.
   A separate checkpoint client permits only one SDK attempt; ordinary document
   and read-client behavior is unchanged. Checkpoints use a single upload request
   rather than hidden multipart retries, with an explicit 5 GiB request ceiling.
5. Only after three actual S3 TLS failures, use the native fallback. API origins
   must match the documented B2 cluster pattern; upload URLs must use a B2-owned
   host and the native upload path. TLS verification stays enabled and HTTP
   redirects are disabled. Validate bucket, key, length and SHA-1 receipt.
6. Read the uploaded object through a bounded `GetObject` stream, decompress to a
   separate file, compare the full original SHA-256 and run SQLite `quick_check`.
   A successful upload or an equal listing size alone is not successful backup
   verification. Failed verification leaves the source DB and old recovery points.
7. Only then prune old concrete versions. Protect the new checkpoint regardless
   of timestamps; stale/incomplete/cyclic listings defer pruning, never authorize
   partial deletion. Failed pruning does not discard the new verified checkpoint.

No per-object HEAD or high-level transfer-manager download is used for checkpoint
reads. One actual object read per new checkpoint is intentional integrity work,
not a zero-transaction claim. Existing document-ingestion caps and its ledger/
Class C reconciliation policy are unchanged.

## Restore limits and replacement

`database_backup.max_compressed_bytes` defaults to 268,435,456 bytes and
`max_restored_bytes` to 2,147,483,648 bytes. Both must be positive integers.
Enforce declared and actual stream lengths and the decompressed-byte ceiling.
The gzip checksum, full SHA-256 metadata when present, embedded key digest and
SQLite check must pass before atomic destination replacement. Legacy keys retain
their existing 12-hex digest fragment; this is not a claim of full-hash metadata
on old backups. Refuse replacement beside SQLite WAL/SHM sidecars. Never start a
second writer or treat automatic bootstrap as permission for archive promotion.

These are processing limits, not increases to the 8.5 GB document/9 GB actual-B2
storage targets. No new service, dependency, schedule or spending is introduced.

## Verification and remaining acceptance

The source contains fixture tests for receipt/endpoint validation, authorization,
mixed transport failures, read-back ordering, corrupted uploads, full-hash mismatch,
bounded gzip/streams, atomic failure preservation, sidecars, fresh keys and unsafe
pagination. Local syntax compilation is available; the local scaffold lacks
`boto3`/`requests`, so full functional results must come from actual committed-head
repository CI. Do not represent compilation as passing functional tests.

Live B2 upload/list/restore, candidate monotonicity, exclusive writer coordination,
recovery promotion, two sequential ingestion runs and deployed replica validation
remain separate acceptance gates. Existing PR #47 and PR #41 must not auto-activate
until those preconditions actually pass. Preserve the independent audit work in
PR #51 and rerun current-head checks after integrating sibling PRs.

Provider references checked October 17, 2026:

- https://www.backblaze.com/apidocs/b2-get-upload-url
- https://www.backblaze.com/apidocs/b2-upload-file
- https://www.backblaze.com/apidocs/introduction-to-the-b2-native-api
- https://docs.aws.amazon.com/boto3/latest/reference/services/s3/client/get_object.html
- https://botocore.amazonaws.com/v1/documentation/api/latest/reference/config.html
