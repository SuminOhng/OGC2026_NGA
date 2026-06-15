"""Top-level solver orchestration."""

from __future__ import annotations

import copy
import os
import time

from .alns import solve_alns, solve_bootstrap_alns, solve_hierarchical_preference
from .planner import build_initial_solution


def solve(prob_info: dict, timelimit: float = 60) -> dict:
    """Return a solution dictionary for one problem instance.

    A conservative empty-bay sequential planner is always available as a fast
    feasible fallback. The ALNS path can keep it or improve it by official
    objective; organizer greedy is intentionally skipped so decomposition and
    MIP seeds receive the runtime budget.
    """

    safe_timelimit = max(0.0, float(timelimit))
    guard_seconds = min(1.0, max(0.35, safe_timelimit * 0.02))
    deadline = time.monotonic() + max(0.0, safe_timelimit - guard_seconds)
    reference_prob_info = copy.deepcopy(prob_info)
    if len(reference_prob_info.get("blocks", [])) >= 150 and safe_timelimit >= 240.0:
        reference_prob_info["_use_fast_release_seed"] = True
    fallback_solution = build_initial_solution(copy.deepcopy(reference_prob_info), deadline=deadline)
    protected_incumbent = copy.deepcopy(fallback_solution)

    use_simple_alns = os.environ.get("OGC_SIMPLE_ALNS", "").strip() == "1"
    use_full_hierarchical = os.environ.get("OGC_FULL_HIERARCHICAL", "").strip() == "1"

    if (
        not use_simple_alns
        and len(reference_prob_info.get("blocks", [])) >= 150
        and safe_timelimit >= 240.0
    ):
        try:
            if use_full_hierarchical or safe_timelimit >= 1500.0:
                large_solution = solve_hierarchical_preference(
                    copy.deepcopy(reference_prob_info),
                    deadline=deadline,
                )
            else:
                large_solution = solve_bootstrap_alns(
                    copy.deepcopy(reference_prob_info),
                    deadline=deadline,
                )
            return _better_checked_solution(reference_prob_info, protected_incumbent, large_solution)
        except Exception as exc:
            print(f"[Solver] large-instance path unavailable, using ALNS: {exc}")

    try:
        alns_seed = copy.deepcopy(fallback_solution)
        alns_solution = solve_alns(
            copy.deepcopy(reference_prob_info),
            deadline=deadline,
            fallback_solution=alns_seed,
        )
        return _better_checked_solution(reference_prob_info, protected_incumbent, alns_solution)
    except Exception as exc:
        print(f"[Solver] ALNS unavailable, using fallback: {exc}")

    return fallback_solution


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
