"""Git repository manager.

Clones a repo into a per-issue workspace, forks via GitHub, adds the upstream
remote, syncs, creates a feature branch, applies/commits/pushes to the fork
only (never upstream), and cleans up.
"""

from __future__ import annotations

import os
import shutil
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from git import Repo, RemoteProgress
from git.exc import GitCommandError, InvalidGitRepositoryError

from .config import config
from .github_client import (
    create_pull_request,
    get_default_branch,
    get_github,
    parse_issue_url,
    repo_is_fork,
    list_user_forks,
)
from .state import WorkspaceState, WorkflowState


SLUG_RE = re.compile(r"[^a-z0-9]+")


@dataclass
class RepoInfo:
    owner: str
    repo: str
    default_branch: str
    clone_url: str
    fork_owner: str
    fork_full_name: str
    workspace_path: str


class RepoManagerError(RuntimeError):
    pass


def slugify(text: str, max_len: int = 60) -> str:
    slug = SLUG_RE.sub("-", text.lower()).strip("-")
    return slug[:max_len].strip("-") or "fix"


def workspace_dir(owner: str, repo: str, issue_number: int) -> Path:
    return Path(config.workspace_root) / f"{owner}__{repo}__{issue_number}"


class RepoManager:
    """High-level git operations confined to WORKSPACE_ROOT."""

    def __init__(self) -> None:
        self.token = config.github_token
        if not self.token:
            print("[issue_agent] [repo_manager] ERROR: GITHUB_TOKEN is not set", flush=True)
            raise RepoManagerError("GITHUB_TOKEN is not set")
        print("[issue_agent] [repo_manager] RepoManager initialized", flush=True)

    # ----- setup / sync -----
    def prepare(
        self,
        issue_url: str,
        state: WorkspaceState,
    ) -> RepoInfo:
        """Fork (reuse existing), clone, add upstream, sync, create branch."""
        print(f"[issue_agent] [repo_manager] prepare() starting for: {issue_url}", flush=True)
        parsed = parse_issue_url(issue_url)
        owner, repo, number = parsed.owner, parsed.repo, parsed.number

        # Determine / reuse a fork.
        print(f"[issue_agent] [repo_manager] Ensuring fork for {owner}/{repo}...", flush=True)
        fork_full_name = self._ensure_fork(owner, repo)
        fork_owner = fork_full_name.split("/", 1)[0]
        print(f"[issue_agent] [repo_manager] Fork: {fork_full_name} (owner: {fork_owner})", flush=True)

        default_branch = get_default_branch(owner, repo)
        print(f"[issue_agent] [repo_manager] Default branch: {default_branch}", flush=True)

        path = workspace_dir(owner, repo, number)
        path.mkdir(parents=True, exist_ok=True)

        # Clone into workspace (reuse if already present).
        clone_url = f"https://x-access-token:{self.token}@github.com/{fork_full_name}.git"
        if self._is_valid_repo(path):
            print(f"[issue_agent] [repo_manager] Reusing existing repo at {path}", flush=True)
            repo_obj = Repo(path)
            # ensure remotes
            self._ensure_remotes(repo_obj, owner, repo, fork_full_name)
        else:
            # clean stale partial dir
            if any(path.iterdir()):
                print(f"[issue_agent] [repo_manager] Cleaning stale partial dir at {path}", flush=True)
                shutil.rmtree(path)
                path.mkdir(parents=True, exist_ok=True)
            print(f"[issue_agent] [repo_manager] Initializing new repo at {path}", flush=True)
            print(f"[issue_agent] [repo_manager] Cloning from {clone_url}...", flush=True)
            repo_obj = Repo.init(path)
            origin = repo_obj.create_remote("origin", clone_url)
            origin.fetch()
            print(f"[issue_agent] [repo_manager] Fetch complete.", flush=True)
            repo_obj.create_head(default_branch, origin.refs[default_branch])
            repo_obj.heads[default_branch].checkout()
            repo_obj.create_remote("upstream", f"https://github.com/{owner}/{repo}.git")
            print(f"[issue_agent] [repo_manager] Remotes origin/upstream configured.", flush=True)

        # Sync with upstream's default branch.
        print(f"[issue_agent] [repo_manager] Syncing with upstream/{default_branch}...", flush=True)
        self._sync_with_upstream(repo_obj, owner, repo, default_branch)
        print(f"[issue_agent] [repo_manager] Sync complete.", flush=True)

        # Create feature branch fix/<issue#>-<slug>
        issue = self._fetch_issue_title(issue_url)
        branch = f"fix/{number}-{slugify(issue)}"
        print(f"[issue_agent] [repo_manager] Creating feature branch: {branch}", flush=True)
        if branch in [h.name for h in repo_obj.heads]:
            repo_obj.heads[branch].checkout()
            print(f"[issue_agent] [repo_manager] Checked out existing branch {branch}", flush=True)
        else:
            repo_obj.create_head(branch).checkout()
            print(f"[issue_agent] [repo_manager] Created and checked out branch {branch}", flush=True)

        state.branch = branch
        state.workspace_path = str(path)
        state.default_branch = default_branch
        state.transition_to(WorkflowState.PREPARED)
        print(f"[issue_agent] [repo_manager] prepare() complete. Workspace: {path}", flush=True)

        return RepoInfo(
            owner=owner,
            repo=repo,
            default_branch=default_branch,
            clone_url=clone_url,
            fork_owner=fork_owner,
            fork_full_name=fork_full_name,
            workspace_path=str(path),
        )

    # ----- diff / commit / push -----
    def diff_stat(self, workspace_path: str) -> str:
        """Return a short diff stat (like `git diff --stat`) for tracked changes."""
        repo_obj = Repo(workspace_path)
        # unstaged + staged changes vs HEAD
        try:
            diff_index = repo_obj.index.diff(None)
            diff_head = repo_obj.head.commit.diff(None) if repo_obj.head.is_valid_branch else []
        except Exception:
            diff_index = []
            diff_head = []
        changed = {d.a_path or d.b_path for d in list(diff_index) + list(diff_head)}
        if not changed:
            return "no changes"
        # Count lines via git diff --numstat for accuracy
        try:
            out = repo_obj.git.diff("--numstat", "HEAD", "--", *sorted(changed))
        except GitCommandError:
            out = ""
        lines = []
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) == 3:
                add, rem, fpath = parts
                lines.append(f" {add} added/{rem} deleted {fpath}")
        return "\n".join(lines) if lines else "binary or untracked changes"

    def count_changed_lines(self, workspace_path: str) -> int:
        repo_obj = Repo(workspace_path)
        try:
            out = repo_obj.git.diff("--numstat", "HEAD")
        except GitCommandError:
            return 0
        total = 0
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) == 3:
                try:
                    total += int(parts[0]) + int(parts[1])
                except ValueError:
                    pass
        return total

    def commit(self, workspace_path: str, message: str) -> str:
        """Stage all changes and commit. Returns the commit SHA."""
        print(f"[issue_agent] [repo_manager] commit() staging all changes in {workspace_path}", flush=True)
        repo_obj = Repo(workspace_path)
        repo_obj.git.add("-A")
        if not repo_obj.is_dirty(untracked_files=True) and not repo_obj.index.diff("HEAD"):
            # nothing to commit unless untracked exist
            if not repo_obj.untracked_files:
                print(f"[issue_agent] [repo_manager] commit() no changes to commit", flush=True)
                raise RepoManagerError("No changes to commit")
        commit = repo_obj.index.commit(message)
        print(f"[issue_agent] [repo_manager] commit() created commit {commit.hexsha[:8]}: {message}", flush=True)
        return commit.hexsha

    def push(self, workspace_path: str, branch: str) -> None:
        """Push the branch to origin (the fork) only. Never upstream, never force."""
        print(f"[issue_agent] [repo_manager] push() pushing branch '{branch}' to origin (fork)...", flush=True)
        repo_obj = Repo(workspace_path)
        origin = repo_obj.remote("origin")
        origin.push(refspec=f"{branch}:{branch}", force=False)
        print(f"[issue_agent] [repo_manager] push() complete.", flush=True)

    # ----- cleanup -----
    def cleanup(self, workspace_path: str) -> None:
        p = Path(workspace_path)
        if not self._is_within_root(p):
            raise RepoManagerError(f"Refusing to remove path outside WORKSPACE_ROOT: {p}")
        if p.exists():
            print(f"[issue_agent] [repo_manager] cleanup() removing {workspace_path}", flush=True)
            shutil.rmtree(p, ignore_errors=True)

    # ----- internals -----
    def _is_valid_repo(self, path: Path) -> bool:
        try:
            Repo(path)
            return True
        except InvalidGitRepositoryError:
            return False

    def _ensure_remotes(
        self, repo_obj: Repo, owner: str, repo: str, fork_full_name: str
    ) -> None:
        names = [r.name for r in repo_obj.remotes]
        origin_url = f"https://x-access-token:{self.token}@github.com/{fork_full_name}.git"
        upstream_url = f"https://github.com/{owner}/{repo}.git"
        if "origin" not in names:
            repo_obj.create_remote("origin", origin_url)
        else:
            repo_obj.remotes.origin.set_url(origin_url)
        if "upstream" not in names:
            repo_obj.create_remote("upstream", upstream_url)
        else:
            repo_obj.remotes.upstream.set_url(upstream_url)

    def _sync_with_upstream(self, repo_obj: Repo, owner: str, repo: str, branch: str) -> None:
        print(f"[issue_agent] [repo_manager] _sync_with_upstream: fetching {branch} from upstream", flush=True)
        upstream = repo_obj.remote("upstream")
        upstream.fetch(branch)
        try:
            repo_obj.heads[branch].set_tracking_branch(
                repo_obj.remote("upstream").refs[branch]
            )
        except (IndexError, AttributeError):
            pass
        # fast-forward local default branch to upstream
        repo_obj.git.checkout(branch)
        try:
            repo_obj.git.merge("--ff-only", f"upstream/{branch}")
            print(f"[issue_agent] [repo_manager] _sync_with_upstream: fast-forward merge succeeded", flush=True)
        except GitCommandError:
            print(f"[issue_agent] [repo_manager] _sync_with_upstream: merge failed, resetting hard to upstream/{branch}", flush=True)
            # If merge fails, reset hard to upstream (safe: we haven't committed feature work yet)
            repo_obj.git.reset("--hard", f"upstream/{branch}")

    def _ensure_fork(self, owner: str, repo: str) -> str:
        """Return fork full_name (fork_owner/repo), creating one if needed."""
        print(f"[issue_agent] [repo_manager] _ensure_fork: checking for fork {owner}/{repo}", flush=True)
        gh = get_github()
        viewer = gh.get_user()
        login = viewer.login
        fork_full_name = f"{login}/{repo}"

        # Check whether the fork already exists.
        try:
            gh.get_repo(fork_full_name)
            print(f"[issue_agent] [repo_manager] _ensure_fork: fork already exists: {fork_full_name}", flush=True)
            return fork_full_name
        except Exception:
            pass

        # Create the fork.
        print(f"[issue_agent] [repo_manager] _ensure_fork: creating fork {fork_full_name}...", flush=True)
        source = gh.get_repo(f"{owner}/{repo}")
        try:
            fork = source.create_fork()
            # wait briefly for it to be ready
            import time
            for _ in range(10):
                try:
                    gh.get_repo(fork_full_name)
                    print(f"[issue_agent] [repo_manager] _ensure_fork: fork ready: {fork_full_name}", flush=True)
                    return fork_full_name
                except Exception:
                    time.sleep(1)
            return fork_full_name
        except Exception as exc:
            raise RepoManagerError(f"Could not ensure fork {fork_full_name}: {exc}") from exc

    def _fetch_issue_title(self, issue_url: str) -> str:
        from .github_client import fetch_issue

        try:
            return fetch_issue(issue_url).title
        except Exception:
            return "issue"

    @staticmethod
    def _is_within_root(path: Path) -> bool:
        root = Path(config.workspace_root).resolve()
        try:
            path.resolve().relative_to(root)
            return True
        except ValueError:
            return False
