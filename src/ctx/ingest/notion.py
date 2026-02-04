"""Notion ingester using the official Python SDK with async block fetching."""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING, Any, cast

import httpx
from notion_client import Client
from notion_client.errors import APIResponseError
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

from ctx.config import get_config
from ctx.db import add_documents, delete_by_source
from ctx.ingest.base import BaseIngester
from ctx.models import ContentType, Document, DocumentMetadata, Involvement, Source

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)
console = Console()

# Notion API settings
NOTION_API_URL = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

# Block recursion depth limit
MAX_BLOCK_DEPTH = 2  # Reduced for speed

# Child page crawl depth limit (how deep to go when discovering pages)
MAX_CHILD_PAGE_DEPTH = 1  # Only direct children, not grandchildren

# Concurrency settings
MAX_CONCURRENT_REQUESTS = 5  # Notion allows ~3/sec, but we batch

# Block types to skip entirely
SKIP_BLOCKS = frozenset({"table_of_contents", "breadcrumb", "unsupported"})


def _extract_rich_text(rich_text: list[dict[str, Any]]) -> str:
    """Extract plain text from Notion rich text array."""
    return "".join(item.get("plain_text", "") for item in rich_text)


def _text_with_prefix(block_data: dict[str, Any], prefix: str = "") -> str:
    """Extract rich_text with optional prefix."""
    text = _extract_rich_text(block_data.get("rich_text", []))
    return f"{prefix}{text}\n" if text else ""


def _heading_handler(level: int) -> Callable[[dict[str, Any]], str]:
    """Create a handler for heading blocks."""

    def handler(block_data: dict[str, Any]) -> str:
        text = _extract_rich_text(block_data.get("rich_text", []))
        return f"{'#' * level} {text}\n" if text else ""

    return handler


def _todo_handler(block_data: dict[str, Any]) -> str:
    """Handle to_do blocks."""
    text = _extract_rich_text(block_data.get("rich_text", []))
    checkbox = "[x]" if block_data.get("checked", False) else "[ ]"
    return f"- {checkbox} {text}\n" if text else ""


def _code_handler(block_data: dict[str, Any]) -> str:
    """Handle code blocks."""
    text = _extract_rich_text(block_data.get("rich_text", []))
    language = block_data.get("language", "")
    return f"```{language}\n{text}\n```\n" if text else ""


def _callout_handler(block_data: dict[str, Any]) -> str:
    """Handle callout blocks."""
    text = _extract_rich_text(block_data.get("rich_text", []))
    icon = block_data.get("icon", {}).get("emoji", "")
    return f"> {icon} {text}\n" if text else ""


def _child_page_handler(block_data: dict[str, Any]) -> str:
    """Handle child_page blocks."""
    return f"[{block_data.get('title', 'Untitled')}](child_page)\n"


def _child_database_handler(block_data: dict[str, Any]) -> str:
    """Handle child_database blocks."""
    return f"[{block_data.get('title', 'Untitled Database')}](child_database)\n"


def _image_handler(block_data: dict[str, Any]) -> str:
    """Handle image blocks."""
    caption = _extract_rich_text(block_data.get("caption", []))
    return f"![{caption or 'image'}](image)\n"


def _bookmark_handler(block_data: dict[str, Any]) -> str:
    """Handle bookmark blocks."""
    url = block_data.get("url", "")
    caption = _extract_rich_text(block_data.get("caption", []))
    return f"[{caption or url}]({url})\n" if url else ""


def _equation_handler(block_data: dict[str, Any]) -> str:
    """Handle equation blocks."""
    expression = block_data.get("expression", "")
    return f"$${expression}$$\n" if expression else ""


def _table_row_handler(block_data: dict[str, Any]) -> str:
    """Handle table_row blocks."""
    cells = block_data.get("cells", [])
    row_text = " | ".join(_extract_rich_text(cell) for cell in cells)
    return f"| {row_text} |\n"


def _divider_handler(_: dict[str, Any]) -> str:
    """Handle divider blocks."""
    return "---\n"


# Dispatch table for block type handlers
BLOCK_HANDLERS: dict[str, Callable[[dict[str, Any]], str]] = {
    "paragraph": _text_with_prefix,
    "bulleted_list_item": partial(_text_with_prefix, prefix="- "),
    "numbered_list_item": partial(_text_with_prefix, prefix="1. "),
    "quote": partial(_text_with_prefix, prefix="> "),
    "toggle": _text_with_prefix,
    "heading_1": _heading_handler(1),
    "heading_2": _heading_handler(2),
    "heading_3": _heading_handler(3),
    "to_do": _todo_handler,
    "code": _code_handler,
    "callout": _callout_handler,
    "divider": _divider_handler,
    "child_page": _child_page_handler,
    "child_database": _child_database_handler,
    "image": _image_handler,
    "bookmark": _bookmark_handler,
    "equation": _equation_handler,
    "table_row": _table_row_handler,
}


def _block_to_markdown(block: dict[str, Any]) -> str:
    """Convert a single Notion block to markdown."""
    block_type = block.get("type", "")

    if block_type in SKIP_BLOCKS:
        return ""

    block_data = block.get(block_type, {})

    # Use dispatch table if handler exists
    if handler := BLOCK_HANDLERS.get(block_type):
        return handler(block_data)

    # Fallback: try to extract any rich_text
    if "rich_text" in block_data:
        return _text_with_prefix(block_data)

    return ""


def _blocks_to_markdown(blocks: list[dict[str, Any]]) -> str:
    """Convert blocks to markdown."""
    return "\n".join(md for block in blocks if (md := _block_to_markdown(block)))


class NotionIngester(BaseIngester):
    """Ingester for Notion pages and databases with async block fetching."""

    def __init__(self) -> None:
        """Initialize the Notion ingester."""
        # Try environment variable first, then config
        token = os.environ.get("NOTION_TOKEN")

        if not token:
            config = get_config()
            token = config.notion.token

        if not token:
            msg = (
                "Notion token not configured. Set NOTION_TOKEN environment variable "
                "or configure notion.token in config.toml"
            )
            raise ValueError(msg)

        self._token = token
        self._client = Client(auth=token)
        self._http_client = httpx.Client(timeout=30.0)
        self._user_cache: dict[str, str] = {}
        self._workspace_name: str | None = None

        # User identification for involvement detection
        self._my_user_id = os.environ.get("NOTION_USER_ID")
        self._my_user_name = os.environ.get("NOTION_USER_NAME")

        # Root pages to crawl (if set, only crawl these instead of searching all)
        root_pages_str = os.environ.get("NOTION_ROOT_PAGES", "")
        self._root_pages = [p.strip() for p in root_pages_str.split(",") if p.strip()]

    @property
    def source(self) -> Source:
        return Source.NOTION

    def _get_user_name(self, user_id: str) -> str:
        """Get user name from cache or API."""
        if user_id not in self._user_cache:
            try:
                user = cast("dict[str, Any]", self._client.users.retrieve(user_id))
                self._user_cache[user_id] = user.get("name", user_id)
            except APIResponseError:
                logger.warning("Failed to get user info for %s", user_id)
                self._user_cache[user_id] = user_id
        return self._user_cache[user_id]

    def _get_current_user(self) -> dict[str, Any]:
        """Get the current bot user info."""
        return cast("dict[str, Any]", self._client.users.me())

    def _get_page_title(self, page: dict[str, Any]) -> str:
        """Extract title from a page object."""
        properties = page.get("properties", {})

        # Try common title property names
        for prop_name in ("title", "Title", "Name", "name"):
            if prop_name in properties:
                prop = properties[prop_name]
                if prop.get("type") == "title":
                    return _extract_rich_text(prop.get("title", []))

        # Fallback: search for any title type property
        for prop in properties.values():
            if prop.get("type") == "title":
                title = _extract_rich_text(prop.get("title", []))
                if title:
                    return title

        return "Untitled"

    def _get_page_author(self, page: dict[str, Any]) -> str:
        """Get the author (created_by) of a page."""
        created_by = page.get("created_by", {})
        if created_by.get("type") == "person":
            user_id = created_by.get("id", "")
            if user_id:
                return self._get_user_name(user_id)
        return "Unknown"

    def _determine_involvement(self, page: dict[str, Any], content: str) -> Involvement | None:
        """Determine user's involvement with a page."""
        if not self._my_user_id:
            return None

        created_by_id = page.get("created_by", {}).get("id")
        last_edited_by_id = page.get("last_edited_by", {}).get("id")

        # Author if user created the page
        if created_by_id == self._my_user_id:
            return Involvement.AUTHOR

        # Participant if user last edited the page
        if last_edited_by_id == self._my_user_id:
            return Involvement.PARTICIPANT

        # Mentioned if user's name appears in content
        if self._my_user_name and self._my_user_name.lower() in content.lower():
            return Involvement.MENTIONED

        return None

    def _query_database(self, database_id: str, start_cursor: str | None = None) -> dict[str, Any]:
        """Query a database using direct HTTP (SDK doesn't support this)."""
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }
        body: dict[str, Any] = {
            "sorts": [{"timestamp": "last_edited_time", "direction": "descending"}],
        }
        if start_cursor:
            body["start_cursor"] = start_cursor

        url = f"{NOTION_API_URL}/databases/{database_id}/query"
        response = self._http_client.post(url, headers=headers, json=body)
        response.raise_for_status()
        return response.json()

    def _fetch_database_pages(
        self, database_id: str, since: datetime | None = None
    ) -> list[dict[str, Any]]:
        """Fetch all pages from a database."""
        pages: list[dict[str, Any]] = []
        cursor: str | None = None

        while True:
            try:
                response = self._query_database(database_id, cursor)
            except httpx.HTTPStatusError as e:
                logger.warning("Failed to query database %s: %s", database_id, e)
                break

            for page in response.get("results", []):
                if since:
                    last_edited = page.get("last_edited_time", "")
                    if last_edited:
                        edited_dt = datetime.fromisoformat(last_edited)
                        if edited_dt < since:
                            return pages
                pages.append(page)

            if not response.get("has_more", False):
                break
            cursor = response.get("next_cursor")

        return pages

    def _fetch_child_pages(
        self, parent_id: str, since: datetime | None = None, depth: int = 0
    ) -> list[dict[str, Any]]:
        """Fetch child pages from a page up to MAX_CHILD_PAGE_DEPTH."""
        if depth >= MAX_CHILD_PAGE_DEPTH:
            return []

        pages: list[dict[str, Any]] = []

        try:
            # Get blocks to find child_page and child_database blocks
            cursor = None
            while True:
                response = cast(
                    "dict[str, Any]",
                    self._client.blocks.children.list(block_id=parent_id, start_cursor=cursor),
                )

                for block in response.get("results", []):
                    block_type = block.get("type")

                    if block_type == "child_page":
                        # Fetch the page details
                        try:
                            page = cast("dict[str, Any]", self._client.pages.retrieve(block["id"]))
                            if since:
                                last_edited = page.get("last_edited_time", "")
                                if last_edited:
                                    edited_dt = datetime.fromisoformat(last_edited)
                                    if edited_dt < since:
                                        continue
                            pages.append(page)
                            # Recursively get children (with depth limit)
                            pages.extend(self._fetch_child_pages(block["id"], since, depth + 1))
                        except APIResponseError:
                            pass

                    elif block_type == "child_database":
                        # Query database for its pages
                        pages.extend(self._fetch_database_pages(block["id"], since))

                if not response.get("has_more", False):
                    break
                cursor = response.get("next_cursor")

        except APIResponseError as e:
            logger.warning("Failed to fetch children for %s: %s", parent_id, e)

        return pages

    def _get_parent_info(self, page: dict[str, Any]) -> str | None:
        """Get parent page/database title if available."""
        parent = page.get("parent", {})
        parent_type = parent.get("type")

        try:
            if parent_type == "page_id":
                parent_page = cast("dict[str, Any]", self._client.pages.retrieve(parent["page_id"]))
                return self._get_page_title(parent_page)

            if parent_type == "database_id":
                db = cast("dict[str, Any]", self._client.databases.retrieve(parent["database_id"]))
                return _extract_rich_text(db.get("title", []))
        except APIResponseError:
            pass

        return None

    def _fetch_from_root_pages(self, since: datetime | None = None) -> list[dict[str, Any]]:
        """Fetch pages from configured root pages/databases."""
        pages: list[dict[str, Any]] = []

        for root_id in self._root_pages:
            console.print(f"[dim]Crawling root: {root_id}[/dim]")

            # First, try to fetch as a page
            try:
                page = cast("dict[str, Any]", self._client.pages.retrieve(root_id))
                # It's a page - add it and crawl children
                if since:
                    last_edited = page.get("last_edited_time", "")
                    if last_edited:
                        edited_dt = datetime.fromisoformat(last_edited)
                        if edited_dt >= since:
                            pages.append(page)
                else:
                    pages.append(page)
                pages.extend(self._fetch_child_pages(root_id, since))
                continue
            except APIResponseError:
                pass

            # Not a page, try as a database
            try:
                pages.extend(self._fetch_database_pages(root_id, since))
            except APIResponseError as e:
                console.print(f"[yellow]Could not fetch {root_id}: {e}[/yellow]")

        return pages

    def fetch_items(self, since: datetime | None = None) -> list[dict[str, Any]]:
        """Fetch Notion pages the integration has access to."""
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress:
            task = progress.add_task("Connecting to Notion...", total=None)

            # Get workspace info
            try:
                bot_user = self._get_current_user()
                self._workspace_name = bot_user.get("bot", {}).get("workspace_name", "Unknown")
                progress.update(task, description=f"Connected to {self._workspace_name}")
            except APIResponseError as e:
                logger.warning("Failed to get workspace info: %s", e)
                self._workspace_name = "Unknown"

            # If root pages configured, crawl those specifically
            if self._root_pages:
                progress.update(task, description=f"Crawling {len(self._root_pages)} root pages...")
                pages = self._fetch_from_root_pages(since)
                progress.update(task, description=f"Found {len(pages)} pages")
                console.print(f"[green]Found {len(pages)} pages to process[/green]")
                return pages

            # Otherwise, search all pages the integration can access
            pages: list[dict[str, Any]] = []
            cursor: str | None = None
            page_num = 1

            while True:
                progress.update(task, description=f"Searching pages (batch {page_num})...")

                try:
                    response = cast(
                        "dict[str, Any]",
                        self._client.search(
                            filter={"property": "object", "value": "page"},
                            sort={"direction": "descending", "timestamp": "last_edited_time"},
                            start_cursor=cursor,
                        ),
                    )
                except APIResponseError as e:
                    console.print(f"[red]Error searching: {e}[/red]")
                    break

                for page in response.get("results", []):
                    # Filter by last_edited_time if since is specified
                    if since:
                        last_edited = page.get("last_edited_time", "")
                        if last_edited:
                            edited_dt = datetime.fromisoformat(last_edited)
                            if edited_dt < since:
                                # Pages sorted desc, so we can stop
                                desc = f"Found {len(pages)} pages since {since.date()}"
                                progress.update(task, description=desc)
                                return pages

                    pages.append(page)

                progress.update(task, description=f"Found {len(pages)} pages...")

                if not response.get("has_more", False):
                    break

                cursor = response.get("next_cursor")
                page_num += 1

            progress.update(task, description=f"Found {len(pages)} pages")

        console.print(f"[green]Found {len(pages)} pages to process[/green]")
        return pages

    def item_to_documents(self, item: dict[str, Any]) -> list[Document]:
        """Convert a Notion page to documents (sync version, not used)."""
        # This is overridden by async processing in ingest()
        # but required by base class
        raise NotImplementedError("Use async processing via ingest()")

    # --- Async methods for parallel block fetching ---

    async def _fetch_blocks_async(
        self,
        client: httpx.AsyncClient,
        semaphore: asyncio.Semaphore,
        page_id: str,
        depth: int = 0,
    ) -> list[dict[str, Any]]:
        """Async version of block fetching with semaphore for rate limiting."""
        if depth > MAX_BLOCK_DEPTH:
            return []

        blocks: list[dict[str, Any]] = []
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Notion-Version": NOTION_VERSION,
        }

        cursor: str | None = None
        while True:
            async with semaphore:
                url = f"{NOTION_API_URL}/blocks/{page_id}/children"
                params = {}
                if cursor:
                    params["start_cursor"] = cursor

                try:
                    response = await client.get(url, headers=headers, params=params)
                    response.raise_for_status()
                    data = response.json()
                except httpx.HTTPStatusError as e:
                    logger.warning("Failed to fetch blocks for %s: %s", page_id, e)
                    return blocks

            results = data.get("results", [])

            # Collect blocks that need children fetched
            # Skip child_page and child_database - they're separate documents, not nested content
            child_tasks = []
            for block in results:
                blocks.append(block)
                block_type = block.get("type")
                if block.get("has_children", False) and block_type not in (
                    "child_page",
                    "child_database",
                ):
                    child_tasks.append(
                        self._fetch_blocks_async(client, semaphore, block["id"], depth + 1)
                    )

            # Fetch children in parallel
            if child_tasks:
                children_results = await asyncio.gather(*child_tasks)
                for children in children_results:
                    blocks.extend(children)

            if not data.get("has_more", False):
                break
            cursor = data.get("next_cursor")

        return blocks

    async def _process_page_async(
        self,
        client: httpx.AsyncClient,
        semaphore: asyncio.Semaphore,
        item: dict[str, Any],
    ) -> list[Document]:
        """Process a single page asynchronously."""
        page_id = item.get("id", "").replace("-", "")
        title = self._get_page_title(item)

        # Fetch blocks asynchronously
        blocks = await self._fetch_blocks_async(client, semaphore, item["id"])
        content = _blocks_to_markdown(blocks)

        # Prepend title to content
        full_content = f"# {title}\n\n{content}" if content else f"# {title}"

        if not full_content.strip():
            return []

        # Parse timestamps
        last_edited = item.get("last_edited_time", "")
        try:
            edited_dt = datetime.fromisoformat(last_edited)
            timestamp = int(edited_dt.timestamp())
        except ValueError:
            timestamp = int(datetime.now(tz=UTC).timestamp())

        # Get author (sync call, cached)
        author = self._get_page_author(item)

        # Skip parent info fetch for speed - can be slow
        parent_title = None

        # Build permalink
        permalink = item.get("url")

        # Determine involvement
        my_involvement = self._determine_involvement(item, full_content)

        metadata = DocumentMetadata(
            source=Source.NOTION,
            source_id=page_id,
            timestamp=timestamp,
            content_type=ContentType.DOC,
            author=author,
            my_involvement=my_involvement,
            permalink=permalink,
            notion_workspace=self._workspace_name,
            notion_page_id=page_id,
            notion_parent=parent_title,
        )

        return self.create_documents_from_content(
            source_id=page_id,
            content=full_content,
            metadata=metadata,
        )

    async def _process_pages_async(self, items: list[dict[str, Any]]) -> list[Document]:
        """Process all pages in parallel with rate limiting."""
        semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
        all_documents: list[Document] = []

        async with httpx.AsyncClient(timeout=30.0) as client:
            # Process in batches to show progress
            batch_size = 5
            for i in range(0, len(items), batch_size):
                batch = items[i : i + batch_size]
                tasks = [self._process_page_async(client, semaphore, item) for item in batch]
                results = await asyncio.gather(*tasks, return_exceptions=True)

                for result in results:
                    if isinstance(result, BaseException):
                        logger.exception("Failed to process page: %s", result)
                    elif isinstance(result, list):
                        all_documents.extend(result)

                console.print(
                    f"[dim]Processed {min(i + batch_size, len(items))}/{len(items)} pages[/dim]"
                )

        return all_documents

    def ingest(
        self,
        since: datetime | None = None,
        full_reindex: bool = False,
    ) -> int:
        """Run the ingestion process with parallel block fetching."""
        if full_reindex:
            console.print("[yellow]Full reindex: deleting existing Notion documents...[/yellow]")
            delete_by_source(self.source)

        # Fetch items (page metadata)
        items = self.fetch_items(since=since)

        if not items:
            console.print("[yellow]No pages found to ingest[/yellow]")
            return 0

        # Process pages in parallel using asyncio
        console.print(f"[blue]Fetching content for {len(items)} pages (parallel)...[/blue]")
        all_documents = asyncio.run(self._process_pages_async(items))

        if not all_documents:
            console.print("[yellow]No documents created[/yellow]")
            return 0

        # Add to database
        console.print(f"[blue]Adding {len(all_documents)} documents to database...[/blue]")
        count = add_documents(all_documents)
        console.print(f"[green]Successfully ingested {count} documents[/green]")

        return count
