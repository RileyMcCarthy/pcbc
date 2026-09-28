"""The one sentence shape every pcbc refusal speaks (E.2), shared by the compiler, the patterns and
the router so an AI that can act on one can act on the other."""

from __future__ import annotations


def move_line(net: str, what: str, goal: str, blockers: str, fixes: str = "", *, sep: str = " ") -> str:
    """"<net>: <what> cannot reach <goal>. In the way: <blockers>. <fixes>" — `sep` is what stands
    between the sentences, a space for a one-line report and a newline plus two spaces for a
    multi-line refusal. `fixes` always ends in a `board.py` or `layout.core.py` edit; a failure line
    that does not is a dead end, which is the whole reason this function exists rather than an
    f-string per call site."""
    tail = f"{sep}{fixes}" if fixes else ""
    return f"{net}: {what} cannot reach {goal}.{sep}In the way: {blockers}.{tail}"
