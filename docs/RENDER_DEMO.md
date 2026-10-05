# Free public demo on Render

The presentation web service can run on Render's Free web-service plan without a persistent disk.

This works because the authoritative document binaries and the verified SQLite presentation snapshot are already stored in Backblaze B2. Whenever Render starts the container with an empty ephemeral filesystem, the container restores the newest verified SQLite snapshot from B2 before FastAPI starts.

## Why the Free plan works for the demo

The web service is read-mostly during a presentation:

- B2 stores archived document binaries.
- B2 stores verified SQLite database snapshots.
- the web container restores SQLite on startup.
- searches run locally against the restored SQLite/FTS5 index.
- archived-file downloads are redirected to short-lived signed B2 URLs.
- the crawler does **not** need to run inside the public demo service.

A persistent Render disk is therefore unnecessary for the presentation deployment.

## Deploy

The repository root contains `render.yaml`.

1. Sign in to Render and connect the GitHub repository.
2. Create a new **Blueprint** from this repository.
3. Render reads `render.yaml` and creates the `foia-aggregator` Free web service.
4. When prompted for secret environment variables, supply:
   - `B2_KEY_ID`
   - `B2_APPLICATION_KEY`
5. Deploy.

The bucket name, B2 region, and S3 endpoint do not need to be entered when the application key is scoped to exactly one bucket. The application resolves them from Backblaze authorization during first boot.

Render provides an `onrender.com` HTTPS URL automatically.

## Expected first boot

The startup sequence is:

1. create the ephemeral `/data` directories;
2. see that `/data/foia_archive.db` is absent;
3. authorize the bucket-scoped B2 key;
4. discover the allowed bucket and S3 endpoint;
5. download the newest `database-backups/*.sqlite.gz` snapshot;
6. verify its stored SHA-256;
7. run SQLite `quick_check`;
8. place the verified DB at `/data/foia_archive.db`;
9. start FastAPI;
10. pass Render's `/healthz` check.

The current presentation snapshot contains at least:

- 65 discovered records
- 62 archived files
- 59 full-text searchable records
- 6 OCR-assisted searchable records

## Free-service cold starts

Render Free web services spin down after inactivity. Because the filesystem is ephemeral, the next cold start restores the SQLite snapshot from B2 again.

Before a live presentation, open the public URL a minute or two early and wait for the homepage to load. Once awake, the demo is served from the local restored SQLite database rather than querying B2 for every search.

## Presentation check

Use the real-corpus script in `docs/DEMO_SCRIPT.md`.

Primary searches:

1. `financial stability`
2. `Medley Global Advisors`
3. `nominee filers`

Also confirm:

- `/healthz` returns HTTP 200;
- the OGE result identifies OCR-derived indexing;
- archived-copy links redirect through signed B2 URLs;
- source-provenance links remain visible.

## Crawler strategy

Do not run the continuous crawler on the Free presentation service.

Continue expanding the corpus with the repository's **Seed presentation corpus** GitHub Actions workflow. Each successful seed run archives binaries to B2 and writes a fresh verified SQLite snapshot. Redeploying or cold-starting the web service will then restore the latest snapshot.

## After the demonstration

The Free service is appropriate for a preview/demo, not a production SLA. For a continuously warm public service, upgrade the compute plan or move the same Docker image to another container host. The archive/storage architecture does not need to change.
