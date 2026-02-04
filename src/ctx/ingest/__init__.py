"""Ingest modules for various data sources."""

from ctx.ingest.base import BaseIngester
from ctx.ingest.obsidian import ObsidianIngester
from ctx.ingest.slack import SlackIngester

__all__ = ["BaseIngester", "ObsidianIngester", "SlackIngester"]
