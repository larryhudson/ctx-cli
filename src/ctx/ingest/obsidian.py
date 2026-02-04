"""Obsidian vault ingester."""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from pathlib import Path

import yaml
from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeRemainingColumn,
)

from ctx.config import get_config
from ctx.db import add_documents, delete_by_source
from ctx.ingest.base import BaseIngester
from ctx.models import ContentType, Document, DocumentMetadata, Involvement, Source

logger = logging.getLogger(__name__)
console = Console()

# Frontmatter regex: matches --- at start, captures content, ends with ---
FRONTMATTER_PATTERN = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)

# Inline tag pattern: #tag (but not #123 which is a number)
INLINE_TAG_PATTERN = re.compile(r"(?<!\S)#([a-zA-Z][a-zA-Z0-9_/-]*)")


class ObsidianIngester(BaseIngester):
    """Ingester for Obsidian markdown notes."""

    def __init__(self, vault_path: Path | None = None) -> None:
        """Initialize the Obsidian ingester.

        Args:
            vault_path: Path to the Obsidian vault. If not provided, uses config.
        """
        resolved_path: Path | None = vault_path

        if not resolved_path:
            config = get_config()
            resolved_path = config.obsidian.vault_path

        if not resolved_path:
            msg = (
                "Obsidian vault path not configured. "
                "Set obsidian.vault_path in ~/.config/ctx/config.toml"
            )
            raise ValueError(msg)

        resolved_path = Path(resolved_path).expanduser().resolve()

        if not resolved_path.exists():
            msg = f"Obsidian vault not found: {resolved_path}"
            raise ValueError(msg)

        if not resolved_path.is_dir():
            msg = f"Obsidian vault path is not a directory: {resolved_path}"
            raise ValueError(msg)

        # Store as resolved Path (never None after validation)
        self._vault_path: Path = resolved_path

        # Load include_folders from config
        config = get_config()
        self._include_folders = config.obsidian.include_folders

    @property
    def source(self) -> Source:
        return Source.OBSIDIAN

    def _should_include_file(self, file_path: Path) -> bool:
        """Check if a file should be included based on folder filters."""
        # Skip hidden files and folders
        for part in file_path.relative_to(self._vault_path).parts:
            if part.startswith("."):
                return False

        # If no include_folders specified, include all
        if not self._include_folders:
            return True

        # Check if file is in any of the include folders
        rel_path = file_path.relative_to(self._vault_path)
        for folder in self._include_folders:
            folder_path = Path(folder)
            try:
                rel_path.relative_to(folder_path)
            except ValueError:
                continue
            else:
                return True

        return False

    def _parse_frontmatter(self, content: str) -> tuple[dict, str]:
        """Parse YAML frontmatter from markdown content.

        Returns:
            Tuple of (frontmatter dict, content without frontmatter)
        """
        match = FRONTMATTER_PATTERN.match(content)
        if not match:
            return {}, content

        frontmatter_text = match.group(1)
        content_without_frontmatter = content[match.end() :]

        try:
            frontmatter = yaml.safe_load(frontmatter_text) or {}
        except yaml.YAMLError:
            logger.warning("Failed to parse frontmatter YAML")
            frontmatter = {}

        return frontmatter, content_without_frontmatter

    def _extract_inline_tags(self, content: str) -> list[str]:
        """Extract inline #tags from content."""
        return INLINE_TAG_PATTERN.findall(content)

    def _get_all_tags(self, frontmatter: dict, content: str) -> list[str]:
        """Get all tags from frontmatter and inline tags."""
        tags: set[str] = set()

        # Frontmatter tags (can be list or single string)
        fm_tags = frontmatter.get("tags", [])
        if isinstance(fm_tags, str):
            # Handle comma-separated or single tag
            for raw_tag in fm_tags.split(","):
                cleaned = raw_tag.strip().lstrip("#")
                if cleaned:
                    tags.add(cleaned)
        elif isinstance(fm_tags, list):
            for raw_tag in fm_tags:
                if isinstance(raw_tag, str):
                    cleaned = raw_tag.strip().lstrip("#")
                    if cleaned:
                        tags.add(cleaned)

        # Inline tags
        for tag in self._extract_inline_tags(content):
            tags.add(tag)

        return sorted(tags)

    def fetch_items(self, since: datetime | None = None) -> list[dict]:
        """Fetch markdown files from the Obsidian vault."""
        items: list[dict] = []

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress:
            task = progress.add_task("Scanning vault...", total=None)

            # Walk the vault directory
            md_files = list(self._vault_path.rglob("*.md"))
            progress.update(task, description=f"Found {len(md_files)} markdown files")

            for file_path in md_files:
                # Check folder filter
                if not self._should_include_file(file_path):
                    continue

                # Check modification time
                stat = file_path.stat()
                mtime = datetime.fromtimestamp(stat.st_mtime, tz=UTC)

                if since and mtime < since:
                    continue

                items.append(
                    {
                        "path": file_path,
                        "mtime": mtime,
                        "mtime_ts": int(stat.st_mtime),
                    }
                )

            progress.update(task, description=f"Found {len(items)} files to process")

        console.print(f"[green]Found {len(items)} files to ingest[/green]")

        # Sort by modification time (newest first)
        items.sort(key=lambda x: x["mtime_ts"], reverse=True)

        return items

    def item_to_documents(self, item: dict) -> list[Document]:
        """Convert an Obsidian note to documents."""
        file_path: Path = item["path"]
        mtime_ts: int = item["mtime_ts"]

        # Read file content
        try:
            content = file_path.read_text(encoding="utf-8")
        except Exception:
            logger.warning("Failed to read file: %s", file_path)
            return []

        # Parse frontmatter
        frontmatter, body = self._parse_frontmatter(content)

        # Get tags
        tags = self._get_all_tags(frontmatter, body)

        # Build relative path for source_id and metadata
        rel_path = file_path.relative_to(self._vault_path)
        source_id = str(rel_path)

        # Get folder (parent directory relative to vault)
        folder = str(rel_path.parent) if rel_path.parent != Path() else None

        # Get title from frontmatter or filename
        title = frontmatter.get("title") or file_path.stem

        # Build content with title prefix
        full_content = f"# {title}\n\n{body.strip()}"

        # Build permalink (obsidian:// URL)
        # Format: obsidian://open?vault=VaultName&file=path/to/note
        vault_name = self._vault_path.name
        encoded_path = str(rel_path).replace(" ", "%20")
        permalink = f"obsidian://open?vault={vault_name}&file={encoded_path}"

        metadata = DocumentMetadata(
            source=Source.OBSIDIAN,
            source_id=source_id,
            timestamp=mtime_ts,
            content_type=ContentType.NOTE,
            author=None,  # Local notes don't have an author in the same sense
            my_involvement=Involvement.AUTHOR,  # User owns their vault
            permalink=permalink,
            obsidian_path=str(rel_path),
            obsidian_folder=folder,
            obsidian_tags=",".join(tags) if tags else None,
        )

        return self.create_documents_from_content(
            source_id=source_id,
            content=full_content,
            metadata=metadata,
        )

    def ingest(
        self,
        since: datetime | None = None,
        full_reindex: bool = False,
    ) -> int:
        """Run the ingestion process with progress display."""
        console.print(f"[bold]Ingesting from vault: {self._vault_path}[/bold]")

        if self._include_folders:
            console.print(f"[dim]Include folders: {', '.join(self._include_folders)}[/dim]")

        if full_reindex:
            console.print("[yellow]Full reindex: deleting existing Obsidian documents...[/yellow]")
            delete_by_source(self.source)

        # Fetch items
        items = self.fetch_items(since=since)

        if not items:
            console.print("[yellow]No files found to ingest[/yellow]")
            return 0

        # Convert to documents with progress bar
        all_documents: list[Document] = []

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TimeRemainingColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("Processing files", total=len(items))

            for item in items:
                try:
                    documents = self.item_to_documents(item)
                    all_documents.extend(documents)
                except Exception:
                    logger.exception("Failed to convert item to document: %s", item.get("path"))
                finally:
                    progress.advance(task)

        if not all_documents:
            console.print("[yellow]No documents created[/yellow]")
            return 0

        # Add to database
        console.print(f"[blue]Adding {len(all_documents)} documents to database...[/blue]")
        count = add_documents(all_documents)
        console.print(f"[green]Successfully ingested {count} documents[/green]")

        return count
