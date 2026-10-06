import typer

from foia_archive.archive_storage import B2ArchiveStorage, get_archive_storage
from foia_archive.database_backup import (
    backup_database_to_b2,
    bootstrap_database_from_b2,
    restore_database_from_b2,
)
from foia_archive.engine import run_once
from foia_archive.scheduler import run_forever
from foia_archive.storage import get_connection
from foia_archive.text_index import reindex_downloaded_documents
from foia_archive.utils import load_config, parse_bool

app = typer.Typer(help="FOIA Archive CLI")


@app.command()
def run(
    config: str = "config/settings.yaml",
    dry_run: str | None = typer.Option(
        None,
        "--dry-run",
        help="Set to true/false to override the dry-run flag in config (defaults to config value).",
        metavar="[true|false]",
    ),
    max_docs_per_source: int = 10,
):
    """Run a single crawl cycle."""
    try:
        dry_run_flag = parse_bool(dry_run)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    run_once(
        config_path=config,
        dry_run=dry_run_flag,
        max_docs_per_source=max_docs_per_source,
    )


@app.command("backup-db")
def backup_db(
    config: str = "config/settings.yaml",
    force: bool = typer.Option(
        False,
        "--force",
        help="Create a backup even when the normal backup interval has not elapsed.",
    ),
):
    """Create a consistent compressed SQLite backup in configured B2 storage."""
    cfg = load_config(config)
    result = backup_database_to_b2(cfg, force=force)
    if result is None:
        typer.echo(
            "No backup created (B2 storage is not selected, backups are disabled, "
            "or the configured interval has not elapsed)."
        )
        return
    typer.echo(
        f"Uploaded {result.key} ({result.compressed_bytes} bytes)"
    )


@app.command("bootstrap-db")
def bootstrap_db(
    config: str = "config/settings.yaml",
    destination: str | None = typer.Option(
        None,
        "--destination",
        help="Live database destination. Defaults to storage.db_path.",
    ),
):
    """Restore the newest verified B2 snapshot only when the live DB is absent."""
    cfg = load_config(config)
    restored = bootstrap_database_from_b2(
        cfg,
        destination_path=destination,
    )
    if restored is None:
        typer.echo(
            "Database bootstrap skipped: a live database already exists "
            "or no B2 backup is available."
        )
        return
    typer.echo(f"Bootstrapped verified database to {restored}")


@app.command("restore-db")
def restore_db(
    config: str = "config/settings.yaml",
    key: str | None = typer.Option(
        None,
        "--key",
        help="Specific B2 backup object key. Defaults to the newest backup.",
    ),
    destination: str | None = typer.Option(
        None,
        "--destination",
        help="Recovery database path. Defaults beside the live DB as *.restored.db.",
    ),
    overwrite: bool = typer.Option(
        False,
        "--overwrite",
        help="Allow replacing an existing recovery destination.",
    ),
):
    """Restore and verify a B2 backup into a separate recovery database."""
    cfg = load_config(config)
    restored = restore_database_from_b2(
        cfg,
        key=key,
        destination_path=destination,
        overwrite=overwrite,
    )
    typer.echo(f"Restored verified database to {restored}")




@app.command("reconcile-b2")
def reconcile_b2(
    config: str = "config/settings.yaml",
):
    """Compare SQLite archive state with the current B2 object manifest."""
    cfg = load_config(config)
    backend = get_archive_storage(cfg)
    if not isinstance(backend, B2ArchiveStorage):
        raise typer.BadParameter("reconcile-b2 requires B2 archive storage")

    manifest = backend.current_object_manifest(prefix="documents/")
    conn = get_connection(cfg.storage.get("db_path"))
    try:
        rows = conn.execute(
            """
            SELECT storage_key, MAX(COALESCE(file_size, 0)) AS file_size
            FROM documents
            WHERE storage_backend = 'b2'
              AND download_status = 'downloaded'
              AND storage_key IS NOT NULL
              AND storage_key != ''
            GROUP BY storage_key
            """
        ).fetchall()
    finally:
        conn.close()

    database = {
        str(row["storage_key"]): int(row["file_size"] or 0)
        for row in rows
    }
    missing = sorted(set(database) - set(manifest))
    unexpected = sorted(set(manifest) - set(database))
    size_mismatches = sorted(
        key
        for key in set(database) & set(manifest)
        if database[key] > 0 and manifest[key] != database[key]
    )

    typer.echo(
        "B2 reconciliation: "
        f"database={len(database)}, "
        f"manifest={len(manifest)}, "
        f"missing={len(missing)}, "
        f"unexpected={len(unexpected)}, "
        f"size_mismatches={len(size_mismatches)}"
    )
    for label, keys in (
        ("missing", missing),
        ("unexpected", unexpected),
        ("size mismatch", size_mismatches),
    ):
        for key in keys[:25]:
            typer.echo(f"{label}: {key}")
        if len(keys) > 25:
            typer.echo(f"{label}: ... and {len(keys) - 25} more")

    if missing or size_mismatches:
        raise typer.Exit(code=1)


@app.command("reindex-text")
def reindex_text(
    config: str = "config/settings.yaml",
    limit: int | None = typer.Option(
        None,
        "--limit",
        min=1,
        help="Maximum number of archived documents to process.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Re-extract documents that already have extraction state.",
    ),
):
    """Backfill full-text search from already archived documents."""
    cfg = load_config(config)
    summary = reindex_downloaded_documents(
        cfg,
        limit=limit,
        force=force,
    )
    typer.echo(
        "Text reindex complete: "
        f"attempted={summary.attempted}, "
        f"indexed={summary.indexed}, "
        f"empty={summary.empty}, "
        f"unsupported={summary.unsupported}, "
        f"failed={summary.failed}"
    )


@app.command()
def daemon(config: str = "config/settings.yaml"):
    """Run continuous crawler."""
    run_forever(config_path=config)


if __name__ == "__main__":
    app()
