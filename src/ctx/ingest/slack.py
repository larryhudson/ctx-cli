"""Slack ingester using browser tokens."""

from __future__ import annotations

import logging
import re
import time
from datetime import UTC, datetime
from typing import Any

import httpx
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
from ctx.ingest.base import BaseIngester
from ctx.models import ContentType, Document, DocumentMetadata, Involvement, Source
from ctx.summarize import summarize_documents
from ctx.writer import delete_by_source, write_documents, write_index_files

logger = logging.getLogger(__name__)
console = Console()

# Slack web API base URL
SLACK_API_URL = "https://slack.com/api"

# Rate limiting settings
RATE_LIMIT_DELAY = 0.3  # Delay between API calls (429 retry/backoff handles actual limits)
MAX_RETRIES = 3
RETRY_BACKOFF = 2.0  # Multiplier for exponential backoff
HTTP_TOO_MANY_REQUESTS = 429


class SlackIngester(BaseIngester):
    """Ingester for Slack threads the user has participated in."""

    def __init__(self) -> None:
        """Initialize the Slack ingester with browser tokens."""
        config = get_config()
        self._xoxc_token = config.slack.token
        self._xoxd_token = config.slack.cookie

        if not self._xoxc_token or not self._xoxd_token:
            msg = (
                "Slack tokens not configured. Set slack.token and slack.cookie "
                "in ~/.config/ctx/config.toml"
            )
            raise ValueError(msg)

        self._client = httpx.Client(timeout=30.0)
        self._user_id: str | None = None
        self._user_cache: dict[str, str] = {}
        self._channel_cache: dict[str, dict[str, str]] = {}
        self._workspace: str | None = None
        self._last_api_call: float = 0

    @property
    def source(self) -> Source:
        return Source.SLACK

    def _rate_limit(self) -> None:
        """Enforce rate limiting between API calls."""
        elapsed = time.time() - self._last_api_call
        if elapsed < RATE_LIMIT_DELAY:
            time.sleep(RATE_LIMIT_DELAY - elapsed)
        self._last_api_call = time.time()

    def _api_call(self, method: str, **kwargs: Any) -> dict[str, Any]:
        """Make a Slack API call with browser token authentication and retry logic."""
        headers = {
            "Authorization": f"Bearer {self._xoxc_token}",
            "Cookie": f"d={self._xoxd_token}",
            "Content-Type": "application/x-www-form-urlencoded",
        }

        for attempt in range(MAX_RETRIES):
            self._rate_limit()

            try:
                response = self._client.post(
                    f"{SLACK_API_URL}/{method}",
                    headers=headers,
                    data=kwargs,
                )

                # Handle rate limiting
                if response.status_code == HTTP_TOO_MANY_REQUESTS:
                    retry_after = int(response.headers.get("Retry-After", 30))
                    console.print(f"[yellow]Rate limited, waiting {retry_after}s...[/yellow]")
                    time.sleep(retry_after)
                    continue

                response.raise_for_status()
                data = response.json()

                if not data.get("ok"):
                    error = data.get("error", "unknown error")
                    # Handle rate limit errors in response body
                    if error == "ratelimited":
                        delay = RETRY_BACKOFF ** (attempt + 1)
                        console.print(f"[yellow]Rate limited, waiting {delay:.0f}s...[/yellow]")
                        time.sleep(delay)
                        continue
                    msg = f"Slack API error: {error}"
                    raise RuntimeError(msg)
                return data  # noqa: TRY300

            except httpx.HTTPStatusError as e:
                if e.response.status_code == HTTP_TOO_MANY_REQUESTS and attempt < MAX_RETRIES - 1:
                    delay = RETRY_BACKOFF ** (attempt + 1)
                    console.print(f"[yellow]Rate limited, waiting {delay:.0f}s...[/yellow]")
                    time.sleep(delay)
                    continue
                raise

        msg = f"Failed after {MAX_RETRIES} retries"
        raise RuntimeError(msg)

    def _get_current_user_id(self) -> str:
        """Get the current user's ID."""
        if self._user_id is None:
            data = self._api_call("auth.test")
            self._user_id = data["user_id"]
        return self._user_id

    def _get_username(self, user_id: str) -> str:
        """Get username for a user ID, with caching."""
        if user_id not in self._user_cache:
            try:
                data = self._api_call("users.info", user=user_id)
                user = data.get("user", {})
                self._user_cache[user_id] = user.get("real_name") or user.get("name") or user_id
            except Exception:
                logger.warning("Failed to get user info for %s", user_id)
                self._user_cache[user_id] = user_id
        return self._user_cache[user_id]

    def _extract_workspace(self, permalink: str) -> str | None:
        """Extract workspace name from a Slack permalink."""
        # Format: https://{workspace}.slack.com/archives/...
        if match := re.match(r"https://([^.]+)\.slack\.com/", permalink):
            return match.group(1)
        return None

    def _build_thread_permalink(self, channel_id: str, thread_ts: str) -> str | None:
        """Build a permalink to the thread (parent message)."""
        if not self._workspace:
            return None
        # Format: https://{workspace}.slack.com/archives/{channel}/p{ts_without_dot}
        ts_without_dot = thread_ts.replace(".", "")
        return f"https://{self._workspace}.slack.com/archives/{channel_id}/p{ts_without_dot}"

    def _get_channel_info(self, channel_id: str) -> dict[str, str]:
        """Get channel info (name, type, DM partner), with caching."""
        if channel_id not in self._channel_cache:
            try:
                data = self._api_call("conversations.info", channel=channel_id)
                channel = data.get("channel", {})
                is_im = channel.get("is_im", False)
                is_mpim = channel.get("is_mpim", False)

                if is_im:
                    # Direct message - get the other user's name
                    dm_user_id = channel.get("user", "")
                    dm_user_name = self._get_username(dm_user_id) if dm_user_id else "Unknown"
                    self._channel_cache[channel_id] = {
                        "name": dm_user_name,
                        "id": channel_id,
                        "type": "dm",
                        "dm_user": dm_user_name,
                    }
                elif is_mpim:
                    # Multi-person DM / group DM
                    self._channel_cache[channel_id] = {
                        "name": channel.get("name", channel_id),
                        "id": channel_id,
                        "type": "group_dm",
                    }
                else:
                    # Regular channel
                    self._channel_cache[channel_id] = {
                        "name": channel.get("name", channel_id),
                        "id": channel_id,
                        "type": "channel",
                    }
            except Exception:
                logger.warning("Failed to get channel info for %s", channel_id)
                self._channel_cache[channel_id] = {
                    "name": channel_id,
                    "id": channel_id,
                    "type": "unknown",
                }
        return self._channel_cache[channel_id]

    def _determine_involvement(self, messages: list[dict], user_id: str) -> Involvement:
        """Determine user's involvement in a thread."""
        for msg in messages:
            if msg.get("user") == user_id:
                return Involvement.AUTHOR if msg == messages[0] else Involvement.PARTICIPANT

        # Check for mentions
        for msg in messages:
            text = msg.get("text", "")
            if f"<@{user_id}>" in text:
                return Involvement.MENTIONED

        return Involvement.PARTICIPANT

    def _clean_message_text(self, text: str) -> str:
        """Clean up Slack message text, resolving user mentions."""

        # Replace user mentions like <@U123ABC> with usernames
        def replace_user(match: re.Match[str]) -> str:
            user_id = match.group(1)
            return f"@{self._get_username(user_id)}"

        text = re.sub(r"<@([A-Z0-9]+)>", replace_user, text)

        # Replace channel mentions like <#C123ABC|channel-name>
        text = re.sub(r"<#[A-Z0-9]+\|([^>]+)>", r"#\1", text)

        # Replace URLs like <https://example.com|example.com>
        text = re.sub(r"<(https?://[^|>]+)\|[^>]+>", r"\1", text)
        return re.sub(r"<(https?://[^>]+)>", r"\1", text)

    def _format_thread_content(self, messages: list[dict], channel_info: dict[str, str]) -> str:
        """Format thread messages into a single document content.

        The first message includes channel context (e.g., "in #channel" or "in DM to Person").
        """
        lines: list[str] = []
        channel_type = channel_info.get("type", "unknown")
        channel_name = channel_info.get("name", "")

        for i, msg in enumerate(messages):
            user_id = msg.get("user", "unknown")
            username = self._get_username(user_id)
            text = self._clean_message_text(msg.get("text", ""))

            if i == 0:
                # First message includes channel context
                if channel_type == "dm":
                    dm_user = channel_info.get("dm_user", channel_name)
                    lines.append(f"{username} in DM with {dm_user}: {text}")
                elif channel_type == "group_dm":
                    lines.append(f"{username} in group DM: {text}")
                elif channel_type == "channel":
                    lines.append(f"{username} in #{channel_name}: {text}")
                else:
                    lines.append(f"{username}: {text}")
            else:
                lines.append(f"{username}: {text}")

        return "\n\n".join(lines)

    def _extract_thread_ts_from_permalink(self, permalink: str) -> str | None:
        """Extract thread_ts from permalink query string if present."""
        # Reply permalinks have format: ...?thread_ts=1234567890.123456&cid=...
        if match := re.search(r"[?&]thread_ts=([0-9.]+)", permalink):
            return match.group(1)
        return None

    def _process_search_match(self, match: dict, seen_threads: set[str]) -> dict | None:
        """Process a search match and return thread info, or None if should skip."""
        channel_id = match.get("channel", {}).get("id")
        message_ts = match.get("ts")

        # Try to get thread_ts from multiple sources:
        # 1. The thread_ts field directly (if Slack provides it)
        # 2. The permalink query string (reliable for replies)
        # 3. Fall back to the message's own ts (standalone message)
        thread_ts = match.get("thread_ts")
        if not thread_ts:
            permalink = match.get("permalink")
            if permalink:
                thread_ts = self._extract_thread_ts_from_permalink(permalink)
        if not thread_ts:
            thread_ts = message_ts

        if not channel_id or not thread_ts:
            return None

        # Skip if we've already seen this thread (O(1) lookup)
        thread_key = f"{channel_id}-{thread_ts}"
        if thread_key in seen_threads:
            return None
        seen_threads.add(thread_key)

        # reply_count is only on parent messages, not replies
        reply_count = match.get("reply_count", 0)
        # If this is a reply (thread_ts != ts), it's part of a thread
        is_reply = thread_ts != message_ts

        return {
            "channel_id": channel_id,
            "thread_ts": thread_ts,
            "thread_key": thread_key,
            "timestamp": float(thread_ts),
            "search_result": match,
            "is_reply": is_reply,
            "reply_count": reply_count if not is_reply else 1,  # Force API for threads
        }

    def fetch_items(self, since: datetime | None = None) -> list[dict]:
        """Fetch Slack threads the user has participated in."""
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress:
            task = progress.add_task("Authenticating with Slack...", total=None)

            user_id = self._get_current_user_id()
            progress.update(task, description=f"Authenticated as user {user_id}")

            threads: list[dict] = []
            seen_threads: set[str] = set()  # O(1) deduplication

            # Search for messages from the user
            query = f"from:<@{user_id}>"
            if since:
                after_date = since.strftime("%Y-%m-%d")
                query += f" after:{after_date}"

            console.print(f"[dim]Search query: {query}[/dim]")
            progress.update(task, description="Searching for messages...")

            page = 1
            while True:
                progress.update(task, description=f"Searching messages (page {page})...")

                params: dict[str, Any] = {
                    "query": query,
                    "sort": "timestamp",
                    "sort_dir": "desc",
                    "count": 100,
                    "page": page,
                }

                try:
                    data = self._api_call("search.messages", **params)
                except Exception as e:
                    console.print(f"[red]Error searching messages: {e}[/red]")
                    break

                messages = data.get("messages", {})
                matches = messages.get("matches", [])

                # Extract workspace from first permalink we see
                if not self._workspace:
                    for m in matches:
                        if permalink := m.get("permalink"):
                            self._workspace = self._extract_workspace(permalink)
                            break

                threads.extend(
                    info
                    for match in matches
                    if (info := self._process_search_match(match, seen_threads))
                )

                progress.update(task, description=f"Found {len(threads)} threads (page {page})...")

                # Check for pagination
                paging = messages.get("paging", {})
                total_pages = paging.get("pages", 1)
                if page >= total_pages:
                    break

                page += 1

            progress.update(task, description=f"Found {len(threads)} threads")

        console.print(f"[green]Found {len(threads)} threads to process[/green]")

        # Debug: show timestamp range
        if threads:

            def ts_to_date(ts: float) -> str:
                return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d %H:%M")

            console.print("[dim]First 3 threads (newest):[/dim]")
            for t in threads[:3]:
                console.print(f"  [dim]{ts_to_date(t['timestamp'])} - {t['thread_key'][:20]}[/dim]")

            console.print("[dim]Last 3 threads (oldest):[/dim]")
            for t in threads[-3:]:
                console.print(f"  [dim]{ts_to_date(t['timestamp'])} - {t['thread_key'][:20]}[/dim]")
        return threads

    def _prefetch_channels(self, items: list[dict]) -> None:
        """Pre-fetch channel info for all items to batch API calls."""
        channel_ids = {item["channel_id"] for item in items}
        to_fetch = [cid for cid in channel_ids if cid not in self._channel_cache]
        if to_fetch:
            console.print(f"[dim]Pre-fetching {len(to_fetch)} channels...[/dim]")
            for channel_id in to_fetch:
                self._get_channel_info(channel_id)

    def _prefetch_users(self, items: list[dict]) -> None:
        """Pre-fetch user info for all items to batch API calls."""
        user_ids: set[str] = set()
        for item in items:
            if search_result := item.get("search_result"):
                # Get the message author
                if uid := search_result.get("user"):
                    user_ids.add(uid)
                if uid := search_result.get("bot_id"):
                    user_ids.add(uid)
                # Extract @mentions from text
                for mention in re.findall(r"<@([A-Z0-9]+)>", search_result.get("text", "")):
                    user_ids.add(mention)

        to_fetch = [uid for uid in user_ids if uid not in self._user_cache]
        if to_fetch:
            console.print(f"[dim]Pre-fetching {len(to_fetch)} users...[/dim]")
            for user_id in to_fetch:
                self._get_username(user_id)

    def _is_search_result_complete(self, search_result: dict) -> bool:
        """Check if search result has complete data (not truncated)."""
        text = search_result.get("text", "")
        # Check for truncation indicators
        if text.endswith(("...", "…")):
            return False
        # Check for required fields
        if not search_result.get("ts"):
            return False
        # Must have either user or bot_id
        return bool(search_result.get("user") or search_result.get("bot_id"))

    def item_to_documents(self, item: dict) -> list[Document]:
        """Convert a Slack thread to documents."""
        channel_id = item["channel_id"]
        thread_ts = item["thread_ts"]
        user_id = self._get_current_user_id()

        reply_count = item.get("reply_count", 0)
        search_result = item.get("search_result")

        # Optimization: For standalone messages (no replies), use search result directly
        # This avoids an API call for ~60% of messages
        if reply_count == 0 and search_result and self._is_search_result_complete(search_result):
            messages = [
                {
                    "user": search_result.get("user") or search_result.get("bot_id", "unknown"),
                    "text": search_result.get("text", ""),
                    "ts": search_result.get("ts"),
                }
            ]
        else:
            # Threaded message or incomplete data - need full context from API
            try:
                data = self._api_call(
                    "conversations.replies",
                    channel=channel_id,
                    ts=thread_ts,
                    limit=100,
                )
                messages = data.get("messages", [])
            except Exception:
                logger.warning("Failed to fetch thread %s in channel %s", thread_ts, channel_id)
                return []

        if not messages:
            return []

        # Get channel info
        channel_info = self._get_channel_info(channel_id)
        channel_name = channel_info["name"]

        # Determine involvement
        involvement = self._determine_involvement(messages, user_id)

        # Get thread author
        author_id = messages[0].get("user", "unknown")
        author = self._get_username(author_id)

        # Format content
        content = self._format_thread_content(messages, channel_info)

        # Parse timestamp
        thread_timestamp = int(float(thread_ts))

        # Build source_id: channel_id + thread_ts
        source_id = f"{channel_id}-{thread_ts}"

        # Build permalink to the thread (not the specific message we found)
        permalink = self._build_thread_permalink(channel_id, thread_ts)

        # Reply count is number of messages minus the parent
        reply_count = len(messages) - 1

        metadata = DocumentMetadata(
            source=Source.SLACK,
            source_id=source_id,
            timestamp=thread_timestamp,
            content_type=ContentType.MESSAGE,
            author=author,
            my_involvement=involvement,
            permalink=permalink,
            slack_channel=channel_name,
            slack_channel_id=channel_id,
            slack_thread_ts=thread_ts,
            slack_reply_count=reply_count,
        )

        return self.create_documents_from_content(
            source_id=source_id,
            content=content,
            metadata=metadata,
        )

    def ingest(
        self,
        since: datetime | None = None,
        full_reindex: bool = False,
    ) -> int:
        """Run the ingestion process with progress display."""
        if full_reindex:
            console.print("[yellow]Full reindex: deleting existing Slack documents...[/yellow]")
            delete_by_source(self.source)

        # Fetch items
        items = self.fetch_items(since=since)

        if not items:
            console.print("[yellow]No threads found to ingest[/yellow]")
            return 0

        # Batch prefetch channels and users to minimize API calls during processing
        self._prefetch_channels(items)
        self._prefetch_users(items)

        # Convert to documents with progress bar and ETA
        all_documents: list[Document] = []

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TimeRemainingColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("Processing threads", total=len(items))

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

        # Summarize and write to markdown files
        console.print(f"[blue]Summarizing {len(all_documents)} documents...[/blue]")
        summarize_documents(all_documents)
        console.print(f"[blue]Writing {len(all_documents)} documents...[/blue]")
        count = write_documents(all_documents)
        write_index_files(all_documents)
        console.print(f"[green]Successfully ingested {count} documents[/green]")

        return count
