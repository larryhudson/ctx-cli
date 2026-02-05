# ctx

A CLI tool that aggregates work context from multiple sources into structured markdown files.

## Overview

**ctx** pulls data from Slack, Linear, GitHub, Notion, and Obsidian and writes them as markdown files with YAML frontmatter, organized by date. Each day gets an index file with one-line summaries, making it easy for AI agents and humans to browse work context.

This is particularly useful for:
- AI agents that need relevant context from your work history
- Searching across all your work tools with a single query
- Building a personal knowledge base from your day-to-day work

## Features

- **Markdown output** - Each document stored as a markdown file with YAML frontmatter
- **Date-organized** - Files grouped into `YYYY/MM/DD_dayname/` directories
- **Day indexes** - Auto-generated `_index.md` per day with one-line summaries
- **LLM summarization** - Optional one-line summaries via PydanticAI (Gemini by default)
- **Incremental sync** - Only fetch new content since last ingestion
- **Multiple sources** - Slack, Linear, GitHub, Notion, Obsidian

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
[output]
path = "~/.local/share/ctx/documents"  # Where markdown files are written

[summary]
enabled = false                        # Enable LLM-generated summaries
model = "google-gla:gemini-2.5-flash"  # PydanticAI model identifier
# gemini_api_key = "..."               # Or set GEMINI_API_KEY env var
min_length = 500                       # Docs shorter than this use first-line extraction

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

Pull data from each source into markdown files:

```bash
# Ingest from specific sources
uv run ctx ingest slack
uv run ctx ingest linear
uv run ctx ingest github
uv run ctx ingest notion
uv run ctx ingest obsidian

# Incremental sync (only new content since last week)
uv run ctx ingest slack --since 7d

# Full re-index (clears existing files for that source)
uv run ctx ingest linear --full
```

Time formats for `--since`: `24h`, `7d`, `2w`, `30d`

### Other Commands

```bash
# View sync status
uv run ctx info
```

## Document Format

Each document is a markdown file with YAML frontmatter, stored in a date-organized directory:

```
~/.local/share/ctx/documents/
└── 2026/
    └── 02/
        └── 05_wednesday/
            ├── _index.md                                    # Day summary
            ├── 2026-02-05T09-30-00_slack_C123-1234567.md
            ├── 2026-02-05T10-00-00_linear_ENG-123.md
            └── 2026-02-05T14-00-00_github_org-repo-pr-42.md
```

Document IDs use the format `{source}:{source_id}`:
- `slack:C123-1234567.123` - Slack thread
- `linear:ENG-123` - Linear issue
- `github:org/repo/pr/42` - GitHub PR
- `notion:abc123` - Notion page
- `obsidian:daily-notes/2024-01-15.md` - Obsidian note

## Architecture

```
src/ctx/
├── models.py      # Pydantic data models
├── config.py      # TOML configuration (~/.config/ctx/config.toml)
├── writer.py      # Markdown file writer (date-organized output)
├── summarize.py   # LLM summarization via PydanticAI
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

## License

MIT
