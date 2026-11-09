"""Issue actionability analyzer.

Heuristics-based verdict with an optional MCP sampling fallback for judgment
calls. Falls back cleanly when the host client doesn't support sampling.
"""

from __future__ import annotations

from typing import Optional, Tuple

from .github_client import IssueData, gather_issue_data
from .models import AnalyzeIssueInput, AnalyzeIssueOutput, IssueActionable


# Labels that strongly suggest the issue is NOT actionable right now.
NON_ACTIONABLE_LABELS = {
    "wontfix",
    "duplicate",
    "invalid",
    "wont fix",
    "won't fix",
    "not planned",
    "triage",
    "needs-triage",
    "question",
    "help wanted",  # sometimes, but keep as signal
}

# Labels that suggest it IS actionable.
ACTIONABLE_LABELS = {
    "good first issue",
    "good-first-issue",
    "beginner",
    "easy",
    "bug",
    "enhancement",
    "feature",
    "performance",
    "security",
}

# Keywords in title/body suggesting the maintainers want discussion first.
CLARIFICATION_KEYWORDS = [
    "discuss",
    "rfc",
    "request for comments",
    "needs discussion",
    "needs clarification",
    "unclear",
    "what do you think",
    "thoughts?",
    "design",
    "proposal",
]

STALE_KEYWORDS = ["stale", "rot", "abandoned", "no recent activity"]


def _label_set(labels) -> set:
    return {l.lower() for l in labels}


def _heuristics(data: IssueData) -> Tuple[bool, bool, Optional[str], bool, IssueActionable, list]:
    """Return (actionable, already_fixed, claimed_by, needs_clarification, risk, reasons)."""
    reasons: list = []
    labels = _label_set(data.labels)

    already_fixed = data.state == "closed"
    claimed_by = data.assignees[0] if data.assignees else None

    # --- already closed / fixed ---
    if already_fixed:
        reasons.append(f"Issue #{data.number} is closed (already fixed or not actionable).")
        return False, True, claimed_by, False, IssueActionable.low, reasons

    # --- claimed ---
    if claimed_by:
        reasons.append(f"Issue is assigned/claimed by @{claimed_by}.")
        return False, False, claimed_by, False, IssueActionable.med, reasons

    # --- non-actionable labels ---
    na = labels & NON_ACTIONABLE_LABELS
    if na:
        reasons.append(f"Has non-actionable labels: {', '.join(sorted(na))}.")
        return False, False, None, False, IssueActionable.low, reasons

    # --- linked or open PR already references the issue ---
    if data.linked_prs:
        reasons.append(
            "Issue body already references PR(s): "
            + ", ".join(f"#{p['number']}" for p in data.linked_prs)
            + ". Already being addressed."
        )
        return False, False, None, False, IssueActionable.low, reasons
    if data.open_prs_referencing:
        reasons.append(
            "Open PR(s) already reference this issue: "
            + ", ".join(f"#{p['number']}" for p in data.open_prs_referencing)
            + ". Already being addressed."
        )
        return False, False, None, False, IssueActionable.low, reasons

    # --- locked ---
    if data.locked:
        reasons.append("Issue is locked (no further contributions accepted).")
        return False, False, None, False, IssueActionable.low, reasons

    # --- clarification needed ---
    text = f"{data.title}\n{data.body}".lower()
    if any(k in text for k in CLARIFICATION_KEYWORDS):
        needs_clarification = True
        reasons.append("Title/body suggests maintainer discussion is needed first.")
    else:
        needs_clarification = False

    # --- risk ---
    risk = IssueActionable.med
    if labels & ACTIONABLE_LABELS:
        reasons.append(f"Has actionable labels: {', '.join(sorted(labels & ACTIONABLE_LABELS))}.")
        risk = IssueActionable.low
    if data.milestone:
        reasons.append(f"Milestoned for '{data.milestone}'.")
        risk = IssueActionable.low
    if data.comments:
        reasons.append(f"{len(data.comments)} comment(s) — some triage has occurred.")
    if needs_clarification:
        risk = IssueActionable.high

    actionable = not needs_clarification
    return actionable, False, None, needs_clarification, risk, reasons


def analyze(data: IssueData, use_sampling: bool = False) -> AnalyzeIssueOutput:
    """Produce an AnalyzeIssueOutput from fetched IssueData."""
    print(f"[issue_agent] [analyzer] Analyzing issue #{data.number}: '{data.title}'", flush=True)
    print(f"[issue_agent] [analyzer]   state={data.state}, labels={data.labels}, assignees={data.assignees}, locked={data.locked}", flush=True)
    print(f"[issue_agent] [analyzer]   comments={len(data.comments)}, linked_prs={len(data.linked_prs)}, open_prs_referencing={len(data.open_prs_referencing)}", flush=True)

    (
        actionable,
        already_fixed,
        claimed_by,
        needs_clarification,
        risk,
        reasons,
    ) = _heuristics(data)

    contributing_notes = getattr(data, "_contributing_notes_raw", "") or ""

    # Optional MCP sampling for a judgment call on borderline cases.
    sampled = False
    if use_sampling and not actionable and needs_clarification:
        try:
            # This is a no-op unless the caller injects a sampler.
            sampled = _maybe_sample(data)
        except Exception:
            sampled = False
    if sampled:
        reasons.append("MCP sampling used for final judgment.")

    if not reasons:
        reasons.append("No disqualifying signals found; issue appears actionable.")

    print(f"[issue_agent] [analyzer] Result: actionable={actionable}, already_fixed={already_fixed}, claimed_by={claimed_by}, needs_clarification={needs_clarification}, risk={risk}", flush=True)
    print(f"[issue_agent] [analyzer]   reasons: {reasons}", flush=True)

    return AnalyzeIssueOutput(
        actionable=actionable,
        already_fixed=already_fixed,
        claimed_by=claimed_by,
        needs_clarification=needs_clarification,
        risk=risk,
        reasons=reasons,
        contributing_notes=contributing_notes.strip(),
    )


def _maybe_sample(data: IssueData) -> bool:
    """Placeholder for MCP sampling. Returns False (sampling not wired here).

    The server tool wraps this with ``ctx.elicit`` / sampling when available.
    """
    return False
