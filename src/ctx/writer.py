"""Markdown file writer for documents."""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path

import yaml

from ctx.config import get_config
from ctx.models import Document, Source

logger = logging.getLogger(__name__)


def _sanitize_for_filename(s: str) -> str:
    """Replace characters unsafe for filenames with hyphens."""
    return re.sub(r"[/:]+", "-", s)


def _day_dir(ts: datetime, base_dir: Path) -> Path:
    """Build the day directory path, e.g. base/2026/02/05_wednesday."""
    day_name = ts.strftime("%A").lower()
    return base_dir / f"{ts:%Y}" / f"{ts:%m}" / f"{ts:%d}_{day_name}"


def _document_path(doc: Document, base_dir: Path) -> Path:
    """Derive the output path for a document."""
    ts = datetime.fromtimestamp(doc.metadata.timestamp, tz=UTC)
    date_dir = _day_dir(ts, base_dir)
    safe_id = _sanitize_for_filename(doc.metadata.source_id)
    filename = f"{ts:%Y-%m-%dT%H-%M-%S}_{doc.metadata.source.value}_{safe_id}.md"
    return date_dir / filename


def _frontmatter(doc: Document) -> str:
    """Build YAML frontmatter string from document metadata."""
    data: dict[str, str | int | float | bool] = {}
    for key, value in doc.metadata.model_dump().items():
        if value is None:
            continue
        if isinstance(value, Enum):
            data[key] = value.value
        else:
            data[key] = value
    return yaml.dump(data, default_flow_style=False, sort_keys=False)


def write_documents(documents: list[Document]) -> int:
    """Write documents as markdown files with YAML frontmatter.

    Returns the number of files written.
    """
    config = get_config()
    base_dir = config.output.path

    count = 0
    for doc in documents:
        path = _document_path(doc, base_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        content = f"---\n{_frontmatter(doc)}---\n\n{doc.content}\n"
        path.write_text(content, encoding="utf-8")
        count += 1

    return count


_MAX_IDENTIFIER_LENGTH = 60


def _parse_frontmatter_from_file(path: Path) -> dict[str, str | int | float | bool] | None:
    """Parse YAML frontmatter from a markdown file on disk."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---\n", 4)
    if end == -1:
        return None
    try:
        return yaml.safe_load(text[4:end])
    except yaml.YAMLError:
        return None


def _index_line_from_frontmatter(
    meta: dict[str, str | int | float | bool], content: str
) -> str | None:
    """Build an index line from parsed frontmatter."""
    timestamp = meta.get("timestamp")
    source_str = meta.get("source")
    if not isinstance(timestamp, int) or not isinstance(source_str, str):
        return None

    ts = datetime.fromtimestamp(timestamp, tz=UTC)
    time_str = ts.strftime("%H:%M")

    # Build identifier based on source
    source_id = str(meta.get("source_id", ""))
    match source_str:
        case "slack":
            channel = meta.get("slack_channel")
            identifier = f"#{channel}" if channel else source_id
        case "linear":
            identifier = source_id
        case "github":
            repo = meta.get("github_repo")
            pr_num = meta.get("github_pr_number")
            identifier = f"{repo}#{pr_num}" if repo and pr_num is not None else source_id
        case "notion":
            # Extract first heading from content
            identifier = source_id
            for raw_line in content.split("\n"):
                stripped = raw_line.strip()
                if stripped.startswith("#"):
                    title = stripped.lstrip("#").strip()
                    identifier = (
                        title[:_MAX_IDENTIFIER_LENGTH]
                        if len(title) > _MAX_IDENTIFIER_LENGTH
                        else title
                    )
                    break
        case "obsidian":
            identifier = str(meta.get("obsidian_path", source_id))
        case _:
            identifier = source_id

    summary = str(meta.get("summary", ""))
    return f"- {time_str} {source_str} {identifier} - {summary}"


def write_index_files(documents: list[Document]) -> int:
    """Write _index.md files for each day directory that was touched.

    Scans all .md files on disk in each affected day directory so the index
    includes documents from every source, not just the current ingester.

    Returns the number of index files written.
    """
    config = get_config()
    base_dir = config.output.path

    # Determine which day directories were affected
    affected_dirs: set[Path] = set()
    for doc in documents:
        ts = datetime.fromtimestamp(doc.metadata.timestamp, tz=UTC)
        affected_dirs.add(_day_dir(ts, base_dir))

    count = 0
    for day_dir in sorted(affected_dirs):
        if not day_dir.exists():
            continue

        # Scan all .md files in this day directory (exclude _index.md)
        entries: list[tuple[int, str]] = []  # (timestamp, formatted line)
        for md_file in sorted(day_dir.glob("*.md")):
            if md_file.name == "_index.md":
                continue
            meta = _parse_frontmatter_from_file(md_file)
            if meta is None:
                continue
            # Read content for identifier extraction (notion headings etc)
            content = md_file.read_text(encoding="utf-8")
            line = _index_line_from_frontmatter(meta, content)
            if line is not None:
                entries.append((int(meta.get("timestamp", 0)), line))

        if not entries:
            continue

        # Sort by timestamp
        entries.sort(key=lambda e: e[0])

        # Build date heading from directory name (e.g. "05_wednesday")
        day_part = day_dir.name.split("_", maxsplit=1)
        day_name = day_part[1].capitalize() if len(day_part) > 1 else ""
        date_str = f"{day_dir.parent.parent.name}-{day_dir.parent.name}-{day_part[0]}"
        heading = f"# {day_name} {date_str}" if day_name else f"# {date_str}"
        lines = [heading, ""]
        lines.extend(line for _, line in entries)

        index_path = day_dir / "_index.md"
        index_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        count += 1

    return count


def delete_by_source(source: Source) -> int:
    """Delete all markdown files for a given source.

    Returns the number of files deleted.
    """
    config = get_config()
    base_dir = config.output.path

    if not base_dir.exists():
        return 0

    count = 0
    for path in base_dir.rglob(f"*_{source.value}_*.md"):
        path.unlink()
        count += 1

    return count
