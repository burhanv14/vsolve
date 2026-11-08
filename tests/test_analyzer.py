"""Unit tests for the analyzer heuristics (no GitHub network)."""

import pytest

from issue_agent.analyzer import _heuristics
from issue_agent.github_client import IssueData


def _data(**overrides):
    base = dict(
        number=1,
        title="fix a bug",
        state="open",
        locked=False,
        html_url="https://github.com/o/r/issues/1",
        body="",
        created_at="2024-01-01",
        updated_at="2024-01-01",
        closed_at=None,
        labels=[],
        assignees=[],
        author="dev",
        milestone=None,
        comments=[],
        linked_prs=[],
        open_prs_referencing=[],
    )
    base.update(overrides)
    return IssueData(**base)


def test_closed_issue_not_actionable():
    actionable, fixed, claimed, clarif, risk, reasons = _heuristics(_data(state="closed"))
    assert actionable is False
    assert fixed is True
    assert any("closed" in r.lower() for r in reasons)


def test_assigned_issue_not_actionable():
    actionable, fixed, claimed, clarif, risk, reasons = _heuristics(_data(assignees=["alice"]))
    assert actionable is False
    assert claimed == "alice"


def test_wontfix_label_not_actionable():
    actionable, fixed, claimed, clarif, risk, reasons = _heuristics(_data(labels=["wontfix"]))
    assert actionable is False


def test_linked_pr_not_actionable():
    actionable, fixed, claimed, clarif, risk, reasons = _heuristics(
        _data(linked_prs=[{"number": 7, "title": "fix", "state": "open", "html_url": "x", "head": "h", "base": "b"}])
    )
    assert actionable is False


def test_open_pr_referencing_not_actionable():
    actionable, fixed, claimed, clarif, risk, reasons = _heuristics(
        _data(open_prs_referencing=[{"number": 3, "title": "fix", "html_url": "x", "head": "h", "base": "b"}])
    )
    assert actionable is False


def test_clarification_keyword_high_risk():
    actionable, fixed, claimed, clarif, risk, reasons = _heuristics(
        _data(title="RFC: redesign the widget", body="what do you think?")
    )
    assert clarif is True
    assert risk.value == "high"


def test_actionable_labels_low_risk():
    actionable, fixed, claimed, clarif, risk, reasons = _heuristics(
        _data(labels=["good first issue", "bug"], milestone="v1.0")
    )
    assert actionable is True
    assert risk.value == "low"


def test_no_signals_actionable_med():
    actionable, fixed, claimed, clarif, risk, reasons = _heuristics(_data(title="typo in readme"))
    assert actionable is True
    assert risk.value == "med"


def test_locked_not_actionable():
    actionable, fixed, claimed, clarif, risk, reasons = _heuristics(_data(locked=True))
    assert actionable is False
