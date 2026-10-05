import typer

from foia_archive.database_backup import (
    backup_database_to_b2,
    restore_database_from_b2,
)
from foia_archive.engine import run_once
from foia_archive.scheduler import run_forever
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


@app.command()
def daemon(config: str = "config/settings.yaml"):
    """Run continuous crawler."""
    run_forever(config_path=config)


if __name__ == "__main__":
    app()
