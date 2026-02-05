"""CLI entry point for ctx."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, NamedTuple

import typer
from rich.console import Console

from ctx.ingest import (
    GitHubIngester,
    LinearIngester,
    NotionIngester,
    ObsidianIngester,
    SlackIngester,
)
from ctx.ingest.base import BaseIngester, parse_since
from ctx.models import Source
from ctx.sync_state import get_all_sync_state, get_last_synced_at, set_last_synced_at

app = typer.Typer(
    name="ctx",
    help="Aggregate work context from multiple sources into markdown files.",
    no_args_is_help=True,
)

console = Console()

# Time constants (seconds)
SECONDS_PER_MINUTE = 60
SECONDS_PER_HOUR = 3600
SECONDS_PER_DAY = 86400


@app.command()
def info() -> None:
    """Show sync status."""
    sync_state = get_all_sync_state()

    console.print("\n[bold]ctx Sync Status[/bold]\n")
    for source in Source:
        ts = sync_state.get(source.value)
        if ts:
            seconds = int((datetime.now(tz=UTC) - ts).total_seconds())
            if seconds < SECONDS_PER_HOUR:
                ago = f"{seconds // SECONDS_PER_MINUTE}m ago"
            elif seconds < SECONDS_PER_DAY:
                ago = f"{seconds // SECONDS_PER_HOUR}h ago"
            else:
                ago = f"{seconds // SECONDS_PER_DAY}d ago"
            console.print(f"  {source.value}: [cyan]{ago}[/cyan]")
        else:
            console.print(f"  {source.value}: [dim]never[/dim]")

    console.print()


ingest_app = typer.Typer(help="Ingest data from sources.")
app.add_typer(ingest_app, name="ingest")


def _run_ingest(
    ingester: BaseIngester,
    since: str | None,
    full: bool,
) -> None:
    """Shared ingestion logic for all sources."""
    source = ingester.source
    since_dt = parse_since(since)
    if since_dt is None and not full:
        since_dt = get_last_synced_at(source)
        if since_dt:
            console.print(f"[dim]Resuming from last sync: {since_dt:%Y-%m-%d %H:%M}[/dim]")

    capture_time = datetime.now(tz=UTC)

    try:
        count = ingester.ingest(since=since_dt, full_reindex=full)
        set_last_synced_at(source, capture_time)
        console.print(f"[green]Ingested {count} documents from {source.value}.[/green]")
    except ValueError as e:
        console.print(f"[red]Configuration error: {e}[/red]")
        raise typer.Exit(1) from None
    except Exception as e:
        console.print(f"[red]Ingestion failed: {e}[/red]")
        raise typer.Exit(1) from None


class _IngesterEntry(NamedTuple):
    name: str
    help: str
    cls: type[BaseIngester]


_INGESTERS = [
    _IngesterEntry("slack", "Ingest Slack threads you've participated in.", SlackIngester),
    _IngesterEntry("github", "Ingest GitHub PRs you've authored or reviewed.", GitHubIngester),
    _IngesterEntry("linear", "Ingest Linear issues you're involved with.", LinearIngester),
    _IngesterEntry("notion", "Ingest Notion pages the integration has access to.", NotionIngester),
    _IngesterEntry("obsidian", "Ingest markdown notes from an Obsidian vault.", ObsidianIngester),
]


def _make_ingest_command(
    ingester_cls: type[BaseIngester],
) -> None:
    def command(
        since: Annotated[
            str | None,
            typer.Option("--since", help="Only ingest items updated since (e.g., 7d, 24h, 2w)"),
        ] = None,
        full: Annotated[
            bool,
            typer.Option("--full", help="Full re-index (clear existing documents first)"),
        ] = False,
    ) -> None:
        _run_ingest(ingester_cls(), since, full)

    return command  # type: ignore[return-value]


for _entry in _INGESTERS:
    ingest_app.command(_entry.name, help=_entry.help)(_make_ingest_command(_entry.cls))


if __name__ == "__main__":
    app()
