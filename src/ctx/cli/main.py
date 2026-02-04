"""CLI entry point for ctx."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

from ctx import db
from ctx.ingest import ObsidianIngester, SlackIngester
from ctx.ingest.base import parse_since
from ctx.models import Source

app = typer.Typer(
    name="ctx",
    help="Aggregate and search work context from multiple sources.",
    no_args_is_help=True,
)

console = Console()

# Display constants
CONTENT_PREVIEW_LENGTH = 100


@app.command()
def search(
    query: Annotated[str, typer.Argument(help="Search query")],
    source: Annotated[
        list[str] | None,
        typer.Option("--source", "-s", help="Filter by source (can be repeated)"),
    ] = None,
    since: Annotated[
        str | None,
        typer.Option("--since", help="Only show results from this time period (e.g., 7d, 24h, 2w)"),
    ] = None,
    involvement: Annotated[
        str | None,
        typer.Option("--involvement", "-i", help="Filter by involvement type"),
    ] = None,
    keyword: Annotated[
        bool,
        typer.Option("--keyword", "-k", help="Use keyword search instead of semantic search"),
    ] = False,
    limit: Annotated[
        int,
        typer.Option("--limit", "-n", help="Maximum number of results"),
    ] = 10,
    output_format: Annotated[
        str,
        typer.Option("--format", "-f", help="Output format: table or json"),
    ] = "table",
) -> None:
    """Search across all indexed work context."""
    # Parse sources
    sources: list[Source] | None = None
    if source:
        sources = [Source(s) for s in source]

    # Parse since
    since_dt = parse_since(since)
    since_ts = int(since_dt.timestamp()) if since_dt else None

    # Execute search
    if keyword:
        results = db.keyword_search(
            query,
            n_results=limit,
            source=sources,
            since_timestamp=since_ts,
        )
    else:
        results = db.search(
            query,
            n_results=limit,
            source=sources,
            since_timestamp=since_ts,
            involvement=involvement,
        )

    if not results:
        console.print("[yellow]No results found.[/yellow]")
        raise typer.Exit

    # Output results
    if output_format == "json":
        console.print(json.dumps(results, indent=2))
    else:
        _print_results_table(results)


def _print_results_table(results: list[dict]) -> None:
    """Print search results as a rich table."""
    table = Table(show_header=True, header_style="bold", expand=True)
    table.add_column("Source", style="cyan", no_wrap=True)
    table.add_column("Content", ratio=3)
    table.add_column("Link", no_wrap=True)
    table.add_column("Dist", justify="right", no_wrap=True)

    for result in results:
        metadata = result.get("metadata", {})
        source = metadata.get("source", "?")
        content = result.get("content", "")
        permalink = metadata.get("permalink", "")
        # Truncate content for display
        if content and len(content) > CONTENT_PREVIEW_LENGTH:
            content = content[:CONTENT_PREVIEW_LENGTH] + "..."
        distance = result.get("distance")
        distance_str = f"{distance:.3f}" if distance is not None else "-"

        # Make permalink a clickable hyperlink
        link = f"[link={permalink}]Open[/link]" if permalink else "-"

        table.add_row(source, content, link, distance_str)

    console.print(table)


@app.command()
def info() -> None:
    """Show database statistics."""
    stats = db.get_stats()

    console.print("\n[bold]ctx Database Statistics[/bold]\n")
    console.print(f"  Total documents: [cyan]{stats['total']}[/cyan]")
    console.print("\n  By source:")

    for source, count in stats["by_source"].items():
        if count > 0:
            console.print(f"    {source}: [cyan]{count}[/cyan]")
        else:
            console.print(f"    {source}: [dim]{count}[/dim]")

    console.print()


@app.command()
def get(
    doc_id: Annotated[str, typer.Argument(help="Document ID to retrieve")],
    output_format: Annotated[
        str,
        typer.Option("--format", "-f", help="Output format: text or json"),
    ] = "text",
) -> None:
    """Get a specific document by ID."""
    result = db.get_document_by_id(doc_id)

    if not result:
        console.print(f"[red]Document not found: {doc_id}[/red]")
        raise typer.Exit(1)

    if output_format == "json":
        console.print(json.dumps(result, indent=2))
    else:
        console.print(f"\n[bold]ID:[/bold] {result['id']}")
        console.print(f"[bold]Source:[/bold] {result['metadata'].get('source', '?')}")
        console.print(f"\n[bold]Content:[/bold]\n{result['content']}")
        console.print("\n[bold]Metadata:[/bold]")
        for key, value in result["metadata"].items():
            console.print(f"  {key}: {value}")


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
    # Load environment variables from .env
    load_dotenv(Path.cwd() / ".env")

    since_dt = parse_since(since)

    console.print("[bold]Ingesting Slack...[/bold]")

    try:
        ingester = SlackIngester()
        count = ingester.ingest(since=since_dt, full_reindex=full)
        console.print(f"[green]Ingested {count} documents from Slack.[/green]")
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
    # Load environment variables from .env
    load_dotenv(Path.cwd() / ".env")

    since_dt = parse_since(since)

    console.print("[bold]Ingesting Obsidian vault...[/bold]")

    try:
        ingester = ObsidianIngester(vault_path=vault_path)
        count = ingester.ingest(since=since_dt, full_reindex=full)
        console.print(f"[green]Ingested {count} documents from Obsidian.[/green]")
    except ValueError as e:
        console.print(f"[red]Configuration error: {e}[/red]")
        raise typer.Exit(1) from None
    except Exception as e:
        console.print(f"[red]Ingestion failed: {e}[/red]")
        raise typer.Exit(1) from None


if __name__ == "__main__":
    app()
