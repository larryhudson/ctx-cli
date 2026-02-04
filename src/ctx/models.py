"""Pydantic models for documents and metadata."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum, StrEnum
from typing import Literal

from pydantic import BaseModel, Field, computed_field


class Source(StrEnum):
    """Data source types."""

    SLACK = "slack"
    LINEAR = "linear"
    GITHUB = "github"
    NOTION = "notion"
    OBSIDIAN = "obsidian"


class ContentType(StrEnum):
    """Content type classification."""

    MESSAGE = "message"
    TICKET = "ticket"
    PR = "pr"
    DOC = "doc"
    NOTE = "note"


class Involvement(StrEnum):
    """How the user is involved with this content."""

    AUTHOR = "author"
    PARTICIPANT = "participant"
    MENTIONED = "mentioned"
    ASSIGNED = "assigned"
    REVIEWER = "reviewer"


class DocumentMetadata(BaseModel):
    """Base metadata that all documents share.

    ChromaDB only supports flat values (str, int, float, bool),
    so we keep everything at the top level.
    """

    # Required fields
    source: Source
    source_id: str
    timestamp: int = Field(description="Unix timestamp (seconds) of content creation/update")
    indexed_at: int = Field(
        default_factory=lambda: int(datetime.now(tz=UTC).timestamp()),
        description="Unix timestamp when we ingested this",
    )

    # Content classification
    content_type: ContentType

    # Involvement
    author: str | None = None
    my_involvement: Involvement | None = None

    # Common fields
    permalink: str | None = Field(default=None, description="URL to view the item externally")

    # Slack-specific
    slack_channel: str | None = None
    slack_channel_id: str | None = None
    slack_thread_ts: str | None = None
    slack_reply_count: int | None = Field(default=None, description="Number of replies in thread")

    # Linear-specific
    linear_team: str | None = None
    linear_status: str | None = None
    linear_priority: int | None = Field(default=None, ge=0, le=4)
    linear_project: str | None = None
    linear_labels: str | None = Field(default=None, description="Comma-separated label names")

    # GitHub-specific
    github_repo: str | None = None
    github_state: Literal["open", "closed", "merged"] | None = None
    github_pr_number: int | None = None

    # Notion-specific
    notion_workspace: str | None = None
    notion_page_id: str | None = None
    notion_parent: str | None = None

    # Obsidian-specific
    obsidian_path: str | None = None
    obsidian_folder: str | None = None
    obsidian_tags: str | None = Field(default=None, description="Comma-separated tags")

    def to_chroma_metadata(self) -> dict[str, str | int | float | bool]:
        """Convert to ChromaDB-compatible metadata dict.

        Excludes None values and converts enums to strings.
        """
        result: dict[str, str | int | float | bool] = {}
        for key, value in self.model_dump().items():
            if value is None:
                continue
            if isinstance(value, Enum):
                result[key] = value.value
            else:
                result[key] = value
        return result


class Document(BaseModel):
    """A document to be indexed in ChromaDB."""

    id: str = Field(description="Document ID in format {source}:{source_id}:{chunk_index}")
    content: str = Field(description="The text content to embed and search")
    metadata: DocumentMetadata

    @computed_field
    @property
    def chunk_index(self) -> int:
        """Extract chunk index from document ID."""
        return int(self.id.rsplit(":", 1)[-1])


class DocumentChunk(BaseModel):
    """A chunk of a document with its index."""

    content: str
    chunk_index: int


def make_document_id(source: Source, source_id: str, chunk_index: int = 0) -> str:
    """Create a document ID in the standard format."""
    return f"{source.value}:{source_id}:{chunk_index}"
