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
_APPROVED = re.compile(rf"(?:^|\r?\n){re.escape(VERDICT_APPROVED)}\Z")

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
    return _APPROVED.search(review_text.rstrip()) is not None


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


def implement(plan_file: Path, worktree: Path, branch: str) -> str:
    """The build's opening turn.

    "Commit your work" is load-bearing rather than tidiness: the review reads
    `git diff <base>...HEAD`, so uncommitted edits are invisible to it. "Do not push and do
    not open a pull request" is equally deliberate -- `wq ship` owns that, and an agent that
    opens its own PR mid-review puts unreviewed work in front of people.
    """
    return (
        f"Implement the approved plan at {plan_file} in this worktree ({worktree}, branch "
        f"{branch}). Follow the repository's own conventions and instruction files. Run the "
        "tests. Commit your work with a clear message. Do not push and do not open a pull "
        "request."
    )


def review_code(diff_file: Path, plan_file: Path, review_file: Path) -> str:
    """Review the diff, not the repository.

    A reviewer told to read the tree re-reads it every round, which triples the cost of a
    build for no extra signal. The escape hatch is explicit so a change that genuinely
    cannot be judged from the diff still gets judged properly.
    """
    return (
        f"Review the diff at {diff_file} against the plan at {plan_file}. Read files from "
        "the worktree only when the diff alone is not enough to judge a change. Write your "
        f"review to {review_file}. {REVIEW_PROTOCOL}"
    )


def revise_code(comment: str) -> str:
    return (
        f"Change request: {comment}\n\n"
        "Make that change in this worktree, following the repository's own conventions. "
        "Run the tests. Commit. Do not push and do not open a pull request."
    )


def review_revision(
    delta_file: Path, comment: str, diff_file: Path, plan_file: Path, review_file: Path
) -> str:
    """Scope the reviewer by instruction, not by withholding the rest of the diff.

    A reviewer given only the delta is *structurally* blind to the call site the delta
    forgot to update -- the regression lives outside the hunk that caused it. So it gets
    the whole change for context and is told which part it is judging.
    """
    return (
        f'Review {delta_file} — the change just made in response to: "{comment}". '
        f"{diff_file} is the full change for context and {plan_file} is the plan. "
        "Judge the delta. Raise findings outside it only where the delta caused them. "
        f"Write your review to {review_file}. {REVIEW_PROTOCOL}"
    )


def fix_findings(review_file: Path) -> str:
    return (
        f"The reviewer raised findings in {review_file}. Fix every BLOCKING finding, re-run "
        "the tests, and commit. If you disagree with a finding, reply explaining why rather "
        "than silently ignoring it."
    )
