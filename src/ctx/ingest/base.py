"""Base ingester class."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import UTC, datetime, timedelta

from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeRemainingColumn,
)

from ctx.models import Document, DocumentMetadata, Source, make_document_id
from ctx.summarize import summarize_documents
from ctx.writer import delete_by_source, write_documents, write_index_files

logger = logging.getLogger(__name__)
console = Console()


class BaseIngester(ABC):
    """Base class for all data source ingesters.

    Subclasses must implement:
    - source: The Source enum value for this ingester
    - fetch_items(): Fetch items from the data source
    - item_to_documents(): Convert a single item to Document(s)
    """

    @property
    @abstractmethod
    def source(self) -> Source:
        """The source type this ingester handles."""
        ...

    @abstractmethod
    def fetch_items(
        self,
        since: datetime | None = None,
    ) -> list[dict]:
        """Fetch items from the data source.

        Args:
            since: Only fetch items updated after this time.
                   If None, fetch all items.

        Returns:
            List of raw items from the source API.
        """
        ...

    def item_to_documents(self, item: dict) -> list[Document]:
        """Convert a single item to one or more Documents.

        This should handle chunking if the item content is large.

        Args:
            item: A raw item from fetch_items().

        Returns:
            List of Document objects (may be multiple if chunked).
        """
        raise NotImplementedError

    def prepare_items(self, items: list[dict]) -> None:  # noqa: B027
        """Hook called after fetch_items(), before processing.

        Override to prefetch data (e.g. batch user/channel lookups).
        """

    def log_ingest_start(self) -> None:
        """Hook called at the start of ingest(). Override for extra startup output."""
        console.print(f"[bold]Ingesting {self.source.value}...[/bold]")

    def ingest(
        self,
        since: datetime | None = None,
        full_reindex: bool = False,
    ) -> int:
        """Run the ingestion process.

        Args:
            since: Only ingest items updated after this time.
            full_reindex: If True, delete all existing documents from this
                          source before ingesting.

        Returns:
            Number of documents ingested.
        """
        self.log_ingest_start()

        if full_reindex:
            console.print(
                f"[yellow]Full reindex: deleting existing {self.source.value} documents...[/yellow]"
            )
            delete_by_source(self.source)

        items = self.fetch_items(since=since)

        if not items:
            console.print("[yellow]No items found to ingest[/yellow]")
            return 0

        self.prepare_items(items)

        all_documents: list[Document] = []
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TimeRemainingColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("Processing items", total=len(items))
            for item in items:
                try:
                    documents = self.item_to_documents(item)
                    all_documents.extend(documents)
                except Exception:
                    logger.exception("Failed to convert item to document")
                finally:
                    progress.advance(task)

        if not all_documents:
            console.print("[yellow]No documents created[/yellow]")
            return 0

        console.print(f"[blue]Summarizing {len(all_documents)} documents...[/blue]")
        summarize_documents(all_documents)
        console.print(f"[blue]Writing {len(all_documents)} documents...[/blue]")
        count = write_documents(all_documents)
        write_index_files(all_documents)
        console.print(f"[green]Successfully ingested {count} documents[/green]")

        return count

    def create_documents_from_content(
        self,
        source_id: str,
        content: str,
        metadata: DocumentMetadata,
    ) -> list[Document]:
        """Helper to create a Document from content.

        Args:
            source_id: The source-specific ID for this item.
            content: The text content.
            metadata: The metadata for this item.

        Returns:
            List containing a single Document.
        """
        doc_id = make_document_id(self.source, source_id)
        return [
            Document(
                id=doc_id,
                content=content,
                metadata=metadata,
            )
        ]


def parse_since(since_str: str | None) -> datetime | None:
    """Parse a 'since' string like '7d' or '24h' into a datetime.

    Args:
        since_str: String like '7d' (7 days), '24h' (24 hours), '2w' (2 weeks).
                   If None, returns None.

    Returns:
        datetime or None.
    """
    if since_str is None:
        return None

    since_str = since_str.strip().lower()

    if since_str.endswith("d"):
        days = int(since_str[:-1])
        return datetime.now(tz=UTC) - timedelta(days=days)
    if since_str.endswith("h"):
        hours = int(since_str[:-1])
        return datetime.now(tz=UTC) - timedelta(hours=hours)
    if since_str.endswith("w"):
        weeks = int(since_str[:-1])
        return datetime.now(tz=UTC) - timedelta(weeks=weeks)

    msg = f"Invalid since format: {since_str}. Use '7d', '24h', or '2w'."
    raise ValueError(msg)
