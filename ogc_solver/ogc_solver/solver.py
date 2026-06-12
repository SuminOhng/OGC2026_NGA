"""Top-level solver orchestration."""

from __future__ import annotations

import contextlib
import copy
import io
import time

from .alns import solve_alns
from .planner import build_initial_solution


def solve(prob_info: dict, timelimit: float = 60) -> dict:
    """Return a solution dictionary for one problem instance.

    A conservative empty-bay sequential planner is always available as a fast
    feasible fallback. Small instances also get the organizer greedy as an
    incumbent; the ALNS path can keep it or improve it by official objective.
    """

    safe_timelimit = max(0.0, float(timelimit))
    guard_seconds = min(1.0, max(0.35, safe_timelimit * 0.02))
    deadline = time.monotonic() + max(0.0, safe_timelimit - guard_seconds)
    reference_prob_info = copy.deepcopy(prob_info)
    if len(reference_prob_info.get("blocks", [])) >= 150 and safe_timelimit >= 240.0:
        reference_prob_info["_use_fast_release_seed"] = True
    greedy_solution = _provided_greedy_seed(reference_prob_info, safe_timelimit, deadline)
    fallback_solution = build_initial_solution(copy.deepcopy(reference_prob_info), deadline=deadline)
    fallback_solution = _better_checked_solution(reference_prob_info, fallback_solution, greedy_solution)
    protected_incumbent = copy.deepcopy(fallback_solution)

    try:
        alns_seed = copy.deepcopy(fallback_solution)
        if len(reference_prob_info.get("blocks", [])) >= 150 and safe_timelimit >= 260.0:
            mid_deadline = min(deadline, time.monotonic() + 180.0)
            if mid_deadline < deadline - 30.0:
                mid_solution = solve_alns(
                    copy.deepcopy(reference_prob_info),
                    deadline=mid_deadline,
                    fallback_solution=alns_seed,
                )
                protected_incumbent = _better_checked_solution(
                    reference_prob_info, protected_incumbent, mid_solution
                )
                alns_seed = copy.deepcopy(protected_incumbent)

        alns_solution = solve_alns(
            copy.deepcopy(reference_prob_info),
            deadline=deadline,
            fallback_solution=alns_seed,
        )
        return _better_checked_solution(reference_prob_info, protected_incumbent, alns_solution)
    except Exception as exc:
        print(f"[Solver] ALNS unavailable, using fallback: {exc}")

    return fallback_solution


def _provided_greedy_seed(prob_info: dict, timelimit: float, deadline: float) -> dict | None:
    """Use the organizer greedy as a small-instance incumbent when it fits."""

    if len(prob_info.get("blocks", [])) > 120:
        return None

    remaining = deadline - time.monotonic()
    if remaining < 5.0:
        return None

    try:
        from . import provided_greedy

        greedy_prob_info = copy.deepcopy(prob_info)
        with contextlib.redirect_stdout(io.StringIO()):
            return provided_greedy.greedyalgorithm(greedy_prob_info, max(1.0, timelimit))
    except Exception:
        return None


def _better_checked_solution(prob_info: dict, incumbent: dict, candidate: dict | None) -> dict:
    if candidate is None:
        return incumbent

    try:
        from utils import check_feasibility

        incumbent_result = check_feasibility(prob_info, incumbent)
        candidate_result = check_feasibility(prob_info, candidate)
    except Exception:
        return incumbent

    if not candidate_result.get("feasible"):
        return incumbent
    if not incumbent_result.get("feasible"):
        return candidate

    incumbent_objective = incumbent_result.get("objective")
    candidate_objective = candidate_result.get("objective")
    if candidate_objective is None:
        return incumbent
    if incumbent_objective is None or float(candidate_objective) < float(incumbent_objective):
        return candidate
    return incumbent
