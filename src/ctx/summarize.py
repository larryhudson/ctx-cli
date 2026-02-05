"""LLM-based document summarization using PydanticAI."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from pathlib import Path

from pydantic_ai import Agent

from ctx.config import get_config
from ctx.models import Document

logger = logging.getLogger(__name__)

# Summary length constraints
MAX_SUMMARY_LENGTH = 120
_TRUNCATED_LENGTH = 117  # MAX_SUMMARY_LENGTH - len("...")

# Cache file location
_CACHE_FILENAME = "summary_cache.json"


def _get_cache_path() -> Path:
    """Get path to the summary cache file."""
    config = get_config()
    return config.output.path.parent / _CACHE_FILENAME


def _load_cache() -> dict[str, str]:
    """Load the summary cache from disk."""
    path = _get_cache_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.warning("Failed to load summary cache, starting fresh")
        return {}


def _save_cache(cache: dict[str, str]) -> None:
    """Save the summary cache to disk."""
    path = _get_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache), encoding="utf-8")


def _content_hash(content: str) -> str:
    """Compute a SHA-256 hash of content for cache keying."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _short_summary(content: str) -> str:
    """Extract a short summary from the first line of content."""
    first_line = content.split("\n", maxsplit=1)[0].strip()
    # Strip leading markdown heading markers
    first_line = first_line.lstrip("#").strip()
    if len(first_line) > MAX_SUMMARY_LENGTH:
        return first_line[:_TRUNCATED_LENGTH] + "..."
    return first_line


def summarize_documents(documents: list[Document]) -> None:
    """Add a one-line summary to each document's metadata.

    Uses LLM summarization if enabled and API key is available,
    otherwise falls back to first-line extraction. Caches results
    by content hash so identical content is never summarized twice.
    """
    config = get_config()
    summary_config = config.summary

    cache = _load_cache()
    cache_dirty = False

    # Inject API key from config into env so PydanticAI can find it
    if summary_config.gemini_api_key and not os.environ.get("GEMINI_API_KEY"):
        os.environ["GEMINI_API_KEY"] = summary_config.gemini_api_key

    # Determine if LLM summarization is available
    use_llm = summary_config.enabled and bool(
        summary_config.gemini_api_key or os.environ.get("GEMINI_API_KEY")
    )

    if use_llm:
        # Separate docs into cached, short (no LLM needed), and needs-LLM
        to_summarize: list[tuple[Document, str]] = []  # (doc, hash)
        for doc in documents:
            h = _content_hash(doc.content)
            if h in cache:
                doc.metadata.summary = cache[h]
            elif len(doc.content) < summary_config.min_length:
                summary = _short_summary(doc.content)
                doc.metadata.summary = summary
                cache[h] = summary
                cache_dirty = True
            else:
                to_summarize.append((doc, h))

        if to_summarize:
            results = asyncio.run(_summarize_batch(to_summarize, summary_config.model, cache))
            # results is the updated cache with new entries
            cache = results
            cache_dirty = True
    else:
        for doc in documents:
            h = _content_hash(doc.content)
            if h in cache:
                doc.metadata.summary = cache[h]
            else:
                summary = _short_summary(doc.content)
                doc.metadata.summary = summary
                cache[h] = summary
                cache_dirty = True

    if cache_dirty:
        _save_cache(cache)


async def _summarize_batch(
    items: list[tuple[Document, str]],
    model_name: str,
    cache: dict[str, str],
) -> dict[str, str]:
    """Summarize a batch of documents using PydanticAI, updating cache in place."""
    agent = Agent(
        model_name,
        system_prompt=(
            "You are a summarizer. Given a work document (Slack thread, Linear issue, "
            "GitHub PR, Notion page, or Obsidian note), produce a single concise sentence "
            "(max 120 chars) summarizing the key topic or action. "
            "No markdown, no quotes, no prefix. Just the summary."
        ),
        output_type=str,
    )

    semaphore = asyncio.Semaphore(10)

    async def _summarize_one(doc: Document, content_hash: str) -> None:
        async with semaphore:
            try:
                result = await agent.run(doc.content)
                summary = result.output.strip()
                if len(summary) > MAX_SUMMARY_LENGTH:
                    summary = summary[:_TRUNCATED_LENGTH] + "..."
                doc.metadata.summary = summary
                cache[content_hash] = summary
            except Exception:
                logger.warning("LLM summarization failed for %s, using fallback", doc.id)
                summary = _short_summary(doc.content)
                doc.metadata.summary = summary
                cache[content_hash] = summary

    await asyncio.gather(*[_summarize_one(doc, h) for doc, h in items])
    return cache
