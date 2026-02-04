# ctx - Specification

A CLI tool to aggregate and search across work-related data sources (Slack, Linear, GitHub, Notion, Obsidian) using ChromaDB for semantic and keyword search.

## Goals

1. **Unified search** - Query across all work context sources with a single interface
2. **Semantic + keyword search** - Find relevant content by meaning or exact terms
3. **Faceted filtering** - Filter by source, date, involvement level, status, etc.
4. **Portable database** - Ingest on one machine, search from another (e.g., sandboxed environment)
5. **LLM-ready context** - Provide relevant context to AI agents

## Architecture

```
┌─────────────────────┐         ┌─────────────────────┐
│   Ingest Machine    │         │   Search Machine    │
│                     │         │   (e.g., sandbox)   │
│  ┌───────────────┐  │         │                     │
│  │ Slack API     │  │         │  ┌───────────────┐  │
│  │ Linear API    │  │  sync   │  │   search CLI  │  │
│  │ GitHub API    │──┼────────►│  │               │  │
│  │ Notion API    │  │         │  │  chroma_data/ │  │
│  │ Obsidian dir  │  │         │  └───────────────┘  │
│  └───────┬───────┘  │         │                     │
│          │          │         └─────────────────────┘
│          ▼          │
│  ┌───────────────┐  │
│  │  chroma_data/ │  │
│  └───────────────┘  │
└─────────────────────┘
```

The `chroma_data/` directory is synced between machines (rsync, Dropbox, git-lfs, etc.).

## Data Sources

### Slack
- **What**: Threads I've participated in (posted, replied, or mentioned)
- **Content**: Message text with thread context
- **Metadata**: Channel, thread timestamp, author

### Linear
- **What**: Tickets assigned to me
- **Content**: Title + description
- **Metadata**: Status, priority, project, labels

### GitHub
- **What**: PRs I authored or reviewed
- **Content**: Title + description + review comments
- **Metadata**: Repo, state (open/closed/merged), PR number

### Notion
- **What**: Docs I've created or been shared on
- **Content**: Page content as markdown
- **Metadata**: Workspace, parent page

### Obsidian
- **What**: Daily work notes and other markdown files
- **Content**: Note content as markdown
- **Metadata**: Folder path, tags (extracted from frontmatter/inline)

## ChromaDB Schema

### Collection

Single collection named `work_context` to enable cross-source search.

### Document ID Format

```
{source}:{source_id}:{chunk_index}
```

Examples:
- `slack:C123ABC-1234567890.123456:0`
- `linear:TEAM-123:0`
- `github:owner/repo/pr/42:0`
- `github:owner/repo/pr/42:1` (chunk 1 of PR description)
- `obsidian:daily-notes/2024-01-15:0`

### Metadata Schema

All fields are optional unless marked required. ChromaDB only supports flat values (str, int, float, bool).

```python
{
    # === Required (all sources) ===
    "source": str,           # "slack" | "linear" | "github" | "notion" | "obsidian"
    "source_id": str,        # Original ID from source system
    "timestamp": int,        # Unix timestamp (seconds) of content creation/update
    "indexed_at": int,       # Unix timestamp when we ingested this

    # === Content classification ===
    "content_type": str,     # "message" | "ticket" | "pr" | "doc" | "note"

    # === Involvement ===
    "author": str,           # Primary author username/email
    "my_involvement": str,   # "author" | "participant" | "mentioned" | "assigned" | "reviewer"

    # === Slack-specific ===
    "slack_channel": str,    # Channel name (without #)
    "slack_channel_id": str, # Channel ID
    "slack_thread_ts": str,  # Thread timestamp

    # === Linear-specific ===
    "linear_team": str,      # Team key (e.g., "ENG")
    "linear_status": str,    # Status name (e.g., "In Progress")
    "linear_priority": int,  # 0=none, 1=urgent, 2=high, 3=medium, 4=low
    "linear_project": str,   # Project name
    "linear_labels": str,    # Comma-separated label names

    # === GitHub-specific ===
    "github_repo": str,      # "owner/repo"
    "github_state": str,     # "open" | "closed" | "merged"
    "github_pr_number": int, # PR number

    # === Notion-specific ===
    "notion_workspace": str, # Workspace name
    "notion_page_id": str,   # Page ID
    "notion_parent": str,    # Parent page title

    # === Obsidian-specific ===
    "obsidian_path": str,    # Relative file path
    "obsidian_folder": str,  # Folder name
    "obsidian_tags": str,    # Comma-separated tags
}
```

### Document Content

The `document` field contains plain text for embedding and full-text search:

| Source   | Document Content                                    |
|----------|-----------------------------------------------------|
| Slack    | Message text (replies include parent context)       |
| Linear   | `{title}\n\n{description}`                          |
| GitHub   | `{title}\n\n{body}` (PRs), comment text (comments)  |
| Notion   | Page content converted to markdown                  |
| Obsidian | Raw markdown content                                |

### Chunking

For documents exceeding ~1000 tokens:
- Split into chunks of ~500 tokens with ~50 token overlap
- Each chunk gets identical metadata
- Chunks distinguished by `chunk_index` in document ID (`:0`, `:1`, etc.)

## CLI Interface

### Ingest Commands

```bash
# Ingest all sources
ctx ingest

# Ingest specific source
ctx ingest slack
ctx ingest linear
ctx ingest github
ctx ingest notion
ctx ingest obsidian

# Options
ctx ingest --since 7d          # Only last 7 days
ctx ingest --full              # Full re-index (clear existing)
```

### Search Commands

```bash
# Semantic search
ctx search "deployment issues with auth"

# With filters
ctx search "bug" --source linear --source github
ctx search "meeting notes" --source obsidian --since 7d
ctx search "PR review" --involvement reviewer

# Keyword search (full-text, no embeddings)
ctx search "error 500" --keyword

# Output formats
ctx search "auth" --format json    # For LLM consumption
ctx search "auth" --format table   # Human-readable (default)
ctx search "auth" --limit 20       # Number of results
```

### Info Commands

```bash
# Show database stats
ctx info

# Output:
# Collection: work_context
# Total documents: 1,234
# By source:
#   slack: 456
#   linear: 123
#   github: 234
#   notion: 89
#   obsidian: 332
# Last indexed: 2024-01-15 10:30:00
```

## Project Structure

```
src/ctx/
├── __init__.py
├── config.py              # Settings, API keys, paths
├── db.py                  # ChromaDB connection and query helpers
├── models.py              # Pydantic models for documents and metadata
├── chunking.py            # Text chunking utilities
├── ingest/
│   ├── __init__.py
│   ├── base.py            # Base ingester class
│   ├── slack.py
│   ├── linear.py
│   ├── github.py
│   ├── notion.py
│   └── obsidian.py
└── cli/
    ├── __init__.py
    ├── main.py            # CLI entry point (click/typer)
    ├── search.py          # Search commands
    └── ingest.py          # Ingest commands
```

## Configuration

Configuration via environment variables and/or `~/.config/ctx/config.toml`:

```toml
[database]
path = "~/.local/share/ctx/chroma_data"

[embedding]
# "default" uses Chroma's default (all-MiniLM-L6-v2)
# "openai" uses text-embedding-3-small
model = "default"

[slack]
# OAuth token with channels:history, users:read scopes
token = "xoxb-..."
# Only index channels matching these patterns
channels = ["engineering-*", "team-*"]

[linear]
api_key = "lin_api_..."

[github]
# Uses gh CLI auth by default, or set token
token = "ghp_..."
# Repos to index PRs from
repos = ["org/repo1", "org/repo2"]

[notion]
token = "secret_..."
# Root pages to crawl
root_pages = ["Work Notes", "Projects"]

[obsidian]
vault_path = "~/Documents/Obsidian/Work"
# Folders to index
include_folders = ["daily-notes", "meetings", "projects"]
```

## Embedding Model

Default: ChromaDB's built-in `all-MiniLM-L6-v2` (384 dimensions, runs locally)

Optional: OpenAI `text-embedding-3-small` for better quality (requires API key)

**Important**: Once a collection is created with an embedding model, all future documents must use the same model. Switching models requires re-indexing.

## Example Queries

### Find recent Slack discussions about deployments

```python
collection.query(
    query_texts=["deployment issues"],
    n_results=10,
    where={
        "$and": [
            {"source": "slack"},
            {"timestamp": {"$gte": now - 7*86400}}
        ]
    }
)
```

### Find open Linear tickets I'm assigned to

```python
collection.query(
    query_texts=["authentication"],
    where={
        "$and": [
            {"source": "linear"},
            {"my_involvement": "assigned"},
            {"linear_status": {"$nin": ["Done", "Cancelled"]}}
        ]
    }
)
```

### Cross-source search for a topic

```python
collection.query(
    query_texts=["rate limiting API"],
    n_results=20,
    where={"source": {"$in": ["slack", "linear", "github"]}}
)
```

### Full-text keyword search

```python
collection.get(
    where={"timestamp": {"$gte": now - 30*86400}},
    where_document={"$contains": "ERROR_CODE_429"}
)
```

## Future Considerations

- **Incremental sync**: Track last sync time per source, only fetch new/updated items
- **Deduplication**: Same content referenced in multiple places (e.g., Linear ticket linked in Slack)
- **Entity extraction**: Extract and index mentioned people, projects, tickets
- **MCP server**: Expose search as an MCP tool for Claude Code integration
- **Web UI**: Optional browser interface for search
