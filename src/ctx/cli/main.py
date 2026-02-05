"""CLI entry point for ctx."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from ctx import db
from ctx.ingest import (
    GitHubIngester,
    LinearIngester,
    NotionIngester,
    ObsidianIngester,
    SlackIngester,
)
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

# Time constants (seconds)
SECONDS_PER_MINUTE = 60
SECONDS_PER_HOUR = 3600
SECONDS_PER_DAY = 86400
SECONDS_PER_WEEK = 604800
SECONDS_PER_MONTH = 2592000

# Minimum word length for query matching
MIN_QUERY_WORD_LENGTH = 2


def _parse_filters(filter_args: list[str] | None) -> dict[str, str]:
    """Parse --filter key=value args into dict."""
    if not filter_args:
        return {}
    result = {}
    for f in filter_args:
        if "=" not in f:
            raise typer.BadParameter(f"Filter must be key=value format: {f}")
        key, value = f.split("=", 1)
        result[key.strip()] = value.strip()
    return result


def _format_relative_time(ts: int | None) -> str:
    """Format as '2d ago', '3h ago', 'just now', or 'Jan 15' for older."""
    if ts is None:
        return ""
    now = datetime.now(tz=UTC)
    dt = datetime.fromtimestamp(ts, tz=UTC)
    seconds = int((now - dt).total_seconds())

    if seconds < 0 or seconds >= SECONDS_PER_MONTH:
        result = dt.strftime("%b %d")
    elif seconds < SECONDS_PER_MINUTE:
        result = "just now"
    elif seconds < SECONDS_PER_HOUR:
        result = f"{seconds // SECONDS_PER_MINUTE}m ago"
    elif seconds < SECONDS_PER_DAY:
        result = f"{seconds // SECONDS_PER_HOUR}h ago"
    elif seconds < SECONDS_PER_WEEK:
        result = f"{seconds // SECONDS_PER_DAY}d ago"
    else:
        result = f"{seconds // SECONDS_PER_WEEK}w ago"
    return result


def _truncate_content(content: str, query: str | None = None, context_chars: int = 100) -> str:
    """Truncate content, showing snippet around query match if found."""
    if not content:
        return ""

    # If query provided, try to find it and show context around it
    if query:
        content_lower = content.lower()
        query_lower = query.lower()

        # Try exact match first
        pos = content_lower.find(query_lower)

        # If no exact match, try individual words
        if pos == -1:
            words = query_lower.split()
            for word in words:
                if len(word) > MIN_QUERY_WORD_LENGTH:
                    pos = content_lower.find(word)
                    if pos != -1:
                        break

        if pos != -1:
            # Show context around the match
            start = max(0, pos - context_chars // 2)
            end = min(len(content), pos + len(query) + context_chars // 2)

            snippet = content[start:end]
            prefix = "..." if start > 0 else ""
            suffix = "..." if end < len(content) else ""
            return f"{prefix}{snippet}{suffix}"

    # No query or no match found - show first part
    if len(content) <= context_chars:
        return content
    return content[:context_chars] + "..."


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
    filter_: Annotated[
        list[str] | None,
        typer.Option("--filter", help="Filter by metadata field (key=value, can be repeated)"),
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
        typer.Option("--format", "-f", help="Output format: markdown, table, or json"),
    ] = "markdown",
) -> None:
    """Search across all indexed work context."""
    # Parse sources
    sources: list[Source] | None = None
    if source:
        sources = [Source(s) for s in source]

    # Parse since
    since_dt = parse_since(since)
    since_ts = int(since_dt.timestamp()) if since_dt else None

    # Parse extra filters
    extra_filters = _parse_filters(filter_)

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
            extra_filters=extra_filters or None,
        )

    if not results:
        console.print("[yellow]No results found.[/yellow]")
        raise typer.Exit

    # Output results
    if output_format == "json":
        console.print(json.dumps(results, indent=2))
    elif output_format in {"markdown", "md"}:
        _print_results_markdown(results, query=query)
    else:
        _print_results_table(results, query=query)


def _print_results_table(results: list[dict], *, query: str | None = None) -> None:
    """Print search results as a rich table."""
    table = Table(show_header=True, header_style="bold", expand=True)
    table.add_column("ID", style="dim", no_wrap=True)
    table.add_column("Source", style="cyan", no_wrap=True)
    table.add_column("When", style="dim", no_wrap=True)
    table.add_column("Content", ratio=3)
    table.add_column("Link", no_wrap=True)
    table.add_column("Dist", justify="right", no_wrap=True)

    for result in results:
        doc_id = result.get("id", "")
        metadata = result.get("metadata", {})
        source = metadata.get("source", "?")
        content = result.get("content", "")
        permalink = metadata.get("permalink", "")
        timestamp = metadata.get("timestamp")

        # Truncate content with query-aware context
        content = _truncate_content(content, query=query, context_chars=CONTENT_PREVIEW_LENGTH)

        when = _format_relative_time(timestamp)
        distance = result.get("distance")
        distance_str = f"{distance:.3f}" if distance is not None else "-"

        # Make permalink a clickable hyperlink
        link = f"[link={permalink}]Open[/link]" if permalink else "-"

        table.add_row(doc_id, source, when, content, link, distance_str)

    console.print(table)


def _print_results_markdown(results: list[dict], *, query: str | None = None) -> None:
    """Print search results in a token-efficient markdown format."""
    for i, result in enumerate(results):
        doc_id = result.get("id", "")
        metadata = result.get("metadata", {})
        content = result.get("content", "")
        permalink = metadata.get("permalink", "")
        timestamp = metadata.get("timestamp")
        source = metadata.get("source", "?")

        # Format relative time
        time_str = _format_relative_time(timestamp)

        # Print result with header
        if i > 0:
            console.print("---")

        # Header line: source + relative time
        header_parts = [f"[cyan]{source}[/cyan]"]
        if time_str:
            header_parts.append(f"[dim]{time_str}[/dim]")
        console.print(" ".join(header_parts))

        # Content (truncated with query context)
        truncated = _truncate_content(content, query=query, context_chars=200)
        console.print(truncated)

        if permalink:
            console.print(f"[dim]{permalink}[/dim]")
        if doc_id:
            console.print(f"[dim]ID: {doc_id}[/dim]")


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
    doc_ids: Annotated[list[str], typer.Argument(help="Document ID(s) to retrieve")],
    output_format: Annotated[
        str,
        typer.Option("--format", "-f", help="Output format: text or json"),
    ] = "text",
) -> None:
    """Get one or more documents by ID."""
    results = db.get_documents_by_ids(doc_ids)

    # Check for missing IDs
    found_ids = {r["id"] for r in results}
    missing_ids = [doc_id for doc_id in doc_ids if doc_id not in found_ids]
    for doc_id in missing_ids:
        console.print(f"[yellow]Warning: Document not found: {doc_id}[/yellow]")

    if not results:
        console.print("[red]No documents found.[/red]")
        raise typer.Exit(1)

    if output_format == "json":
        console.print(json.dumps(results, indent=2))
    else:
        for i, result in enumerate(results):
            if i > 0:
                console.print("\n" + "=" * 60 + "\n")
            console.print(f"[bold]ID:[/bold] {result['id']}")
            console.print(f"[bold]Source:[/bold] {result['metadata'].get('source', '?')}")
            console.print(f"\n[bold]Content:[/bold]\n{result['content']}")
            console.print("\n[bold]Metadata:[/bold]")
            for key, value in result["metadata"].items():
                console.print(f"  {key}: {value}")


@app.command("list")
def list_(
    source: Annotated[
        list[str] | None,
        typer.Option("--source", "-s", help="Filter by source (can be repeated)"),
    ] = None,
    since: Annotated[
        str | None,
        typer.Option(
            "--since", help="Only show documents from this time period (e.g., 7d, 24h, 2w)"
        ),
    ] = None,
    filter_: Annotated[
        list[str] | None,
        typer.Option("--filter", help="Filter by metadata field (key=value, can be repeated)"),
    ] = None,
    limit: Annotated[
        int,
        typer.Option("--limit", "-n", help="Maximum number of results"),
    ] = 10,
    output_format: Annotated[
        str,
        typer.Option("--format", "-f", help="Output format: markdown, table, or json"),
    ] = "markdown",
) -> None:
    """List documents with filters (no search query)."""
    # Parse sources
    sources: list[Source] | None = None
    if source:
        sources = [Source(s) for s in source]

    # Parse since
    since_dt = parse_since(since)
    since_ts = int(since_dt.timestamp()) if since_dt else None

    # Parse extra filters
    extra_filters = _parse_filters(filter_)

    # Execute list
    results = db.list_documents(
        n_results=limit,
        source=sources,
        since_timestamp=since_ts,
        extra_filters=extra_filters or None,
    )

    if not results:
        console.print("[yellow]No documents found.[/yellow]")
        raise typer.Exit

    # Output results
    if output_format == "json":
        console.print(json.dumps(results, indent=2))
    elif output_format in {"markdown", "md"}:
        _print_results_markdown(results)
    else:
        _print_results_table(results)


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

    console.print("[bold]Ingesting GitHub PRs...[/bold]")

    try:
        ingester = GitHubIngester()
        count = ingester.ingest(since=since_dt, full_reindex=full)
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

    console.print("[bold]Ingesting Linear issues...[/bold]")

    try:
        ingester = LinearIngester()
        count = ingester.ingest(since=since_dt, full_reindex=full)
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

    console.print("[bold]Ingesting Notion pages...[/bold]")

    try:
        ingester = NotionIngester()
        count = ingester.ingest(since=since_dt, full_reindex=full)
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
