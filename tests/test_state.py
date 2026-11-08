"""Unit tests for the workflow state machine."""

import json
import tempfile
from pathlib import Path

import pytest

from issue_agent.state import (
    WorkflowState,
    WorkspaceState,
    allowed_from,
    require_state,
)


def test_transition_chain():
    s = WorkspaceState(issue_url="u")
    assert s.state == WorkflowState.NEW
    assert s.transition_to(WorkflowState.ANALYZED) == ""
    assert s.state == WorkflowState.ANALYZED
    assert s.transition_to(WorkflowState.PREPARED) == ""
    assert s.state == WorkflowState.PREPARED
    assert s.transition_to(WorkflowState.CHECKS_PASSED) == ""
    assert s.state == WorkflowState.CHECKS_PASSED
    assert s.transition_to(WorkflowState.PUSHED) == ""
    assert s.state == WorkflowState.PUSHED
    assert s.transition_to(WorkflowState.PR_OPENED) == ""
    assert s.state == WorkflowState.PR_OPENED


def test_invalid_transition_rejected():
    s = WorkspaceState(issue_url="u", state=WorkflowState.NEW)
    err = s.transition_to(WorkflowState.PREPARED)
    assert err != ""
    assert "NEW" in err
    assert s.state == WorkflowState.NEW  # unchanged


def test_reset_to_prepared_from_checks_passed():
    s = WorkspaceState(issue_url="u", state=WorkflowState.CHECKS_PASSED)
    s.reset_to_prepared()
    assert s.state == WorkflowState.PREPARED


def test_reset_to_prepared_noop_when_push():
    s = WorkspaceState(issue_url="u", state=WorkflowState.PUSHED)
    s.reset_to_prepared()
    assert s.state == WorkflowState.PUSHED


def test_require_state_error():
    err = require_state(WorkflowState.NEW, WorkflowState.ANALYZED, "prepare_workspace")
    assert err is not None
    assert "prepare_workspace" in err
    assert require_state(WorkflowState.ANALYZED, WorkflowState.ANALYZED, "x") is None


def test_allowed_from():
    assert "PREPARED" in allowed_from(WorkflowState.ANALYZED)
    assert "PUSHED" in allowed_from(WorkflowState.CHECKS_PASSED)
    assert "PR_OPENED" in allowed_from(WorkflowState.PUSHED)


def test_persist_and_load_json(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    s = WorkspaceState(issue_url="u", state=WorkflowState.PREPARED, branch="fix/1-x", workspace_path=str(ws))
    s.save()
    loaded = WorkspaceState.load(str(ws), "u")
    assert loaded.state == WorkflowState.PREPARED
    assert loaded.branch == "fix/1-x"
    assert loaded.workspace_path == str(ws)
    # raw JSON check
    data = json.loads((ws / ".issue_agent_state.json").read_text())
    assert data["state"] == "PREPARED"
