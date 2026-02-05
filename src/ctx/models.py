"""Pydantic models for documents and metadata."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


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
    """Base metadata that all documents share."""

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
    summary: str | None = Field(default=None, description="One-line summary")

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


class Document(BaseModel):
    """A document to be written as a markdown file."""

    id: str = Field(description="Document ID in format {source}:{source_id}")
    content: str = Field(description="The text content")
    metadata: DocumentMetadata


def make_document_id(source: Source, source_id: str) -> str:
    """Create a document ID in the standard format."""
    return f"{source.value}:{source_id}"
