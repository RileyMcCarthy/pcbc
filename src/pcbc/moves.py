"""The one sentence shape every pcbc refusal speaks (E.2), shared by the compiler, the patterns and
the router so every failure reads the same way."""

from __future__ import annotations


def move_line(net: str, what: str, goal: str, blockers: str, *, sep: str = " ") -> str:
    """"<net>: <what> cannot reach <goal>. In the way: <blockers>." — `sep` is what stands between the
    sentences, a space for a one-line report and a newline plus two spaces for a multi-line refusal.

    It used to end in a fifth sentence, the `board.py` or `layout.core.py` edit; that is gone
    (docs/direction.md §7b, owner 2026-10-01: "Remove suggestions in tool, just throw errors if unable
    to complete"): a failure states what failed and why, and deciding the edit is the author's job."""
    return f"{net}: {what} cannot reach {goal}.{sep}In the way: {blockers}."
