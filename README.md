# ctx

A CLI tool that aggregates work context from multiple sources into a unified semantic search database.

## Overview

**ctx** pulls data from Slack, Linear, GitHub, Notion, and Obsidian into a ChromaDB vector database, enabling semantic and keyword search across all your work context in one place.

This is particularly useful for:
- AI agents that need relevant context from your work history
- Searching across all your work tools with a single query
- Building a personal knowledge base from your day-to-day work

## Features

- **Unified search** - Semantic and keyword search across all sources
- **Multiple output formats** - Markdown, table, or JSON for LLM consumption
- **Flexible filtering** - By source, time range, or involvement level
- **Incremental sync** - Only fetch new content since last ingestion
- **Token-aware chunking** - Preserves semantic boundaries for better search results
- **Local-first** - Embeddings run locally by default (no API costs)

## Installation

Requires Python 3.12+.

```bash
# Clone the repository
git clone https://github.com/larryhudson/work-context.git
cd work-context

# Install with uv (recommended)
uv sync

# Or with pip
pip install -e .
```

## Configuration

All configuration lives in `~/.config/ctx/config.toml`:

```toml
[database]
path = "~/.local/share/ctx/chroma_data"

[embedding]
model = "default"  # Uses all-MiniLM-L6-v2 locally (no API key needed)
# model = "openai"
# openai_api_key = "sk-..."

[slack]
token = "xoxc-..."   # Browser token (see setup below)
cookie = "xoxd-..."  # Browser cookie

[linear]
api_key = "lin_api_..."

[github]
token = "ghp_..."  # Optional if using gh CLI
repos = ["org/repo1", "org/repo2"]

[notion]
token = "secret_..."
root_pages = ["Page ID 1", "Page ID 2"]  # Optional: limit to specific pages
# user_id = "..."     # Optional: for involvement detection
# user_name = "..."   # Optional: for mention detection

[obsidian]
vault_path = "~/Documents/Obsidian/Work"
include_folders = ["daily-notes", "meetings"]  # Optional
```

Environment variables with `CTX_` prefix can override config values (e.g., `CTX_SLACK__TOKEN`).

### Data Source Setup

#### Slack

Slack ingestion uses browser tokens for API access:

1. Open Slack in your browser and sign in
2. Open Developer Tools (F12) → Network tab
3. Find any API request to `api.slack.com`
4. Copy the `token` parameter (starts with `xoxc-`) → add as `slack.token`
5. Copy the `d` cookie value (starts with `xoxd-`) → add as `slack.cookie`

#### Linear

1. Go to Linear Settings → API → Personal API Keys
2. Create a new key with read access
3. Add the key as `linear.api_key`

#### GitHub

Option 1: Use the `gh` CLI (recommended - no config needed)
```bash
gh auth login
```

Option 2: Create a personal access token
1. Go to GitHub Settings → Developer settings → Personal access tokens
2. Create a token with `repo` scope
3. Add as `github.token`

Then add repos to index:
```toml
[github]
repos = ["org/repo1", "org/repo2"]
```

#### Notion

1. Go to [Notion Integrations](https://www.notion.so/my-integrations)
2. Create a new integration with read access
3. Add the token as `notion.token`
4. Share the pages you want indexed with your integration

#### Obsidian

No authentication needed - just set `vault_path`:

```toml
[obsidian]
vault_path = "~/Documents/Obsidian/Work"
include_folders = ["daily-notes", "meetings"]
```

## Usage

### Ingesting Data

Pull data from each source into the database:

```bash
# Ingest from specific sources
uv run ctx ingest slack
uv run ctx ingest linear
uv run ctx ingest github
uv run ctx ingest notion
uv run ctx ingest obsidian

# Incremental sync (only new content since last week)
uv run ctx ingest slack --since 7d

# Full re-index (clears existing data for that source)
uv run ctx ingest linear --full
```

Time formats for `--since`: `24h`, `7d`, `2w`, `30d`

### Searching

```bash
# Basic semantic search
uv run ctx search "authentication flow"

# Filter by source
uv run ctx search "bug fix" --source github --source linear

# Filter by time
uv run ctx search "meeting notes" --since 7d

# Filter by involvement
uv run ctx search "review" --involvement reviewer

# Keyword search (exact matching, no embeddings)
uv run ctx search "TODO" --keyword

# Different output formats
uv run ctx search "deploy" --format markdown  # Human-readable (default)
uv run ctx search "deploy" --format table     # Rich table
uv run ctx search "deploy" --format json      # For LLM consumption

# Limit results
uv run ctx search "api" --limit 20
```

### Other Commands

```bash
# View database statistics
uv run ctx info

# Retrieve a specific document by ID
uv run ctx get "slack:C123-1234567.123:0"
uv run ctx get "linear:ENG-123:0" --format json
```

## Document Format

Documents are stored with the ID format: `{source}:{source_id}:{chunk_index}`

Examples:
- `slack:C123-1234567.123:0` - Slack thread
- `linear:ENG-123:0` - Linear issue
- `github:org/repo/pr/42:1` - GitHub PR (chunk 2)
- `notion:abc123:0` - Notion page
- `obsidian:daily-notes/2024-01-15.md:0` - Obsidian note

## Architecture

```
src/ctx/
├── models.py      # Pydantic data models
├── config.py      # TOML configuration (~/.config/ctx/config.toml)
├── db.py          # ChromaDB connection and queries
├── chunking.py    # Token-aware text chunking
├── ingest/
│   ├── base.py    # BaseIngester abstract class
│   ├── slack.py   # Slack threads ingester
│   ├── linear.py  # Linear issues ingester
│   ├── github.py  # GitHub PRs ingester
│   ├── notion.py  # Notion pages ingester
│   └── obsidian.py # Obsidian notes ingester
└── cli/
    └── main.py    # Typer CLI commands
```

## Development

```bash
# Install dependencies
uv sync

# Run the CLI
uv run ctx --help

# Format code
uv run ruff format .

# Lint
uv run ruff check .
uv run ruff check --fix .

# Type check
uv run ty check

# Run tests
uv run pytest
```

### Adding a New Ingester

1. Create `src/ctx/ingest/{source}.py`
2. Subclass `BaseIngester`
3. Implement:
   - `source` property returning a `Source` enum value
   - `fetch_items(since)` to call the API and return raw items
   - `item_to_documents(item)` to convert items to `Document` objects
4. Add CLI command in `cli/main.py`

See `src/ctx/ingest/base.py` for the abstract interface and helper methods.

## Sandboxed Execution

For isolated search (e.g., in CI or untrusted environments), use the Docker sandbox:

```bash
# Build the container
./scripts/sandbox.sh build

# Start the container (mounts ChromaDB data read-only)
./scripts/sandbox.sh start

# Search in the sandbox
./scripts/sandbox.sh search "query"

# Stop and cleanup
./scripts/sandbox.sh stop
```

## License

MIT
