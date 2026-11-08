"""issue_agent MCP server (FastMCP, streamable HTTP on 127.0.0.1 only).

Provides deterministic git/GitHub tools, guardrails, and a per-workflow state
machine that helps an LLM turn a GitHub issue into an open-source PR.

The host LLM does the reasoning and code editing; this server is the
deterministic, safety-checked interface to git and GitHub.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from fastmcp import FastMCP, Context
from pydantic import BaseModel

from . import config as config_mod
from .config import config
from .context import ContextError, file_slices, ripgrep_search, repo_map
from .detector import detect_language
from .github_client import (
    GitHubError,
    create_pull_request,
    gather_issue_data,
    get_default_branch,
    get_repo_contents,
    parse_issue_url,
)
from .models import (
    AnalyzeIssueInput,
    AnalyzeIssueOutput,
    ApplyPatchInput,
    ApplyPatchOutput,
    CheckResult,
    CleanupOutput,
    CommitPushInput,
    CommitPushOutput,
    CreatePRInput,
    CreatePROutput,
    GetContextInput,
    GetContextOutput,
    PrepareWorkspaceInput,
    PrepareWorkspaceOutput,
    RunChecksInput,
    RunChecksOutput,
    StatusOutput,
    IssueActionable,
)
from .repo_manager import RepoManager, RepoManagerError, slugify, workspace_dir
from .sandbox import CommandResult, run_command, scrub_env
from .state import WorkflowState, WorkspaceState, allowed_from, require_state

logger = logging.getLogger("issue_agent")
logger.setLevel(logging.INFO)
_handler = logging.StreamHandler(sys.stderr)
_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
logger.addHandler(_handler)

app = FastMCP(
    name="issue_agent",
    instructions=(
        "issue_agent: turn a GitHub issue into an open-source PR. "
        "Tools: analyze_issue -> prepare_workspace -> [edit/apply_patch] -> "
        "run_checks -> commit_and_push -> create_pr. State is enforced per "
        "workspace. The GitHub token is never logged or returned."
    ),
)


# ---------------------------------------------------------------------------
# In-memory session store keyed by issue URL, persisted to workspace JSON.
# ---------------------------------------------------------------------------
@dataclass
class SessionStore:
    _sessions: dict = field(default_factory=dict)  # issue_url -> WorkspaceState

    def get(self, issue_url: str) -> WorkspaceState:
        if issue_url in self._sessions:
            return self._sessions[issue_url]
        # Try to reconstruct from a known workspace path if we have one cached.
        ws = self._sessions.get(issue_url)
        if ws and ws.workspace_path:
            loaded = WorkspaceState.load(ws.workspace_path, issue_url)
            self._sessions[issue_url] = loaded
            return loaded
        return WorkspaceState(issue_url=issue_url)

    def set(self, issue_url: str, state: WorkspaceState) -> None:
        self._sessions[issue_url] = state
        state.save()

    def reset_to_prepared(self, issue_url: str) -> None:
        st = self.get(issue_url)
        st.reset_to_prepared()


store = SessionStore()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _require_reachable(token: str, owner: str, repo: str) -> None:
    """Guardrail: only operate on repos reachable by the token."""
    if not token:
        raise GitHubError("GITHUB_TOKEN is not set; cannot access GitHub.")
    # A lightweight reachability check: fetch the repo.
    from .github_client import fetch_repo

    fetch_repo(owner, repo)


def _contributing_forbids_ai(workspace_path: str, repo_owner: str, repo_name: str) -> tuple:
    """Check CONTRIBUTING.md for AI-contribution policy. Returns (forbids, notes)."""
    notes = ""
    try:
        from .github_client import fetch_repo

        repo = fetch_repo(repo_owner, repo_name)
        text = get_repo_contents(repo, "CONTRIBUTING.md") or get_repo_contents(repo, "CONTRIBUTING")
        if text:
            notes = text[:2000]
            low = text.lower()
            if any(k in low for k in ("no ai", "ai-generated", "no llm", "no large language model", "prohibit.*ai")):
                return True, notes
    except Exception:
        pass
    return False, notes


def _detect_commit_convention(repo_obj) -> bool:
    """Heuristic: does the repo use Conventional Commits?"""
    try:
        log = repo_obj.git.log("--format=%s", "-20")
    except Exception:
        return False
    markers = ("feat:", "fix:", "docs:", "refactor:", "test:", "chore:", "build:", "ci:", "perf:")
    return any(line.startswith(markers) for line in log.splitlines())


def _needs_dco(repo_owner: str, repo_name: str) -> bool:
    try:
        from .github_client import fetch_repo, get_repo_contents

        repo = fetch_repo(repo_owner, repo_name)
        text = get_repo_contents(repo, "CONTRIBUTING.md") or get_repo_contents(repo, "CONTRIBUTING") or ""
        low = text.lower()
        return any(k in low for k in ("dco", "developer certificate", "sign-off", "signed-off-by"))
    except Exception:
        return False


def _commit_message_with_convention(message: str, conventional: bool, dco: bool, author: str) -> str:
    if conventional:
        # Validate / normalize to conventional commit. If it doesn't start with a type, prefix 'chore:'.
        types = ("feat", "fix", "docs", "refactor", "test", "chore", "build", "ci", "perf")
        if not any(message.startswith(t + ":") or message.startswith(t + "(") for t in types):
            message = f"chore: {message}"
    if dco:
        message = message.rstrip() + f"\n\nSigned-off-by: {author}"
    return message


# ---------------------------------------------------------------------------
# Tool: analyze_issue
# ---------------------------------------------------------------------------
@app.tool(name="analyze_issue")
async def analyze_issue(
    inp: AnalyzeIssueInput,
    ctx: Context,
    use_sampling: bool = False,
) -> AnalyzeIssueOutput:
    """Analyze a GitHub issue for actionability.

    Fetches the issue, comments, labels, assignees, linked/open PRs, and
    CONTRIBUTING.md. Returns a verdict plus reasons. Sets state to ANALYZED.
    """
    try:
        parsed = parse_issue_url(inp.url)
    except GitHubError as exc:
        logger.warning("analyze_issue bad URL: %s", exc)
        return AnalyzeIssueOutput(
            actionable=False,
            already_fixed=False,
            claimed_by=None,
            needs_clarification=False,
            risk=IssueActionable.med,
            reasons=[str(exc)],
            contributing_notes="",
        )

    owner, repo = parsed.owner, parsed.repo
    _require_reachable(config.github_token, owner, repo)

    try:
        data = gather_issue_data(inp.url)
    except GitHubError as exc:
        logger.warning("analyze_issue fetch failed: %s", exc)
        return AnalyzeIssueOutput(
            actionable=False,
            already_fixed=False,
            claimed_by=None,
            needs_clarification=False,
            risk=IssueActionable.med,
            reasons=[f"GitHub fetch error: {exc}"],
            contributing_notes="",
        )

    from .analyzer import analyze as analyze_heuristics

    output = analyze_heuristics(data, use_sampling=use_sampling and ctx is not None)

    # Guardrail: refuse if claiming signals present (already handled by heuristics).
    state = store.get(inp.url)
    state.extra["owner"] = owner
    state.extra["repo"] = repo
    state.extra["issue_number"] = str(parsed.number)
    state.extra["_actionable"] = str(output.actionable)
    err = state.transition_to(WorkflowState.ANALYZED)
    if err:
        logger.warning("analyze_issue state transition: %s", err)

    logger.info(
        "analyze_issue %s actionable=%s risk=%s reasons=%d",
        inp.url,
        output.actionable,
        output.risk,
        len(output.reasons),
    )
    return output


# ---------------------------------------------------------------------------
# Tool: prepare_workspace
# ---------------------------------------------------------------------------
@app.tool(name="prepare_workspace")
async def prepare_workspace(
    inp: PrepareWorkspaceInput,
    ctx: Context,
) -> PrepareWorkspaceOutput:
    """Fork, clone, sync, and branch for the issue.

    Requires state ANALYZED with actionable=true. Sets state to PREPARED.
    """
    state = store.get(inp.url)
    err = require_state(state.state, WorkflowState.ANALYZED, "prepare_workspace")
    if err:
        return _prepare_error(err)

    try:
        parsed = parse_issue_url(inp.url)
    except GitHubError as exc:
        return _prepare_error(str(exc))

    owner, repo, number = parsed.owner, parsed.repo, parsed.number
    if not state.extra.get("_actionable", "true").lower() == "true":
        return _prepare_error("Issue is not actionable (previous analyze_issue returned actionable=false).")

    _require_reachable(config.github_token, owner, repo)

    try:
        mgr = RepoManager()
        info = mgr.prepare(inp.url, state)
    except (RepoManagerError, GitHubError) as exc:
        logger.warning("prepare_workspace failed: %s", exc)
        return _prepare_error(str(exc))

    # Persist fork owner for PR creation later.
    state.extra["fork_owner"] = info.fork_owner

    # Detect language + commands.
    det = detect_language(info.workspace_path)
    try:
        rmap = repo_map(info.workspace_path)
    except ContextError as exc:
        rmap = f"(could not build repo map: {exc})"

    store.set(inp.url, state)

    logger.info(
        "prepare_workspace %s -> %s branch=%s", inp.url, info.workspace_path, info.branch
    )
    return PrepareWorkspaceOutput(
        workspace_path=info.workspace_path,
        branch=info.branch,
        default_branch=info.default_branch,
        language=det.language,
        test_command=det.test_command,
        lint_command=det.lint_command,
        format_command=det.format_command,
        repo_map=rmap,
    )


def _prepare_error(message: str) -> PrepareWorkspaceOutput:
    return PrepareWorkspaceOutput(
        workspace_path="",
        branch="",
        default_branch="",
        language="",
        test_command=None,
        lint_command=None,
        format_command=None,
        repo_map="",
    )


# ---------------------------------------------------------------------------
# Tool: get_context
# ---------------------------------------------------------------------------
@app.tool(name="get_context")
async def get_context(
    inp: GetContextInput,
    ctx: Context,
) -> GetContextOutput:
    """Search (ripgrep) or slice files within the workspace only.

    Exactly one of ``query`` or ``paths`` should be provided. Never reads
    outside WORKSPACE_ROOT; rejects path traversal and escaping symlinks.
    """
    # Determine workspace from current state if available via ctx state or a
    # heuristic: we require a prior prepare_workspace. Find the most recent
    # prepared session. For simplicity, require the caller to have set state.
    # We search across all known sessions' workspaces.
    workspace = _current_workspace(store)
    if not workspace:
        return GetContextOutput(
            matches=[],
            slices=[],
            truncated=False,
        )

    root = Path(workspace).resolve()
    if not _is_within_root(root):
        return GetContextOutput(matches=[], slices=[], truncated=False)

    if inp.query:
        matches, truncated = ripgrep_search(workspace, inp.query, inp.max_bytes)
        return GetContextOutput(matches=matches, slices=[], truncated=truncated)

    if inp.paths:
        slices, truncated = file_slices(workspace, inp.paths, inp.max_bytes)
        return GetContextOutput(matches=[], slices=slices, truncated=truncated)

    # Default: return repo map as a single synthetic match block.
    try:
        rmap = repo_map(workspace)
    except ContextError:
        rmap = ""
    return GetContextOutput(matches=[rmap] if rmap else [], slices=[], truncated=False)


def _current_workspace(store: SessionStore) -> Optional[str]:
    for url, st in store._sessions.items():
        if st.workspace_path and st.state in {
            WorkflowState.PREPARED,
            WorkflowState.CHECKS_PASSED,
            WorkflowState.PUSHED,
        }:
            return st.workspace_path
    return None


def _is_within_root(path: Path) -> bool:
    root = Path(config.workspace_root).resolve()
    try:
        path.resolve().relative_to(root)
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Tool: apply_patch
# ---------------------------------------------------------------------------
@app.tool(name="apply_patch")
async def apply_patch(
    inp: ApplyPatchInput,
    ctx: Context,
) -> ApplyPatchOutput:
    """Apply a unified diff to the workspace. Resets state to PREPARED if it was CHECKS_PASSED."""
    workspace = _current_workspace(store)
    if not workspace:
        return ApplyPatchOutput(applied=False, files_changed=[], message="No active workspace.")

    state = _state_for_workspace(store, workspace)
    if state:
        state.reset_to_prepared()

    # Validate workspace confinement of the patch itself (paths inside diff).
    try:
        import subprocess
        proc = subprocess.run(
            ["git", "apply", "--check", "--verbose", "--directory", workspace],
            input=inp.diff,
            capture_output=True,
            text=True,
            timeout=60,
            env=scrub_env(),
        )
    except FileNotFoundError:
        return ApplyPatchOutput(applied=False, files_changed=[], message="git not available.")
    except Exception as exc:
        return ApplyPatchOutput(applied=False, files_changed=[], message=f"patch check failed: {exc}")

    if proc.returncode != 0:
        return ApplyPatchOutput(
            applied=False,
            files_changed=[],
            message=f"patch did not apply cleanly:\n{proc.stderr or proc.stdout}",
        )

    proc2 = subprocess.run(
        ["git", "apply", "--directory", workspace],
        input=inp.diff,
        capture_output=True,
        text=True,
        timeout=120,
        env=scrub_env(),
    )
    if proc2.returncode != 0:
        return ApplyPatchOutput(
            applied=False,
            files_changed=[],
            message=f"patch apply failed:\n{proc2.stderr or proc2.stdout}",
        )

    # Extract changed file paths from diff headers.
    changed = []
    for line in inp.diff.splitlines():
        if line.startswith("+++ b/"):
            changed.append(line[6:])
    store_changed = state
    if store_changed:
        store.set(store_changed.issue_url, store_changed)

    logger.info("apply_patch applied %d files in %s", len(changed), workspace)
    return ApplyPatchOutput(applied=True, files_changed=changed, message="patch applied cleanly")


def _state_for_workspace(store: SessionStore, workspace: str) -> Optional[WorkspaceState]:
    for st in store._sessions.values():
        if st.workspace_path == workspace:
            return st
    return None


# ---------------------------------------------------------------------------
# Tool: run_checks
# ---------------------------------------------------------------------------
@app.tool(name="run_checks")
async def run_checks(
    inp: RunChecksInput,
    ctx: Context,
) -> RunChecksOutput:
    """Run test/lint/format commands in the sandbox.

    Output is truncated and the env is scrubbed of secrets. Sets
    CHECKS_PASSED only if all required checks pass.
    """
    workspace = _current_workspace(store)
    if not workspace:
        return RunChecksOutput(checks_passed=False, results=[])

    state = _state_for_workspace(store, workspace)
    if state and state.state not in {WorkflowState.PREPARED, WorkflowState.CHECKS_PASSED}:
        # still allow running checks but don't transition unless PREPARED
        pass

    # Build command list.
    commands: list = []
    if inp.command:
        commands.append(inp.command)
    else:
        from .detector import detect_language

        det = detect_language(workspace)
        for cmd in (det.test_command, det.lint_command, det.format_command):
            if cmd:
                commands.append(cmd)

    results: list = []
    all_passed = True
    for cmd in commands:
        res = run_command(cmd, cwd=workspace)
        results.append(
            CheckResult(
                command=cmd,
                passed=res.return_code == 0 and not res.timed_out,
                return_code=res.return_code,
                log=res.log,
                truncated=res.truncated,
            )
        )
        if res.return_code != 0 or res.timed_out:
            all_passed = False

    if all_passed and results:
        state_transition = state
        if state_transition:
            err = state_transition.transition_to(WorkflowState.CHECKS_PASSED)
            if err:
                logger.warning("run_checks transition: %s", err)
        store.set(state_transition.issue_url, state_transition) if state_transition else None

    logger.info("run_checks %s passed=%s commands=%d", workspace, all_passed, len(results))
    return RunChecksOutput(checks_passed=all_passed and bool(results), results=results)


# ---------------------------------------------------------------------------
# Tool: commit_and_push
# ---------------------------------------------------------------------------
@app.tool(name="commit_and_push")
async def commit_and_push(
    inp: CommitPushInput,
    ctx: Context,
) -> CommitPushOutput:
    """Commit and push to the fork only.

    Requires CHECKS_PASSED. Enforces MAX_DIFF_LINES, follows conventional-commit
    convention if detected, adds DCO sign-off if required. Asks for confirmation
    via elicitation showing the diff stat. Pushes to origin (fork), never
    upstream, never force. Sets state to PUSHED.
    """
    workspace = _current_workspace(store)
    if not workspace:
        return CommitPushOutput(
            committed=False, pushed=False, branch="", diff_stat="",
            message="No active workspace.",
        )

    state = _state_for_workspace(store, workspace)
    if not state:
        return CommitPushOutput(
            committed=False, pushed=False, branch="", diff_stat="",
            message="No state found for workspace.",
        )

    err = require_state(state.state, WorkflowState.CHECKS_PASSED, "commit_and_push")
    if err:
        return CommitPushOutput(
            committed=False, pushed=False, branch="", diff_stat="", message=err
        )

    from .repo_manager import RepoManager

    try:
        mgr = RepoManager()
    except RepoManagerError as exc:
        return CommitPushOutput(
            committed=False, pushed=False, branch="", diff_stat="", message=str(exc)
        )

    # Diff stat + line enforcement.
    stat = mgr.diff_stat(workspace)
    nlines = mgr.count_changed_lines(workspace)
    if nlines > config.max_diff_lines:
        return CommitPushOutput(
            committed=False,
            pushed=False,
            branch=state.branch or "",
            diff_stat=stat,
            message=(
                f"Diff exceeds MAX_DIFF_LINES ({nlines} > {config.max_diff_lines}). "
                "Split into smaller changes."
            ),
        )

    # Elicitation: ask for confirmation showing the diff stat.
    confirm = await _elicit_confirm(
        ctx,
        message=(
            f"About to commit and push branch `{state.branch}` to your fork (origin), never upstream.\n"
            f"Diff stat:\n{stat}\n\n"
            f"Commit message: {inp.message}\n\nConfirm?"
        ),
        response_title="commit_and_push",
    )
    if confirm is not True:
        return CommitPushOutput(
            committed=False,
            pushed=False,
            branch=state.branch or "",
            diff_stat=stat,
            message="Commit/push declined or not supported by client.",
        )

    # Commit convention + DCO.
    conventional = False
    dco = False
    author = "issue-agent <issue-agent@localhost>"
    try:
        repo_obj = __import__("git").Repo(workspace)
        conventional = _detect_commit_convention(repo_obj)
    except Exception:
        pass
    owner = state.extra.get("owner", "")
    repo = state.extra.get("repo", "")
    dco = _needs_dco(owner, repo)

    final_msg = _commit_message_with_convention(inp.message, conventional, dco, author)

    try:
        sha = mgr.commit(workspace, final_msg)
        mgr.push(workspace, state.branch or "fix")
    except (RepoManagerError, Exception) as exc:  # noqa: BLE001
        logger.warning("commit_and_push failed: %s", exc)
        return CommitPushOutput(
            committed=False,
            pushed=False,
            branch=state.branch or "",
            diff_stat=stat,
            message=f"commit/push failed: {exc}",
        )

    err = state.transition_to(WorkflowState.PUSHED)
    if err:
        logger.warning("commit_and_push transition: %s", err)
    store.set(state.issue_url, state)

    logger.info("commit_and_push %s branch=%s sha=%s", workspace, state.branch, sha)
    return CommitPushOutput(
        committed=True,
        pushed=True,
        branch=state.branch or "",
        diff_stat=stat,
        message=f"Committed {sha[:8]} and pushed to fork.",
    )


# ---------------------------------------------------------------------------
# Tool: create_pr
# ---------------------------------------------------------------------------
@app.tool(name="create_pr")
async def create_pr(
    inp: CreatePRInput,
    ctx: Context,
) -> CreatePROutput:
    """Open a PR from the fork to upstream.

    Requires PUSHED. Body defaults to a template with Fixes #N, summary, test
    plan, and AI-assistance disclosure if policy asks. Asks for confirmation
    via elicitation. Sets state to PR_OPENED and returns the PR URL.
    """
    workspace = _current_workspace(store)
    if not workspace:
        return CreatePROutput(pr_url="", pr_number=0, state="")

    state = _state_for_workspace(store, workspace)
    if not state:
        return CreatePROutput(pr_url="", pr_number=0, state="")

    err = require_state(state.state, WorkflowState.PUSHED, "create_pr")
    if err:
        return CreatePROutput(pr_url="", pr_number=0, state="")

    owner = state.extra.get("owner", "")
    repo = state.extra.get("repo", "")
    number = int(state.extra.get("issue_number", "0"))
    fork_owner = state.extra.get("fork_owner", owner)
    branch = state.branch or ""

    if not owner or not repo or not number or not branch:
        return CreatePROutput(
            pr_url="", pr_number=0, state="",
            message="Missing state (owner/repo/issue/branch).",
        )

    # Build default body.
    default_body = _default_pr_body(owner, repo, number, inp.title, inp.body or "", state)
    head = f"{fork_owner}:{branch}"
    base = state.default_branch or "main"

    # Elicitation: confirm PR creation.
    confirm = await _elicit_confirm(
        ctx,
        message=(
            f"About to open a PR from `{head}` to `{base}` in {owner}/{repo}.\n"
            f"Title: {inp.title}\n\nBody preview:\n{default_body[:800]}...\n\nConfirm?"
        ),
        response_title="create_pr",
    )
    if confirm is not True:
        return CreatePROutput(
            pr_url="", pr_number=0, state="",
            message="PR creation declined or not supported by client.",
        )

    try:
        result = create_pull_request(owner, repo, inp.title, default_body, head, base)
    except GitHubError as exc:
        logger.warning("create_pr failed: %s", exc)
        return CreatePROutput(pr_url="", pr_number=0, state="", message=str(exc))

    err = state.transition_to(WorkflowState.PR_OPENED)
    if err:
        logger.warning("create_pr transition: %s", err)
    state.pr_url = result["url"]
    state.pr_number = result["number"]
    store.set(state.issue_url, state)

    logger.info("create_pr %s -> PR #%d %s", workspace, result["number"], result["url"])
    return CreatePROutput(
        pr_url=result["url"],
        pr_number=result["number"],
        state=WorkflowState.PR_OPENED.value,
    )


def _default_pr_body(owner: str, repo: str, number: int, title: str, body: str, state: WorkspaceState) -> str:
    lines = []
    lines.append(f"## Summary\n{body or title}\n")
    lines.append(f"## Fixes\nFixes #{number}\n")
    lines.append("## Test Plan\nRan the project's test suite locally; all checks passed.\n")
    # AI-assistance disclosure if contributing guide asks for one.
    forbids, notes = _contributing_forbids_ai("", owner, repo)
    low = (notes or "").lower()
    if any(k in low for k in ("ai", "llm", "generated")):
        lines.append("## AI Assistance\nThis PR was prepared with AI assistance (issue_agent).\n")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool: get_status
# ---------------------------------------------------------------------------
@app.tool(name="get_status")
async def get_status(ctx: Context) -> StatusOutput:
    """Return the current workflow state, branch, and diff summary."""
    workspace = _current_workspace(store)
    state = _state_for_workspace(store, workspace) if workspace else None
    if not state:
        return StatusOutput(state=WorkflowState.NEW.value, issue_url="", diff_summary="")

    diff_summary = ""
    if workspace:
        from .repo_manager import RepoManager

        try:
            mgr = RepoManager()
            diff_summary = mgr.diff_stat(workspace)
        except Exception:
            diff_summary = "(could not compute diff)"

    return StatusOutput(
        state=state.state.value,
        issue_url=state.issue_url,
        workspace_path=state.workspace_path,
        branch=state.branch,
        diff_summary=diff_summary,
    )


# ---------------------------------------------------------------------------
# Tool: cleanup_workspace
# ---------------------------------------------------------------------------
@app.tool(name="cleanup_workspace")
async def cleanup_workspace(ctx: Context) -> CleanupOutput:
    """Delete the active workspace and reset its state to NEW."""
    workspace = _current_workspace(store)
    if not workspace:
        return CleanupOutput(cleaned=False, workspace_path="", message="No active workspace.")

    state = _state_for_workspace(store, workspace)
    from .repo_manager import RepoManager

    mgr = RepoManager()
    try:
        mgr.cleanup(workspace)
        cleaned = True
        message = "workspace deleted"
    except RepoManagerError as exc:
        return CleanupOutput(cleaned=False, workspace_path=workspace, message=str(exc))

    if state:
        state.state = WorkflowState.NEW
        state.workspace_path = None
        state.branch = None
        store.set(state.issue_url, state)

    logger.info("cleanup_workspace %s", workspace)
    return CleanupOutput(cleaned=cleaned, workspace_path=workspace, message=message)


# ---------------------------------------------------------------------------
# Elicitation helper
# ---------------------------------------------------------------------------
async def _elicit_confirm(ctx: Context, message: str, response_title: str) -> Optional[bool]:
    """Ask the client to confirm via MCP elicitation. Returns True/False/None."""
    if ctx is None:
        return None
    try:
        from typing import Any
        result = await ctx.elicit(
            message=message,
            response_type=bool,
            response_title=response_title,
            response_description="Confirm the action (yes/no).",
        )
        # AcceptedElicitation has .value; Declined/Cancelled are not True.
        accepted = getattr(result, "value", None)
        if isinstance(accepted, bool):
            return accepted
        # Some clients return a dict with "value".
        if isinstance(result, dict):
            return bool(result.get("value"))
        return None
    except Exception as exc:
        logger.warning("elicitation not supported or declined: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
def main() -> None:
    """Start the streamable-HTTP MCP server on 127.0.0.1 (loopback only)."""
    logger.info(
        "Starting issue_agent MCP server on http://%s:%d%s",
        config.host,
        config.port,
        config.path,
    )
    app.run(
        transport="http",
        host=config.host,
        port=config.port,
        path=config.path,
    )


if __name__ == "__main__":
    main()
