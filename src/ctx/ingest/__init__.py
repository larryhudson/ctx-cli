"""Ingest modules for various data sources."""

from ctx.ingest.base import BaseIngester
from ctx.ingest.slack import SlackIngester

__all__ = ["BaseIngester", "SlackIngester"]
