"""GitHub client wrapper around PyGithub.

Provides deterministic, read-mostly operations used by the analyzer and PR
creation tools. All errors are surfaced as clear, typed exceptions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional
from urllib.parse import urlparse

from github import Github, GithubException, RateLimitExceededException
from github.Issue import Issue
from github.PullRequest import PullRequest
from github.Repository import Repository

from .config import config


class GitHubError(RuntimeError):
    """Raised when a GitHub API operation fails."""


@dataclass
class ParsedIssue:
    owner: str
    repo: str
    number: int
    html_url: str


ISSUE_URL_RE = re.compile(
    r"^https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/issues/(?P<number>\d+)/?$"
)


def parse_issue_url(url: str) -> ParsedIssue:
    """Parse a GitHub issue URL into its components."""
    m = ISSUE_URL_RE.match(url.strip())
    if not m:
        raise GitHubError(
            f"Unsupported issue URL: {url!r}. Expected "
            "https://github.com/<owner>/<repo>/issues/<number>"
        )
    return ParsedIssue(
        owner=m.group("owner"),
        repo=m.group("repo"),
        number=int(m.group("number")),
        html_url=url,
    )


def get_github() -> Github:
    """Build an authenticated PyGithub client from config."""
    token = config.github_token
    if not token:
        raise GitHubError(
            "GITHUB_TOKEN is not set. Create a personal access token and add it "
            "to .env (see README for required scopes)."
        )
    return Github(login_or_token=token, base_url=config.github_api_base_url)


def fetch_issue(url: str) -> Issue:
    gh = get_github()
    parsed = parse_issue_url(url)
    try:
        repo = gh.get_repo(f"{parsed.owner}/{parsed.repo}")
        return repo.get_issue(number=parsed.number)
    except RateLimitExceededException as exc:
        raise GitHubError(f"GitHub rate limit exceeded: {exc}") from exc
    except GithubException as exc:
        raise GitHubError(
            f"GitHub API error fetching issue {url}: {exc.status} {exc.data}"
        ) from exc


def fetch_repo(owner: str, repo: str) -> Repository:
    gh = get_github()
    try:
        return gh.get_repo(f"{owner}/{repo}")
    except GithubException as exc:
        raise GitHubError(
            f"GitHub API error fetching repo {owner}/{repo}: {exc.status} {exc.data}"
        ) from exc


@dataclass
class IssueData:
    number: int
    title: str
    state: str  # open | closed
    locked: bool
    html_url: str
    body: str
    created_at: str
    updated_at: str
    closed_at: Optional[str]
    labels: List[str]
    assignees: List[str]
    author: str
    milestone: Optional[str]
    comments: List[dict]
    linked_prs: List[dict]  # PRs in body that reference this issue
    open_prs_referencing: List[dict]  # open PRs whose body mentions this issue


def gather_issue_data(url: str) -> IssueData:
    """Fetch issue plus comments, labels, assignees, and related PRs."""
    issue = fetch_issue(url)
    parsed = parse_issue_url(url)
    repo = issue.repository

    labels = [l.name for l in issue.get_labels()]
    assignees = [a.login for a in issue.get_assignees()]
    comments = [
        {
            "id": c.id,
            "user": c.user.login if c.user else None,
            "body": c.body or "",
            "created_at": str(c.created_at),
        }
        for c in issue.get_comments()
    ]

    # PRs linked in the issue body (e.g. "fixes #12" or a PR URL).
    linked_prs: List[dict] = []
    pr_number_re = re.compile(r"(?:fixes?|closes?|resolves?)\s+#(\d+)", re.IGNORECASE)
    for num in pr_number_re.findall(issue.body or ""):
        try:
            pr = repo.get_pull(int(num))
            linked_prs.append(
                {
                    "number": pr.number,
                    "title": pr.title,
                    "state": pr.state,
                    "html_url": pr.html_url,
                    "head": f"{pr.head.repo.full_name}:{pr.head.ref}",
                    "base": f"{pr.base.repo.full_name}:{pr.base.ref}",
                }
            )
        except GithubException:
            continue

    # Open PRs whose body references this issue.
    open_prs_referencing: List[dict] = []
    try:
        for pr in repo.get_pulls(state="open"):
            body = pr.body or ""
            if f"#{issue.number}" in body or f"issues/{issue.number}" in body:
                open_prs_referencing.append(
                    {
                        "number": pr.number,
                        "title": pr.title,
                        "html_url": pr.html_url,
                        "head": f"{pr.head.repo.full_name}:{pr.head.ref}",
                        "base": f"{pr.base.repo.full_name}:{pr.base.ref}",
                    }
                )
    except GithubException:
        pass

    contributing_notes = ""
    try:
        readme = repo.get_readme()
        # lightweight: just note README exists
        contributing_notes += f"README present ({readme.path}). "
    except GithubException:
        pass
    try:
        cont = repo.get_contents("CONTRIBUTING.md")
        contributing_notes += f"CONTRIBUTING.md present ({cont.size} bytes). "
    except GithubException:
        try:
            cont = repo.get_contents("CONTRIBUTING")
            contributing_notes += f"CONTRIBUTING present ({cont.size} bytes). "
        except GithubException:
            pass

    return IssueData(
        number=issue.number,
        title=issue.title,
        state=issue.state,
        locked=issue.locked,
        html_url=issue.html_url,
        body=issue.body or "",
        created_at=str(issue.created_at),
        updated_at=str(issue.updated_at),
        closed_at=str(issue.closed_at) if issue.closed_at else None,
        labels=labels,
        assignees=assignees,
        author=issue.user.login if issue.user else None,
        milestone=issue.milestone.title if issue.milestone else None,
        comments=comments,
        linked_prs=linked_prs,
        open_prs_referencing=open_prs_referencing,
        _contributing_notes_raw=contributing_notes,
    )


def get_rate_limit() -> dict:
    gh = get_github()
    try:
        rl = gh.get_rate_limit()
        return {
            "core": {
                "limit": rl.core.limit,
                "remaining": rl.core.remaining,
                "reset": str(rl.core.reset),
            },
            "search": {
                "limit": rl.search.limit,
                "remaining": rl.search.remaining,
                "reset": str(rl.search.reset),
            },
        }
    except GithubException as exc:
        raise GitHubError(f"GitHub API error fetching rate limit: {exc}") from exc


def create_pull_request(
    owner: str,
    repo: str,
    title: str,
    body: str,
    head: str,
    base: str,
) -> dict:
    """Open a PR from ``head`` to ``base`` in ``owner/repo``.

    ``head`` must be a fork branch (e.g. ``forkowner:branch``). Never force-push
    is handled at the git layer; here we just open the PR.
    """
    gh = get_github()
    repository = gh.get_repo(f"{owner}/{repo}")
    try:
        pr = repository.create_pull(title=title, body=body, head=head, base=base)
        return {
            "number": pr.number,
            "url": pr.html_url,
            "state": pr.state,
            "head": pr.head.ref,
            "base": pr.base.ref,
        }
    except GithubException as exc:
        raise GitHubError(
            f"Failed to create PR: {exc.status} {exc.data}"
        ) from exc


def get_default_branch(owner: str, repo: str) -> str:
    repository = fetch_repo(owner, repo)
    return repository.default_branch


def repo_is_fork(owner: str, repo: str) -> bool:
    repository = fetch_repo(owner, repo)
    return bool(repository.fork)


def list_user_forks(login: str) -> List[dict]:
    gh = get_github()
    user = gh.get_user(login)
    forks: List[dict] = []
    try:
        for r in user.get_repos(type="fork", sort="full_name"):
            forks.append(
                {"full_name": r.full_name, "html_url": r.html_url, "default_branch": r.default_branch}
            )
    except GithubException:
        pass
    return forks


def get_repo_contents(repo_obj: Repository, path: str) -> Optional[str]:
    """Return decoded text contents of a file, or None if absent."""
    try:
        content = repo_obj.get_contents(path)
        if isinstance(content, list):
            return None
        return content.decoded_content.decode("utf-8", errors="replace")
    except GithubException:
        return None


# Attach contributing notes holder used by gather_issue_data.
IssueData._contributing_notes_raw: str = ""
