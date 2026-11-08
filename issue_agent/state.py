"""Per-workspace workflow state machine.

State machine::

    NEW -> ANALYZED -> PREPARED -> CHECKS_PASSED -> PUSHED -> PR_OPENED

Editing files after CHECKS_PASSED resets state to PREPARED.
State is persisted as JSON inside each workspace (``.issue_agent_state.json``)
and keyed by issue URL.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, Optional


class WorkflowState(str, Enum):
    NEW = "NEW"
    ANALYZED = "ANALYZED"
    PREPARED = "PREPARED"
    CHECKS_PASSED = "CHECKS_PASSED"
    PUSHED = "PUSHED"
    PR_OPENED = "PR_OPENED"


# Valid transitions: from -> set of allowed "to" states.
_TRANSITIONS: Dict[WorkflowState, frozenset[WorkflowState]] = {
    WorkflowState.NEW: {WorkflowState.ANALYZED},
    WorkflowState.ANALYZED: {WorkflowState.ANALYZED, WorkflowState.PREPARED},
    WorkflowState.PREPARED: {WorkflowState.CHECKS_PASSED, WorkflowState.PREPARED},
    WorkflowState.CHECKS_PASSED: {WorkflowState.PUSHED, WorkflowState.PREPARED},
    WorkflowState.PUSHED: {WorkflowState.PR_OPENED},
    WorkflowState.PR_OPENED: {WorkflowState.PR_OPENED},
}


@dataclass
class WorkspaceState:
    issue_url: str
    state: WorkflowState = WorkflowState.NEW
    branch: Optional[str] = None
    workspace_path: Optional[str] = None
    default_branch: Optional[str] = None
    pr_url: Optional[str] = None
    pr_number: Optional[int] = None
    extra: Dict[str, str] = field(default_factory=dict)

    def state_file(self) -> Optional[Path]:
        if self.workspace_path is None:
            return None
        return Path(self.workspace_path) / ".issue_agent_state.json"

    def to_dict(self) -> dict:
        return {
            "issue_url": self.issue_url,
            "state": self.state.value,
            "branch": self.branch,
            "workspace_path": self.workspace_path,
            "default_branch": self.default_branch,
            "pr_url": self.pr_url,
            "pr_number": self.pr_number,
            "extra": self.extra,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "WorkspaceState":
        return cls(
            issue_url=d.get("issue_url", ""),
            state=WorkflowState(d.get("state", WorkflowState.NEW.value)),
            branch=d.get("branch"),
            workspace_path=d.get("workspace_path"),
            default_branch=d.get("default_branch"),
            pr_url=d.get("pr_url"),
            pr_number=d.get("pr_number"),
            extra=d.get("extra", {}) or {},
        )

    def save(self) -> None:
        sf = self.state_file()
        if sf is None:
            return
        sf.parent.mkdir(parents=True, exist_ok=True)
        sf.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")

    @classmethod
    def load(cls, workspace_path: str, issue_url: str) -> "WorkspaceState":
        sf = Path(workspace_path) / ".issue_agent_state.json"
        if sf.exists():
            try:
                return cls.from_dict(json.loads(sf.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError, ValueError):
                pass
        return cls(issue_url=issue_url, workspace_path=workspace_path)

    # ----- transition helpers -----
    def can_transition(self, new_state: WorkflowState) -> bool:
        return new_state in _TRANSITIONS.get(self.state, frozenset())

    def transition_to(self, new_state: WorkflowState) -> str:
        """Validate a transition and return a human message if invalid."""
        if not self.can_transition(new_state):
            return (
                f"Invalid state transition {self.state.value} -> {new_state.value}. "
                f"Allowed from {self.state.value}: "
                + ", ".join(s.value for s in _TRANSITIONS.get(self.state, frozenset()))
            )
        self.state = new_state
        self.save()
        return ""

    def reset_to_prepared(self) -> None:
        """Call when files are edited after CHECKS_PASSED."""
        if self.state == WorkflowState.CHECKS_PASSED:
            self.state = WorkflowState.PREPARED
            self.save()


def allowed_from(state: WorkflowState) -> list[str]:
    return sorted(s.value for s in _TRANSITIONS.get(state, frozenset()))


def require_state(current: WorkflowState, required: WorkflowState, tool_name: str) -> Optional[str]:
    """Return an error message if ``current`` doesn't satisfy ``required``."""
    if current != required:
        return (
            f"{tool_name} requires state {required.value}, but current state is "
            f"{current.value}. Allowed next from {current.value}: "
            + ", ".join(allowed_from(current))
            + "."
        )
    return None
