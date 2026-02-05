# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**ctx** is a CLI tool that aggregates work context from multiple sources (Slack, Linear, GitHub, Notion, Obsidian) into a ChromaDB vector database for semantic and keyword search.

Source code lives in `src/ctx/`. See `SPEC.md` for the full specification.

## Architecture

```
src/ctx/
├── models.py      # Pydantic models: Source, ContentType, Involvement, DocumentMetadata, Document
├── config.py      # Config loading from ~/.config/ctx/config.toml
├── db.py          # ChromaDB connection (Database singleton), search/add/delete operations
├── chunking.py    # Text chunking utilities (token-based, paragraph-aware)
├── ingest/
│   ├── base.py    # BaseIngester ABC - subclass for each source
│   ├── slack.py   # Slack threads (browser tokens)
│   ├── linear.py  # Linear issues (GraphQL API)
│   ├── github.py  # GitHub PRs (GraphQL API)
│   ├── notion.py  # Notion pages (official SDK)
│   └── obsidian.py # Obsidian notes (filesystem)
└── cli/
    └── main.py    # Typer CLI: search, info, get, ingest commands
```

### Key Components

**models.py** - Data models
- `Source` enum: slack, linear, github, notion, obsidian
- `DocumentMetadata`: flat metadata schema for ChromaDB (source-specific fields prefixed)
- `Document`: id + content + metadata, ready for indexing

**db.py** - Database layer
- `Database` class: singleton managing ChromaDB client and collection
- `add_documents()`, `delete_by_source()`: write operations
- `search()`, `keyword_search()`: query operations with filtering
- `get_stats()`, `get_document_by_id()`: read operations

**ingest/base.py** - Ingestion framework
- `BaseIngester` ABC: implement `source`, `fetch_items()`, `item_to_documents()`
- `create_documents_from_content()`: helper that handles chunking
- `parse_since()`: converts "7d", "24h", "2w" strings to datetime

**cli/main.py** - User interface
- `ctx search <query>` - semantic search with filters
- `ctx info` - database statistics
- `ctx get <id>` - retrieve single document

### Document ID Format

```
{source}:{source_id}:{chunk_index}
```

Examples: `slack:C123-1234567.123:0`, `linear:ENG-123:0`, `github:owner/repo/pr/42:1`

## Development Commands

```bash
# Install dependencies
uv sync

# Run the CLI
uv run ctx --help
uv run ctx search "query"
uv run ctx info

# Linting and formatting
uv run ruff format .
uv run ruff check .
uv run ruff check --fix .

# Type checking
uv run ty check
```

## Automated Checks

A Claude Code hook runs after every Edit/Write on Python files:
1. Auto-formats with ruff (silent)
2. Reports lint errors (no auto-fix to preserve unused imports during multi-file edits)
3. Reports type errors from ty

Fix any reported errors before proceeding.

## Git Workflow

- One commit per PR. Squash before pushing if needed.

## Code Style

- Line length: 100 characters
- Python 3.12+ features encouraged (StrEnum, `|` unions, etc.)
- Use `pathlib.Path` over `os.path`
- No print statements in production code (use logging or rich console)
- Import sorting: stdlib → third-party → first-party (`ctx`)

## Adding a New Ingester

1. Create `src/ctx/ingest/{source}.py`
2. Subclass `BaseIngester`
3. Implement:
   - `source` property → return `Source.{SOURCE}`
   - `fetch_items(since)` → call API, return list of raw items
   - `item_to_documents(item)` → convert to `Document` list (use `create_documents_from_content()`)
4. Add CLI command in `cli/main.py` if needed

## Writing Efficient Ingesters

Lessons learned from optimizing the Slack ingester (reduced 400s → ~5s for 400 threads).

### 1. Minimize API Calls

**Batch lookups upfront.** Don't fetch user/channel info inside item processing loops. Instead:
```python
def _prefetch_channels(self, items: list[dict]) -> None:
    channel_ids = {item["channel_id"] for item in items}
    to_fetch = [cid for cid in channel_ids if cid not in self._channel_cache]
    for channel_id in to_fetch:
        self._get_channel_info(channel_id)  # Populates cache
```
Call this once after `fetch_items()` returns, before processing.

**Skip API calls when data is already available.** Search APIs often return enough data for standalone items:
```python
# If search result has complete data, skip the detail fetch
if item.get("reply_count") == 0 and self._is_data_complete(search_result):
    # Use search result directly
else:
    # Fetch full details from API
```

**Cache aggressively.** User IDs, channel names, and other lookup data should be cached for the session:
```python
def _get_username(self, user_id: str) -> str:
    if user_id not in self._user_cache:
        # API call...
        self._user_cache[user_id] = result
    return self._user_cache[user_id]
```

### 2. Use O(1) Data Structures for Deduplication

**Bad (O(n²)):**
```python
if any(t.get("thread_key") == thread_key for t in threads):
    continue
```

**Good (O(1)):**
```python
seen_threads: set[str] = set()
# ...
if thread_key in seen_threads:
    continue
seen_threads.add(thread_key)
```

### 3. Extract Parent IDs from Multiple Sources

APIs may not always return parent/thread IDs directly. Look for them in:
- Direct fields (`thread_ts`, `parent_id`)
- Permalink URLs (`?thread_ts=123.456` in query string)
- Nested objects

```python
def _extract_thread_ts_from_permalink(self, permalink: str) -> str | None:
    # Reply permalinks: ...?thread_ts=1234567890.123456&cid=...
    if match := re.search(r"[?&]thread_ts=([0-9.]+)", permalink):
        return match.group(1)
    return None
```

### 4. Rate Limiting Strategy

**Be aggressive but handle limits gracefully:**
```python
RATE_LIMIT_DELAY = 0.3  # Start low, let 429s guide you

def _api_call(self, method: str, **kwargs) -> dict:
    for attempt in range(MAX_RETRIES):
        self._rate_limit()  # Enforce minimum delay
        response = self._client.post(...)

        if response.status_code == 429:
            retry_after = int(response.headers.get("Retry-After", 30))
            time.sleep(retry_after)
            continue
        # ...
```

### 5. Store Metadata for Filtering

Add useful fields to `DocumentMetadata` for later filtering:
- `permalink` - clickable link to view externally
- `slack_reply_count` - filter by threads vs standalone
- `my_involvement` - filter by author/participant/mentioned

Build permalinks consistently (to parent, not the found message):
```python
def _build_thread_permalink(self, channel_id: str, thread_ts: str) -> str:
    ts_without_dot = thread_ts.replace(".", "")
    return f"https://{self._workspace}.slack.com/archives/{channel_id}/p{ts_without_dot}"
```

### 6. Progress Display

Use Rich progress bars with ETA for long operations:
```python
with Progress(
    SpinnerColumn(),
    TextColumn("[progress.description]{task.description}"),
    BarColumn(),
    TaskProgressColumn(),
    TimeRemainingColumn(),
    console=console,
) as progress:
    task = progress.add_task("Processing", total=len(items))
    for item in items:
        # ... process ...
        progress.advance(task)
```

### 7. Test Incrementally

Before running full ingestion:
1. Test regex/parsing logic with mock data
2. Test individual API calls with real credentials
3. Verify the full flow for a single item
4. Then run the full ingestion
