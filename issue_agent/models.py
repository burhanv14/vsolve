"""Pydantic models for MCP tool input/output.

All tools return structured pydantic models so host LLMs receive predictable,
typed results rather than free-form text.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional, List

from pydantic import BaseModel, Field, HttpUrl


class IssueActionable(str, Enum):
    low = "low"
    med = "med"
    high = "high"


class State(str, Enum):
    NEW = "NEW"
    ANALYZED = "ANALYZED"
    PREPARED = "PREPARED"
    CHECKS_PASSED = "CHECKS_PASSED"
    PUSHED = "PUSHED"
    PR_OPENED = "PR_OPENED"


# ---------------------------------------------------------------------------
# analyze_issue
# ---------------------------------------------------------------------------
class AnalyzeIssueInput(BaseModel):
    url: str = Field(description="GitHub issue URL, e.g. https://github.com/owner/repo/issues/42")


class AnalyzeIssueOutput(BaseModel):
    actionable: bool = Field(description="Whether the issue appears actionable")
    already_fixed: bool = Field(description="Whether the issue is already fixed/closed")
    claimed_by: Optional[str] = Field(
        default=None, description="Login of the assignee, if claimed"
    )
    needs_clarification: bool = Field(
        default=False, description="Whether the issue needs more discussion first"
    )
    risk: IssueActionable = Field(
        default=IssueActionable.med, description="low | med | high"
    )
    reasons: List[str] = Field(default_factory=list, description="Human-readable reasons")
    contributing_notes: str = Field(
        default="", description="Excerpt from CONTRIBUTING.md relevant to contributions"
    )


# ---------------------------------------------------------------------------
# prepare_workspace
# ---------------------------------------------------------------------------
class PrepareWorkspaceInput(BaseModel):
    url: str = Field(description="GitHub issue URL")


class PrepareWorkspaceOutput(BaseModel):
    workspace_path: str = Field(description="Absolute path to the prepared workspace")
    branch: str = Field(description="New branch name, e.g. fix/42-slug")
    default_branch: str = Field(description="Upstream default branch, e.g. main")
    language: str = Field(description="Detected primary language")
    test_command: Optional[str] = Field(default=None)
    lint_command: Optional[str] = Field(default=None)
    format_command: Optional[str] = Field(default=None)
    repo_map: str = Field(description="Tree map (depth 2) of the cloned repo")


# ---------------------------------------------------------------------------
# get_context
# ---------------------------------------------------------------------------
class GetContextInput(BaseModel):
    query: Optional[str] = Field(default=None, description="ripgrep search query")
    paths: Optional[List[str]] = Field(
        default=None, description="File paths to slice (with line numbers)"
    )
    max_bytes: int = Field(default=50_000, description="Max bytes to return")


class FileSlice(BaseModel):
    path: str
    start_line: int
    end_line: int
    content: str


class GetContextOutput(BaseModel):
    matches: List[str] = Field(
        default_factory=list, description="ripgrep matches (file:line:col:text)"
    )
    slices: List[FileSlice] = Field(default_factory=list, description="Requested file slices")
    truncated: bool = Field(default=False)


# ---------------------------------------------------------------------------
# apply_patch
# ---------------------------------------------------------------------------
class ApplyPatchInput(BaseModel):
    diff: str = Field(description="Unified diff to apply to the workspace")


class ApplyPatchOutput(BaseModel):
    applied: bool = Field(description="Whether the patch applied cleanly")
    files_changed: List[str] = Field(default_factory=list)
    message: str = Field(default="")


# ---------------------------------------------------------------------------
# run_checks
# ---------------------------------------------------------------------------
class CheckResult(BaseModel):
    command: str
    passed: bool
    return_code: int
    log: str
    truncated: bool = False


class RunChecksInput(BaseModel):
    command: Optional[str] = Field(
        default=None, description="Optional single command to run instead of detected checks"
    )


class RunChecksOutput(BaseModel):
    checks_passed: bool = Field(default=False)
    results: List[CheckResult] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# commit_and_push
# ---------------------------------------------------------------------------
class CommitPushInput(BaseModel):
    message: str = Field(description="Commit message")


class CommitPushOutput(BaseModel):
    committed: bool
    pushed: bool
    branch: str
    diff_stat: str
    message: str = ""


# ---------------------------------------------------------------------------
# create_pr
# ---------------------------------------------------------------------------
class CreatePRInput(BaseModel):
    title: str
    body: Optional[str] = None


class CreatePROutput(BaseModel):
    pr_url: str
    pr_number: int
    state: str = "PR_OPENED"


# ---------------------------------------------------------------------------
# status / cleanup
# ---------------------------------------------------------------------------
class StatusOutput(BaseModel):
    state: str
    issue_url: str
    workspace_path: Optional[str] = None
    branch: Optional[str] = None
    diff_summary: str = ""


class CleanupOutput(BaseModel):
    cleaned: bool
    workspace_path: str
    message: str = ""
