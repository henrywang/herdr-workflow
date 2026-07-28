"""The prompts wq sends, and the convergence rule they enforce.

These are **core product, not private configuration.** The review protocol in particular is
the most reusable idea in the project, so it ships as a default rather than living in
someone's dotfiles -- while staying overridable for people who want a different house
style.

Two rules are worth stating because they are easy to break by accident:

**Reviewers get a path, never a pasted plan or diff.** Passing content between panes is the
single largest token sink in a loop like this, and it grows with every round.

**Convergence is a grep, not an interpretation.** The reviewer ends its file with exactly
one machine-readable line, so nothing has to infer agreement from prose.
"""

from __future__ import annotations

import re
from pathlib import Path

VERDICT_APPROVED = "VERDICT: APPROVED"
VERDICT_CHANGES = "VERDICT: CHANGES"

# Anchored and whole-line: a reviewer that merely *discusses* approval in its prose has not
# approved anything. Anything that is not an explicit approval means "keep going".
_APPROVED = re.compile(rf"^{re.escape(VERDICT_APPROVED)}[ \t]*$", re.MULTILINE)

REVIEW_PROTOCOL = (
    "Classify each finding as BLOCKING or NON-BLOCKING. Be adversarial: your job is to "
    "find what is wrong, not to be agreeable. End the file with exactly one line, no other "
    f'text after it: "{VERDICT_APPROVED}" if there are no BLOCKING findings, otherwise '
    f'"{VERDICT_CHANGES}".'
)

PLAN_SECTIONS = "Objective, Findings, Approach, Files Affected, Validation, Risks, Open Questions"


def approved(review_text: str) -> bool:
    """Did the reviewer sign off?

    Only an exact `VERDICT: APPROVED` line counts. Treating anything ambiguous as approval
    would quietly convert a disagreement into a merge.
    """
    return _APPROVED.search(review_text) is not None


def approved_file(path: Path) -> bool:
    try:
        return approved(path.read_text())
    except OSError:
        return False


def draft_plan(request_file: Path, plan_file: Path) -> str:
    return (
        f"Write an implementation plan for the request in {request_file}. "
        "Investigate before proposing. "
        f"Write the plan to {plan_file} as markdown with sections: {PLAN_SECTIONS}. "
        "Write the file — do not print the plan in chat."
    )


def review_plan(plan_file: Path, request_file: Path, review_file: Path) -> str:
    return (
        f"Review the plan at {plan_file} against the request in {request_file}. "
        f"Write your review to {review_file}. {REVIEW_PROTOCOL}"
    )


def revise_plan(review_file: Path, plan_file: Path) -> str:
    return (
        f"The reviewer raised findings in {review_file}. Address every BLOCKING finding and "
        f"update {plan_file} in place. If you disagree with a finding, say so in the Open "
        "Questions section rather than silently ignoring it."
    )
