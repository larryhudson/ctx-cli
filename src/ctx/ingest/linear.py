"""Linear ingester using the GraphQL API."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import httpx
from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeRemainingColumn,
)

from ctx.config import get_config
from ctx.ingest.base import BaseIngester
from ctx.models import ContentType, Document, DocumentMetadata, Involvement, Source
from ctx.summarize import summarize_documents
from ctx.writer import delete_by_source, write_documents, write_index_files

logger = logging.getLogger(__name__)
console = Console()

LINEAR_API_URL = "https://api.linear.app/graphql"


class LinearIngester(BaseIngester):
    """Ingester for Linear issues the user is involved with."""

    def __init__(self) -> None:
        """Initialize the Linear ingester."""
        config = get_config()
        self._api_key = config.linear.api_key

        if not self._api_key:
            msg = "Linear API key not configured. Set linear.api_key in ~/.config/ctx/config.toml"
            raise ValueError(msg)

        self._client = httpx.Client(timeout=30.0)
        self._user_id: str | None = None
        self._user_email: str | None = None

    @property
    def source(self) -> Source:
        return Source.LINEAR

    def _graphql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        """Execute a GraphQL query against Linear's API."""
        headers: dict[str, str] = {
            "Authorization": self._api_key or "",
            "Content-Type": "application/json",
        }

        response = self._client.post(
            LINEAR_API_URL,
            headers=headers,
            json={"query": query, "variables": variables or {}},
        )
        response.raise_for_status()
        data: dict[str, Any] = response.json()

        if "errors" in data:
            errors = data["errors"]
            msg = f"Linear API error: {errors}"
            raise RuntimeError(msg)

        return data.get("data", {})

    def _get_current_user(self) -> tuple[str, str]:
        """Get the current user's ID and email."""
        if self._user_id is None:
            query = """
            query {
                viewer {
                    id
                    email
                }
            }
            """
            data = self._graphql(query)
            viewer = data.get("viewer", {})
            self._user_id = viewer.get("id", "")
            self._user_email = viewer.get("email", "")

        return self._user_id or "", self._user_email or ""

    def _fetch_issues_page(
        self,
        filter_obj: dict,
        after: str | None = None,
    ) -> tuple[list[dict], str | None, bool]:
        """Fetch a page of issues matching the filter.

        Returns:
            Tuple of (issues, end_cursor, has_next_page)
        """
        query = """
        query($filter: IssueFilter!, $after: String) {
            issues(filter: $filter, first: 50, after: $after) {
                nodes {
                    id
                    identifier
                    title
                    description
                    url
                    createdAt
                    updatedAt
                    priority
                    state {
                        name
                    }
                    team {
                        key
                        name
                    }
                    project {
                        name
                    }
                    labels {
                        nodes {
                            name
                        }
                    }
                    assignee {
                        id
                        email
                    }
                    creator {
                        id
                        email
                    }
                    comments {
                        nodes {
                            id
                            body
                            createdAt
                            user {
                                id
                                email
                                name
                            }
                        }
                    }
                }
                pageInfo {
                    endCursor
                    hasNextPage
                }
            }
        }
        """

        variables = {"filter": filter_obj}
        if after:
            variables["after"] = after

        data = self._graphql(query, variables)
        issues_data = data.get("issues", {})
        nodes = issues_data.get("nodes", [])
        page_info = issues_data.get("pageInfo", {})

        return nodes, page_info.get("endCursor"), page_info.get("hasNextPage", False)

    def _fetch_all_issues_with_filter(
        self,
        filter_obj: dict[str, Any],
        involvement: Involvement,
        seen_ids: set[str],
    ) -> list[dict[str, Any]]:
        """Fetch all issues matching a filter, handling pagination."""
        results: list[dict[str, Any]] = []
        cursor = None
        while True:
            issues, cursor, has_next = self._fetch_issues_page(filter_obj, cursor)
            for issue in issues:
                if issue["id"] not in seen_ids:
                    issue["_involvement"] = involvement
                    results.append(issue)
                    seen_ids.add(issue["id"])
            if not has_next:
                break
        return results

    def fetch_items(self, since: datetime | None = None) -> list[dict[str, Any]]:
        """Fetch Linear issues the user is involved with."""
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress:
            task = progress.add_task("Authenticating with Linear...", total=None)

            user_id, user_email = self._get_current_user()
            progress.update(task, description=f"Authenticated as {user_email}")

            all_issues: list[dict[str, Any]] = []
            seen_ids: set[str] = set()

            # Build date filter if provided
            date_filter: dict[str, Any] = {}
            if since:
                date_filter = {"updatedAt": {"gte": since.isoformat()}}

            # Define filters and their involvement types
            filters = [
                ({"assignee": {"id": {"eq": user_id}}, **date_filter}, Involvement.ASSIGNED),
                ({"creator": {"id": {"eq": user_id}}, **date_filter}, Involvement.AUTHOR),
                ({"subscribers": {"id": {"eq": user_id}}, **date_filter}, Involvement.PARTICIPANT),
            ]
            filter_names = ["assigned", "created", "subscribed"]

            for (filter_obj, involvement), name in zip(filters, filter_names, strict=True):
                progress.update(task, description=f"Fetching {name} issues...")
                issues = self._fetch_all_issues_with_filter(filter_obj, involvement, seen_ids)
                all_issues.extend(issues)
                progress.update(
                    task, description=f"Fetching {name} issues... ({len(all_issues)} total)"
                )

            progress.update(task, description=f"Found {len(all_issues)} issues")

        console.print(f"[green]Found {len(all_issues)} issues to process[/green]")
        return all_issues

    def _format_issue_content(self, issue: dict) -> str:
        """Format issue and comments into document content."""
        lines: list[str] = []

        # Issue header
        identifier = issue.get("identifier", "?")
        title = issue.get("title", "Untitled")
        lines.append(f"# {identifier}: {title}")
        lines.append("")

        # Description
        description = issue.get("description")
        if description:
            lines.append(description)
            lines.append("")

        # Comments
        comments = issue.get("comments", {}).get("nodes", [])
        if comments:
            lines.append("## Comments")
            lines.append("")

            for comment in comments:
                user = comment.get("user") or {}
                author = user.get("name") or user.get("email") or "Unknown"
                body = comment.get("body", "")
                created_at = comment.get("createdAt", "")
                created = created_at[:10] if created_at else ""
                lines.append(f"**{author}** ({created}):")
                lines.append(body)
                lines.append("")

        return "\n".join(lines)

    def item_to_documents(self, item: dict) -> list[Document]:
        """Convert a Linear issue to documents."""
        issue_id = item["id"]
        identifier = item.get("identifier", issue_id)

        # Parse timestamp
        updated_at = item.get("updatedAt") or item.get("createdAt")
        if updated_at:
            dt = datetime.fromisoformat(updated_at)
            timestamp = int(dt.timestamp())
        else:
            timestamp = int(datetime.now(tz=UTC).timestamp())

        # Get metadata fields
        state = item.get("state", {})
        team = item.get("team", {})
        project = item.get("project")
        labels = item.get("labels", {}).get("nodes", [])
        creator = item.get("creator", {})

        # Determine author
        author_email = creator.get("email") if creator else None

        # Get involvement (set during fetch_items)
        involvement = item.get("_involvement", Involvement.PARTICIPANT)

        # Format labels as comma-separated string
        label_names = [label.get("name") for label in labels if label.get("name")]
        labels_str = ",".join(label_names) if label_names else None

        metadata = DocumentMetadata(
            source=Source.LINEAR,
            source_id=identifier,
            timestamp=timestamp,
            content_type=ContentType.TICKET,
            author=author_email,
            my_involvement=involvement,
            permalink=item.get("url"),
            linear_team=team.get("key"),
            linear_status=state.get("name"),
            linear_priority=item.get("priority"),
            linear_project=project.get("name") if project else None,
            linear_labels=labels_str,
        )

        content = self._format_issue_content(item)

        return self.create_documents_from_content(
            source_id=identifier,
            content=content,
            metadata=metadata,
        )

    def ingest(
        self,
        since: datetime | None = None,
        full_reindex: bool = False,
    ) -> int:
        """Run the ingestion process with progress display."""
        if full_reindex:
            console.print("[yellow]Full reindex: deleting existing Linear documents...[/yellow]")
            delete_by_source(self.source)

        # Fetch items
        items = self.fetch_items(since=since)

        if not items:
            console.print("[yellow]No issues found to ingest[/yellow]")
            return 0

        # Convert to documents with progress bar
        all_documents: list[Document] = []

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TimeRemainingColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("Processing issues", total=len(items))

            for item in items:
                try:
                    documents = self.item_to_documents(item)
                    all_documents.extend(documents)
                except Exception:
                    logger.exception("Failed to convert issue to document")
                finally:
                    progress.advance(task)

        if not all_documents:
            console.print("[yellow]No documents created[/yellow]")
            return 0

        # Summarize and write to markdown files
        console.print(f"[blue]Summarizing {len(all_documents)} documents...[/blue]")
        summarize_documents(all_documents)
        console.print(f"[blue]Writing {len(all_documents)} documents...[/blue]")
        count = write_documents(all_documents)
        write_index_files(all_documents)
        console.print(f"[green]Successfully ingested {count} documents[/green]")

        return count
