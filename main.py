import typer

from foia_archive.engine import run_once
from foia_archive.scheduler import run_forever
from foia_archive.utils import parse_bool

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


@app.command()
def daemon(config: str = "config/settings.yaml"):
    """Run continuous crawler."""
    run_forever(config_path=config)


if __name__ == "__main__":
    app()
