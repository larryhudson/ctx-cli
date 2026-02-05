"""CLI entry point for ctx."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from ctx.ingest import (
    GitHubIngester,
    LinearIngester,
    NotionIngester,
    ObsidianIngester,
    SlackIngester,
)
from ctx.ingest.base import parse_since
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


@ingest_app.command("slack")
def ingest_slack(
    since: Annotated[
        str | None,
        typer.Option("--since", help="Only ingest items from this time period (e.g., 7d, 24h, 2w)"),
    ] = None,
    full: Annotated[
        bool,
        typer.Option("--full", help="Full re-index (clear existing documents first)"),
    ] = False,
) -> None:
    """Ingest Slack threads you've participated in."""
    since_dt = parse_since(since)
    if since_dt is None and not full:
        since_dt = get_last_synced_at(Source.SLACK)
        if since_dt:
            console.print(f"[dim]Resuming from last sync: {since_dt:%Y-%m-%d %H:%M}[/dim]")

    capture_time = datetime.now(tz=UTC)
    console.print("[bold]Ingesting Slack...[/bold]")

    try:
        ingester = SlackIngester()
        count = ingester.ingest(since=since_dt, full_reindex=full)
        set_last_synced_at(Source.SLACK, capture_time)
        console.print(f"[green]Ingested {count} documents from Slack.[/green]")
    except ValueError as e:
        console.print(f"[red]Configuration error: {e}[/red]")
        raise typer.Exit(1) from None
    except Exception as e:
        console.print(f"[red]Ingestion failed: {e}[/red]")
        raise typer.Exit(1) from None


@ingest_app.command("github")
def ingest_github(
    since: Annotated[
        str | None,
        typer.Option("--since", help="Only ingest PRs updated since (e.g., 7d, 24h, 2w)"),
    ] = None,
    full: Annotated[
        bool,
        typer.Option("--full", help="Full re-index (clear existing documents first)"),
    ] = False,
) -> None:
    """Ingest GitHub PRs you've authored or reviewed."""
    since_dt = parse_since(since)
    if since_dt is None and not full:
        since_dt = get_last_synced_at(Source.GITHUB)
        if since_dt:
            console.print(f"[dim]Resuming from last sync: {since_dt:%Y-%m-%d %H:%M}[/dim]")

    capture_time = datetime.now(tz=UTC)
    console.print("[bold]Ingesting GitHub PRs...[/bold]")

    try:
        ingester = GitHubIngester()
        count = ingester.ingest(since=since_dt, full_reindex=full)
        set_last_synced_at(Source.GITHUB, capture_time)
        console.print(f"[green]Ingested {count} documents from GitHub.[/green]")
    except ValueError as e:
        console.print(f"[red]Configuration error: {e}[/red]")
        raise typer.Exit(1) from None
    except Exception as e:
        console.print(f"[red]Ingestion failed: {e}[/red]")
        raise typer.Exit(1) from None


@ingest_app.command("linear")
def ingest_linear(
    since: Annotated[
        str | None,
        typer.Option("--since", help="Only ingest issues updated since (e.g., 7d, 24h, 2w)"),
    ] = None,
    full: Annotated[
        bool,
        typer.Option("--full", help="Full re-index (clear existing documents first)"),
    ] = False,
) -> None:
    """Ingest Linear issues you're involved with."""
    since_dt = parse_since(since)
    if since_dt is None and not full:
        since_dt = get_last_synced_at(Source.LINEAR)
        if since_dt:
            console.print(f"[dim]Resuming from last sync: {since_dt:%Y-%m-%d %H:%M}[/dim]")

    capture_time = datetime.now(tz=UTC)
    console.print("[bold]Ingesting Linear issues...[/bold]")

    try:
        ingester = LinearIngester()
        count = ingester.ingest(since=since_dt, full_reindex=full)
        set_last_synced_at(Source.LINEAR, capture_time)
        console.print(f"[green]Ingested {count} documents from Linear.[/green]")
    except ValueError as e:
        console.print(f"[red]Configuration error: {e}[/red]")
        raise typer.Exit(1) from None
    except Exception as e:
        console.print(f"[red]Ingestion failed: {e}[/red]")
        raise typer.Exit(1) from None


@ingest_app.command("notion")
def ingest_notion(
    since: Annotated[
        str | None,
        typer.Option("--since", help="Only ingest pages updated since (e.g., 7d, 24h, 2w)"),
    ] = None,
    full: Annotated[
        bool,
        typer.Option("--full", help="Full re-index (clear existing documents first)"),
    ] = False,
) -> None:
    """Ingest Notion pages the integration has access to."""
    since_dt = parse_since(since)
    if since_dt is None and not full:
        since_dt = get_last_synced_at(Source.NOTION)
        if since_dt:
            console.print(f"[dim]Resuming from last sync: {since_dt:%Y-%m-%d %H:%M}[/dim]")

    capture_time = datetime.now(tz=UTC)
    console.print("[bold]Ingesting Notion pages...[/bold]")

    try:
        ingester = NotionIngester()
        count = ingester.ingest(since=since_dt, full_reindex=full)
        set_last_synced_at(Source.NOTION, capture_time)
        console.print(f"[green]Ingested {count} documents from Notion.[/green]")
    except ValueError as e:
        console.print(f"[red]Configuration error: {e}[/red]")
        raise typer.Exit(1) from None
    except Exception as e:
        console.print(f"[red]Ingestion failed: {e}[/red]")
        raise typer.Exit(1) from None


@ingest_app.command("obsidian")
def ingest_obsidian(
    vault_path: Annotated[
        Path | None,
        typer.Option("--vault", "-v", help="Path to Obsidian vault (overrides config)"),
    ] = None,
    since: Annotated[
        str | None,
        typer.Option("--since", help="Only ingest files modified since (e.g., 7d, 24h, 2w)"),
    ] = None,
    full: Annotated[
        bool,
        typer.Option("--full", help="Full re-index (clear existing documents first)"),
    ] = False,
) -> None:
    """Ingest markdown notes from an Obsidian vault."""
    since_dt = parse_since(since)
    if since_dt is None and not full:
        since_dt = get_last_synced_at(Source.OBSIDIAN)
        if since_dt:
            console.print(f"[dim]Resuming from last sync: {since_dt:%Y-%m-%d %H:%M}[/dim]")

    capture_time = datetime.now(tz=UTC)
    console.print("[bold]Ingesting Obsidian vault...[/bold]")

    try:
        ingester = ObsidianIngester(vault_path=vault_path)
        count = ingester.ingest(since=since_dt, full_reindex=full)
        set_last_synced_at(Source.OBSIDIAN, capture_time)
        console.print(f"[green]Ingested {count} documents from Obsidian.[/green]")
    except ValueError as e:
        console.print(f"[red]Configuration error: {e}[/red]")
        raise typer.Exit(1) from None
    except Exception as e:
        console.print(f"[red]Ingestion failed: {e}[/red]")
        raise typer.Exit(1) from None


if __name__ == "__main__":
    app()
