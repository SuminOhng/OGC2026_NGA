"""Top-level solver orchestration."""

from __future__ import annotations

import time

from .planner import build_initial_solution


def solve(prob_info: dict, timelimit: float = 60) -> dict:
    """Return a solution dictionary for one problem instance.

    The current implementation intentionally starts with a conservative
    empty-bay sequential planner. It gives the package a working submission
    shape while leaving room for stronger planning, repair, and search modules.
    """

    safe_timelimit = max(0.0, float(timelimit))
    deadline = time.monotonic() + safe_timelimit
    return build_initial_solution(prob_info, deadline=deadline)
