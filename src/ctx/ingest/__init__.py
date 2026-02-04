"""Ingest modules for various data sources."""

from ctx.ingest.base import BaseIngester
from ctx.ingest.github import GitHubIngester
from ctx.ingest.linear import LinearIngester
from ctx.ingest.notion import NotionIngester
from ctx.ingest.obsidian import ObsidianIngester
from ctx.ingest.slack import SlackIngester

__all__ = [
    "BaseIngester",
    "GitHubIngester",
    "LinearIngester",
    "NotionIngester",
    "ObsidianIngester",
    "SlackIngester",
]
