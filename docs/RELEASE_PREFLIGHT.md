# FA-009 release evidence preflight

The operator CLI evaluates a local JSON file with the six evidence records required by
`foia_archive.release_readiness`. It performs no network requests, database changes,
notifications, deployments or production transitions.

Run `python scripts/release_preflight.py /path/to/reviewed-evidence.json`.
Exit 0 means structurally complete, fresh, source-consistent evidence is **ready for
review** only; exit 1 means explicit failed checks, and exit 2 means invalid or
unavailable evidence. No result is production certification or deployment approval.
The caller must independently authenticate the actual provider records and exact
revision before preparing this JSON; a supplied `passed` boolean is not provider
authentication. The file is bounded to 1 MiB and must contain no secrets or private
FOIA material. Production change controls and separate human/legal authorization
remain effective where required.
