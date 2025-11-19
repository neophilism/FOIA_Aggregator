import typer

from foia_archive.engine import run_once
from foia_archive.scheduler import run_forever

app = typer.Typer(help="FOIA Archive CLI")


@app.command()
def run(
    config: str = "config/settings.yaml",
    dry_run: bool = True,
    max_docs_per_source: int = 10,
):
    """Run a single crawl cycle."""
    run_once(
        config_path=config,
        dry_run=dry_run,
        max_docs_per_source=max_docs_per_source,
    )


@app.command()
def daemon(config: str = "config/settings.yaml"):
    """Run continuous crawler."""
    run_forever(config_path=config)


if __name__ == "__main__":
    app()
