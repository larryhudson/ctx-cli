"""GitHub ingester for PRs authored or reviewed by the user."""

from __future__ import annotations

import logging
import subprocess
import time
from datetime import UTC, datetime
from typing import Any, Literal

import httpx
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

from ctx.config import get_config
from ctx.ingest.base import BaseIngester
from ctx.models import ContentType, Document, DocumentMetadata, Involvement, Source

logger = logging.getLogger(__name__)
console = Console()

# GitHub API URLs
GITHUB_GRAPHQL_URL = "https://api.github.com/graphql"
GITHUB_REST_URL = "https://api.github.com"

# Rate limiting
RATE_LIMIT_DELAY = 0.05
MAX_RETRIES = 3
HTTP_OK = 200

# Type alias for PR state
PRState = Literal["open", "closed", "merged"]

# GraphQL query to fetch PR details + review comments in one request
PR_QUERY = """
query($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      title
      body
      state
      merged
      number
      url
      updatedAt
      author {
        login
      }
      reviews(first: 100) {
        nodes {
          author {
            login
          }
          body
          createdAt
          comments(first: 50) {
            nodes {
              author {
                login
              }
              body
              path
              createdAt
            }
          }
        }
      }
    }
  }
}
"""


class GitHubIngester(BaseIngester):
    """Ingester for GitHub PRs the user has authored or reviewed."""

    def __init__(self) -> None:
        """Initialize the GitHub ingester."""
        config = get_config()

        # Try config first, then gh CLI
        token = config.github.token
        if not token:
            token = self._get_gh_cli_token()

        if not token:
            msg = (
                "GitHub token not configured. Set github.token in ~/.config/ctx/config.toml "
                "or authenticate with `gh auth login`"
            )
            raise ValueError(msg)

        self._token = token
        self._client = httpx.Client(timeout=30.0)
        self._username: str | None = None
        self._last_api_call: float = 0

        # Get configured repos (optional filtering)
        self._repos = config.github.repos

    def _get_gh_cli_token(self) -> str | None:
        """Try to get GitHub token from gh CLI."""
        try:
            result = subprocess.run(
                ["gh", "auth", "token"],
                capture_output=True,
                text=True,
                check=True,
            )
            return result.stdout.strip() or None
        except (subprocess.CalledProcessError, FileNotFoundError):
            return None

    @property
    def source(self) -> Source:
        return Source.GITHUB

    def _rate_limit(self) -> None:
        """Enforce rate limiting between API calls."""
        elapsed = time.time() - self._last_api_call
        if elapsed < RATE_LIMIT_DELAY:
            time.sleep(RATE_LIMIT_DELAY - elapsed)
        self._last_api_call = time.time()

    def _graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        """Execute a GraphQL query."""
        self._rate_limit()

        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

        for attempt in range(MAX_RETRIES):
            response = self._client.post(
                GITHUB_GRAPHQL_URL,
                headers=headers,
                json={"query": query, "variables": variables},
            )

            if response.status_code == HTTP_OK:
                data = response.json()
                if "errors" in data:
                    logger.warning("GraphQL errors: %s", data["errors"])
                return data.get("data", {})

            if response.status_code in (429, 403):
                delay = 2 ** (attempt + 1)
                console.print(f"[yellow]Rate limited, waiting {delay}s...[/yellow]")
                time.sleep(delay)
                continue

            response.raise_for_status()

        msg = f"GraphQL request failed after {MAX_RETRIES} retries"
        raise RuntimeError(msg)

    def _rest_api(
        self, endpoint: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any] | list[Any]:
        """Make a REST API call."""
        self._rate_limit()

        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
        }

        response = self._client.get(
            f"{GITHUB_REST_URL}/{endpoint.lstrip('/')}",
            headers=headers,
            params=params,
        )
        response.raise_for_status()
        return response.json()

    def _get_current_username(self) -> str:
        """Get the current authenticated user's username."""
        if self._username is None:
            data = self._rest_api("/user")
            if isinstance(data, dict):
                self._username = data["login"]
        return self._username or "unknown"

    def _search_prs(self, query: str, since: datetime | None = None) -> list[dict[str, Any]]:
        """Search for PRs using REST API (search doesn't work well in GraphQL)."""
        if since:
            query += f" updated:>={since.strftime('%Y-%m-%d')}"

        all_results: list[dict[str, Any]] = []
        page = 1
        max_pages = 10

        while page <= max_pages:
            self._rate_limit()

            params = {
                "q": query,
                "sort": "updated",
                "order": "desc",
                "per_page": 100,
                "page": page,
            }

            data = self._rest_api("/search/issues", params=params)
            if not isinstance(data, dict):
                break

            items = data.get("items", [])
            all_results.extend(items)

            total = data.get("total_count", 0)
            if len(all_results) >= total or not items:
                break

            page += 1

        return all_results

    def fetch_items(self, since: datetime | None = None) -> list[dict]:
        """Fetch GitHub PRs the user has authored or reviewed."""
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress:
            task = progress.add_task("Authenticating with GitHub...", total=None)

            username = self._get_current_username()
            progress.update(task, description=f"Authenticated as {username}")

            prs: list[dict] = []
            seen_urls: set[str] = set()

            # Build repo filter
            repo_filter = ""
            if self._repos:
                repo_filter = " " + " ".join(f"repo:{repo}" for repo in self._repos)

            # Search for authored PRs
            progress.update(task, description="Searching for authored PRs...")
            author_query = f"is:pr author:{username}{repo_filter}"
            for pr in self._search_prs(author_query, since):
                if pr["html_url"] not in seen_urls:
                    seen_urls.add(pr["html_url"])
                    prs.append({"search_result": pr, "involvement": Involvement.AUTHOR})

            progress.update(task, description=f"Found {len(prs)} authored, searching reviewed...")

            # Search for reviewed PRs
            reviewer_query = f"is:pr reviewed-by:{username}{repo_filter}"
            for pr in self._search_prs(reviewer_query, since):
                if pr["html_url"] not in seen_urls:
                    seen_urls.add(pr["html_url"])
                    prs.append({"search_result": pr, "involvement": Involvement.REVIEWER})

            # Search for review-requested PRs
            requested_query = f"is:pr review-requested:{username}{repo_filter}"
            for pr in self._search_prs(requested_query, since):
                if pr["html_url"] not in seen_urls:
                    seen_urls.add(pr["html_url"])
                    prs.append({"search_result": pr, "involvement": Involvement.REVIEWER})

            progress.update(task, description=f"Found {len(prs)} PRs total")

        console.print(f"[green]Found {len(prs)} PRs to process[/green]")
        return prs

    def _parse_repo_from_url(self, url: str) -> tuple[str, str, int] | None:
        """Parse owner, repo, and PR number from GitHub URL."""
        # Format: https://github.com/owner/repo/pull/123
        parts = url.rstrip("/").split("/")
        if "pull" not in parts:
            return None
        try:
            pull_idx = parts.index("pull")
            owner = parts[pull_idx - 2]
            repo = parts[pull_idx - 1]
            number = int(parts[pull_idx + 1])
        except (ValueError, IndexError):
            return None
        return owner, repo, number

    def _fetch_pr_with_reviews(self, owner: str, repo: str, number: int) -> dict[str, Any] | None:
        """Fetch PR details and review comments in a single GraphQL request."""
        try:
            data = self._graphql(PR_QUERY, {"owner": owner, "repo": repo, "number": number})
            return data.get("repository", {}).get("pullRequest")
        except Exception:
            logger.warning("Failed to fetch PR %s/%s#%d", owner, repo, number)
            return None

    def _determine_state(self, pr: dict[str, Any]) -> PRState:
        """Determine PR state from GraphQL response."""
        if pr.get("merged"):
            return "merged"
        state = pr.get("state", "OPEN").lower()
        return "closed" if state == "closed" else "open"

    def _format_pr_content(self, pr: dict[str, Any]) -> str:
        """Format PR content including review comments."""
        lines: list[str] = []

        # Title and body
        lines.append(f"# {pr.get('title', 'Untitled PR')}")
        lines.append("")

        body = pr.get("body") or ""
        if body.strip():
            lines.append(body.strip())
            lines.append("")

        # Collect all review comments
        comments: list[tuple[str, str, str | None, str]] = []  # (user, body, path, created_at)

        reviews = pr.get("reviews", {}).get("nodes", [])
        for review in reviews:
            # Review body (general review comment)
            review_body = review.get("body", "").strip()
            if review_body:
                user = review.get("author", {}).get("login", "unknown")
                created = review.get("createdAt", "")
                comments.append((user, review_body, None, created))

            # Inline review comments
            for comment in review.get("comments", {}).get("nodes", []):
                user = comment.get("author", {}).get("login", "unknown")
                body = comment.get("body", "").strip()
                path = comment.get("path")
                created = comment.get("createdAt", "")
                if body:
                    comments.append((user, body, path, created))

        # Sort by created_at and format
        comments.sort(key=lambda x: x[3])

        if comments:
            lines.append("## Review Comments")
            lines.append("")

            for user, body, path, _ in comments:
                if path:
                    lines.append(f"**{user}** (on `{path}`):")
                else:
                    lines.append(f"**{user}**:")
                lines.append(body)
                lines.append("")

        return "\n".join(lines)

    def item_to_documents(self, item: dict) -> list[Document]:
        """Convert a GitHub PR to documents."""
        search_result = item["search_result"]
        involvement = item["involvement"]

        # Parse repo info from URL
        parsed = self._parse_repo_from_url(search_result["html_url"])
        if not parsed:
            logger.warning("Could not parse URL: %s", search_result["html_url"])
            return []

        owner, repo_name, pr_number = parsed
        repo = f"{owner}/{repo_name}"

        # Fetch full PR with reviews via GraphQL (single request)
        pr = self._fetch_pr_with_reviews(owner, repo_name, pr_number)
        if not pr:
            # Fall back to search result data if GraphQL fails
            pr = {
                "title": search_result.get("title", ""),
                "body": search_result.get("body", ""),
                "state": search_result.get("state", "open"),
                "merged": bool(search_result.get("pull_request", {}).get("merged_at")),
                "url": search_result["html_url"],
                "updatedAt": search_result.get("updated_at", ""),
                "author": {"login": search_result.get("user", {}).get("login", "unknown")},
                "reviews": {"nodes": []},
            }

        # Format content
        content = self._format_pr_content(pr)

        # Determine state
        state = self._determine_state(pr)

        # Get author
        author_data = pr.get("author")
        if isinstance(author_data, dict):
            login = author_data.get("login")
            author = str(login) if login else "unknown"
        else:
            author = "unknown"

        # Parse timestamp
        updated_at = pr.get("updatedAt", "")
        if updated_at and isinstance(updated_at, str):
            dt = datetime.fromisoformat(updated_at)
            timestamp = int(dt.timestamp())
        else:
            timestamp = int(datetime.now(tz=UTC).timestamp())

        # Build source_id
        source_id = f"{repo}/pr/{pr_number}"

        metadata = DocumentMetadata(
            source=Source.GITHUB,
            source_id=source_id,
            timestamp=timestamp,
            content_type=ContentType.PR,
            author=author,
            my_involvement=involvement,
            permalink=search_result["html_url"],
            github_repo=repo,
            github_state=state,
            github_pr_number=pr_number,
        )

        return self.create_documents_from_content(
            source_id=source_id,
            content=content,
            metadata=metadata,
        )
