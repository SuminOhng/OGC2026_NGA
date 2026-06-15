"""Access-aware temporal ALNS prototype.

This module intentionally does not call the organizer-provided greedy baseline.
It builds its own feasible incumbent, then repeatedly removes difficult blocks
and reinserts them with local geometry/crane checks.
"""

from __future__ import annotations

import math
import random
import re
import time
from dataclasses import dataclass
from typing import Iterable

from ..fast_feasibility import FastFeasibilityOracle
from ..planner import build_initial_solution
from ..state import Placement, lower_left_integer_position, orientation_bbox
from ..stabilize import stabilize_solution
from ..subsolvers import (
    build_edd_master_seed,
    build_hierarchical_seed,
    early_exit_protection_removal,
    should_run_schedule_polish,
)

_BLOCK_RE = re.compile(r"block\s+(\d+)")
_COLLISION_CACHE: dict[tuple, bool] = {}
_MAX_COLLISION_CACHE_SIZE = 200_000
_CANDIDATE_POSITION_LIMIT = 80
_FULL_CHECK_INTERVAL = 50
_ORACLE_VERIFY_INTERVAL = 10


@dataclass(frozen=True)
class SearchResult:
    solution: dict
    feasible: bool
    objective: float | None


def _trace_alns_event(event: str, **payload) -> None:
    import json

    record = {"event": event}
    for key, value in payload.items():
        if isinstance(value, float):
            record[key] = round(value, 3)
        else:
            record[key] = value
    print("[ALNS_TRACE] " + json.dumps(record, sort_keys=True), flush=True)


def solve_alns(
    prob_info: dict,
    deadline: float,
    fallback_solution: dict | None = None,
    *,
    use_internal_seeds: bool = True,
) -> dict:
    """Run a small access-aware ALNS loop and return the best feasible solution."""

    from utils import check_feasibility
    import os

    _COLLISION_CACHE.clear()
    started_at = time.monotonic()
    total_budget = max(0.0, deadline - started_at)
    trace_enabled = bool(os.environ.get("OGC_ALNS_TRACE"))
    oracle = FastFeasibilityOracle(prob_info, verify_interval=_ORACLE_VERIFY_INTERVAL)
    _set_candidate_position_limit(total_budget, len(prob_info["blocks"]))
    fallback_solution = fallback_solution or build_initial_solution(prob_info, deadline)
    fallback_result = check_feasibility(prob_info, fallback_solution)
    best = SearchResult(
        solution=fallback_solution,
        feasible=bool(fallback_result.get("feasible")),
        objective=fallback_result.get("objective"),
    )

    reserve_seconds = max(0.25, min(12.0, (deadline - time.monotonic()) * 0.02))
    if len(prob_info["blocks"]) >= 150 and total_budget >= 500.0:
        reserve_seconds = max(reserve_seconds, 30.0)
    search_deadline = deadline - reserve_seconds
    large_handoff_mode = not use_internal_seeds and len(prob_info["blocks"]) >= 150 and total_budget >= 60.0
    if trace_enabled:
        _trace_alns_event(
            "budget",
            total_budget=total_budget,
            reserve_seconds=reserve_seconds,
            search_budget=max(0.0, search_deadline - started_at),
            n_blocks=len(prob_info["blocks"]),
            n_bays=len(prob_info["bays"]),
            use_internal_seeds=use_internal_seeds,
            large_handoff_mode=large_handoff_mode,
        )
    if use_internal_seeds and total_budget >= 120.0:
        seed_budget = _seed_phase_budget(total_budget, len(prob_info["blocks"]))
        seed_phase_deadline = min(search_deadline, started_at + seed_budget)
        seed_build_deadline = started_at + seed_budget * 0.85
    elif use_internal_seeds:
        seed_phase_deadline = deadline
        seed_build_deadline = search_deadline
    else:
        seed_phase_deadline = started_at
        seed_build_deadline = started_at
    rng = random.Random(_stable_seed(prob_info))

    if use_internal_seeds:
        for seed_solution in _initial_seeds(prob_info, seed_build_deadline, seed_phase_deadline, total_budget):
            seed_result = check_feasibility(prob_info, seed_solution)
            if seed_result.get("feasible"):
                best = _better(best, SearchResult(seed_solution, True, seed_result["objective"]))

    use_polishing = should_run_schedule_polish(total_budget)
    allow_initial_polish = use_polishing and use_internal_seeds
    initial_polish_deadline = _initial_polish_deadline(total_budget, search_deadline)
    if allow_initial_polish and best.feasible and time.monotonic() < initial_polish_deadline - 0.5:
        compressed = _compress_solution(prob_info, best.solution, initial_polish_deadline)
        compressed_result = check_feasibility(prob_info, compressed)
        if compressed_result.get("feasible"):
            best = _better(best, SearchResult(compressed, True, compressed_result["objective"]))

    current = best.solution
    current_obj = float(best.objective or math.inf)
    iteration = 0
    temperature = max(1.0, current_obj * 0.01 if math.isfinite(current_obj) else 1.0)
    use_early_exit_destroy = len(prob_info["blocks"]) >= 150
    use_adaptive_ops = total_budget >= 120.0 and (len(prob_info["blocks"]) < 150 or large_handoff_mode)
    operator_weights = _initial_operator_weights(use_early_exit_destroy, total_budget, len(prob_info["blocks"]))
    operator_scores = {name: 0.0 for name in operator_weights}
    operator_counts = {name: 0 for name in operator_weights}
    local_checks = 0
    full_checks = 0
    search_started_at = time.monotonic()
    while time.monotonic() < search_deadline - 0.5:
        iteration += 1
        assignments = _assignments_from_solution(current)
        if not assignments:
            break

        use_preference_repair = False
        if use_adaptive_ops:
            operator = _choose_operator(operator_weights, rng)
            removed = _apply_destroy_operator(operator, prob_info, assignments, rng)
        else:
            operator = "fixed"
            remove_count = 2
            if iteration % 7 == 0:
                removed = _random_removal(assignments, remove_count, rng)
                use_preference_repair = False
            elif use_early_exit_destroy and iteration % 11 == 3:
                removed = _worst_preference_removal(prob_info, assignments, remove_count)
                use_preference_repair = True
            elif use_early_exit_destroy and iteration % 9 == 1:
                removed = early_exit_protection_removal(prob_info, assignments, remove_count)
                use_preference_repair = False
            elif iteration % 5 == 1:
                removed = _access_blocker_removal(prob_info, assignments, remove_count)
                use_preference_repair = False
            else:
                removed = _worst_tardiness_removal(prob_info, assignments, remove_count)
                use_preference_repair = False

        candidate = _repair_removed(
            prob_info,
            assignments,
            removed,
            search_deadline,
            use_regret=(iteration % 6 == 2),
            use_preference_bias=use_preference_repair or operator.startswith("preference"),
        )
        if candidate is None:
            if use_adaptive_ops:
                operator_counts[operator] += 1
            continue

        result, used_full_check = _candidate_result_with_periodic_full_check(
            prob_info,
            assignments,
            candidate,
            removed,
            iteration,
            check_feasibility,
            oracle=oracle,
        )
        if used_full_check:
            full_checks += 1
        else:
            local_checks += 1
        if not result.get("feasible"):
            if use_adaptive_ops:
                operator_counts[operator] += 1
            continue

        candidate_obj = float(result["objective"])
        delta = candidate_obj - current_obj
        accept = delta < 0 or rng.random() < math.exp(-max(0.0, delta) / max(1.0, temperature))
        if accept:
            current = candidate
            current_obj = candidate_obj

        previous_best = best.objective
        best = _better(best, SearchResult(candidate, True, candidate_obj))
        if use_adaptive_ops:
            operator_counts[operator] += 1
            if previous_best is None or candidate_obj < previous_best:
                operator_scores[operator] += 12.0
            elif delta < 0:
                operator_scores[operator] += 6.0
            elif accept:
                operator_scores[operator] += 1.0
            if iteration % 25 == 0:
                _update_operator_weights(operator_weights, operator_scores, operator_counts)
        temperature *= 0.995

    if trace_enabled:
        search_seconds = time.monotonic() - search_started_at
        _trace_alns_event(
            "search",
            total_budget=total_budget,
            elapsed=time.monotonic() - started_at,
            seed_seconds=search_started_at - started_at,
            search_seconds=search_seconds,
            iterations=iteration,
            iterations_per_second=iteration / search_seconds if search_seconds > 0 else 0.0,
            avg_iteration_seconds=search_seconds / iteration if iteration else None,
            adaptive_ops=use_adaptive_ops,
            operator_counts=operator_counts if use_adaptive_ops else None,
            local_checks=local_checks,
            full_checks=full_checks,
            best_objective=best.objective,
            **oracle.summary(),
        )

    if use_polishing and best.feasible and time.monotonic() < deadline - 0.5:
        compressed = _compress_solution(prob_info, best.solution, deadline)
        compressed_result = check_feasibility(prob_info, compressed)
        if compressed_result.get("feasible"):
            best = _better(best, SearchResult(compressed, True, compressed_result["objective"]))

    if best.feasible and time.monotonic() < deadline - 0.5:
        best_result = check_feasibility(prob_info, best.solution)
        polish_deadline = deadline - 0.35
        preference_attempted = False
        preference_budget_ok = total_budget >= 240.0
        preference_seed_ok = True
        preference_obj1_ok = _obj1_low_enough_for_secondary(best_result)
        preference_time_ok = time.monotonic() < polish_deadline - 2.5
        if trace_enabled:
            _trace_alns_event(
                "preference_polish_gate",
                phase="early",
                budget_ok=preference_budget_ok,
                seed_ok=preference_seed_ok,
                obj1_ok=preference_obj1_ok,
                time_ok=preference_time_ok,
                remaining_seconds=polish_deadline - time.monotonic(),
                objective=best_result.get("objective"),
                obj1=best_result.get("obj1"),
                obj2=best_result.get("obj2"),
                obj3=best_result.get("obj3"),
            )
        if preference_budget_ok and preference_seed_ok and preference_obj1_ok and preference_time_ok:
            preference_attempted = True
            preferred, preferred_result = _objective_safe_preference_polish(
                prob_info,
                best.solution,
                best_result,
                min(polish_deadline, time.monotonic() + 6.0),
                trace_enabled=trace_enabled,
            )
            if _objective_safe_secondary_better(best_result, preferred_result):
                best = SearchResult(preferred, True, preferred_result["objective"])
                best_result = preferred_result
        polished, polished_result = _objective_safe_obj1_polish(
            prob_info,
            best.solution,
            best_result,
            polish_deadline,
            total_budget=total_budget,
        )
        if _objective_safe_obj1_better(best_result, polished_result):
            best = SearchResult(polished, True, polished_result["objective"])
            best_result = polished_result
        if total_budget >= 240.0 and time.monotonic() < polish_deadline - 0.8:
            shifted, shifted_result = _objective_safe_left_shift_polish(
                prob_info,
                best.solution,
                best_result,
                polish_deadline,
            )
            if _objective_safe_obj1_better(best_result, shifted_result):
                best = SearchResult(shifted, True, shifted_result["objective"])
                best_result = shifted_result
            if time.monotonic() < polish_deadline - 3.0:
                clustered, clustered_result = _objective_safe_cluster_left_shift_polish(
                    prob_info,
                    best.solution,
                    best_result,
                    polish_deadline,
                )
                if _objective_safe_obj1_better(best_result, clustered_result):
                    best = SearchResult(clustered, True, clustered_result["objective"])
                    best_result = clustered_result
            preference_budget_ok = total_budget >= 240.0
            preference_seed_ok = True
            preference_obj1_ok = _obj1_low_enough_for_secondary(best_result)
            preference_time_ok = time.monotonic() < polish_deadline - 2.5
            if trace_enabled:
                _trace_alns_event(
                    "preference_polish_gate",
                    phase="late",
                    budget_ok=preference_budget_ok,
                    seed_ok=preference_seed_ok,
                    obj1_ok=preference_obj1_ok,
                    time_ok=preference_time_ok,
                    remaining_seconds=polish_deadline - time.monotonic(),
                    objective=best_result.get("objective"),
                    obj1=best_result.get("obj1"),
                    obj2=best_result.get("obj2"),
                    obj3=best_result.get("obj3"),
                )
            if (
                not preference_attempted
                and preference_budget_ok
                and preference_seed_ok
                and preference_obj1_ok
                and preference_time_ok
            ):
                preferred, preferred_result = _objective_safe_preference_polish(
                    prob_info,
                    best.solution,
                    best_result,
                    polish_deadline,
                    trace_enabled=trace_enabled,
                )
                if _objective_safe_secondary_better(best_result, preferred_result):
                    best = SearchResult(preferred, True, preferred_result["objective"])
                    best_result = preferred_result
            if (
                total_budget >= 500.0
                and float(best_result.get("obj1") or math.inf) <= 25.0
                and time.monotonic() < polish_deadline - 3.0
            ):
                due_window, due_window_result = _final_due_window_relocation_polish(
                    prob_info,
                    best.solution,
                    best_result,
                    polish_deadline,
                    trace_enabled=trace_enabled,
                )
                if _objective_safe_obj1_better(best_result, due_window_result):
                    best = SearchResult(due_window, True, due_window_result["objective"])
                    best_result = due_window_result
            if (
                total_budget >= 1000.0
                and float(best_result.get("obj1") or math.inf) <= 1e-6
                and time.monotonic() < polish_deadline - 3.0
            ):
                exact_obj3, exact_obj3_result = _zero_tardiness_obj3_recovery_polish(
                    prob_info,
                    best.solution,
                    best_result,
                    polish_deadline,
                    trace_enabled=trace_enabled,
                )
                if _objective_safe_secondary_better(best_result, exact_obj3_result):
                    best = SearchResult(exact_obj3, True, exact_obj3_result["objective"])
                    best_result = exact_obj3_result

    final_result = check_feasibility(prob_info, best.solution)
    if trace_enabled:
        elapsed = time.monotonic() - started_at
        _trace_alns_event(
            "final",
            total_budget=total_budget,
            elapsed=elapsed,
            iterations=iteration,
            iterations_per_second=iteration / elapsed if elapsed > 0 else 0.0,
            local_checks=local_checks,
            full_checks=full_checks,
            objective=final_result.get("objective"),
            obj1=final_result.get("obj1"),
            obj2=final_result.get("obj2"),
            obj3=final_result.get("obj3"),
            feasible=final_result.get("feasible"),
            **oracle.summary(),
        )
    if final_result.get("feasible"):
        return best.solution
    return fallback_solution


def _set_candidate_position_limit(total_budget: float, n_blocks: int) -> None:
    global _CANDIDATE_POSITION_LIMIT
    if n_blocks >= 150 and total_budget < 90.0:
        _CANDIDATE_POSITION_LIMIT = 20
    else:
        _CANDIDATE_POSITION_LIMIT = 80


def _seed_phase_budget(total_budget: float, n_blocks: int) -> float:
    if total_budget >= 1000.0:
        base = min(180.0, total_budget * 0.08)
    elif total_budget >= 500.0:
        base = min(90.0, total_budget * 0.12)
    elif total_budget >= 240.0:
        base = min(45.0, total_budget * 0.14)
    else:
        base = min(60.0, total_budget)
    if n_blocks >= 250:
        base *= 1.15
    return min(total_budget, max(20.0, base))


def _hierarchical_seed_budget(total_budget: float, n_blocks: int, available_build_seconds: float) -> float:
    if total_budget >= 1000.0:
        target = min(160.0, total_budget * 0.06)
    elif total_budget >= 500.0:
        target = min(60.0, total_budget * 0.09)
    elif total_budget >= 240.0:
        target = min(36.0, total_budget * 0.10)
    else:
        target = min(8.0, max(2.0, total_budget * 0.12))
    if n_blocks >= 250:
        target *= 1.15
    return max(2.0, min(available_build_seconds, target))


def _initial_seeds(
    prob_info: dict,
    build_deadline: float,
    repair_deadline: float,
    total_budget: float,
) -> list[dict]:
    seeds = []
    if time.monotonic() < build_deadline:
        if total_budget >= 20.0:
            hierarchical_budget = _hierarchical_seed_budget(
                total_budget,
                len(prob_info["blocks"]),
                max(0.0, build_deadline - time.monotonic()),
            )
            hierarchical_seed = build_hierarchical_seed(
                prob_info,
                min(build_deadline, time.monotonic() + hierarchical_budget),
            )
            if hierarchical_seed is not None:
                seeds.append(hierarchical_seed)
                if total_budget >= 120.0:
                    return _rank_feasible_seeds(prob_info, seeds)
                return seeds
        if total_budget >= 30.0 and len(prob_info["blocks"]) >= 150:
            batch_deadline = min(repair_deadline - 6.0, time.monotonic() + 52.0)
            batch_seed = _construct_release_batch_seed(
                prob_info,
                batch_deadline,
                use_fast_release=bool(prob_info.get("_use_fast_release_seed")),
            )
            seeds.append(batch_seed)
            if time.monotonic() < repair_deadline - 0.5:
                seeds.append(stabilize_solution(prob_info, batch_seed, deadline=repair_deadline))
        else:
            aggressive = _construct_seed(prob_info, build_deadline, require_access=False)
            if total_budget >= 120.0 and len(prob_info["blocks"]) >= 150 and repair_deadline - time.monotonic() > 20.0:
                seeds.append(_repair_by_reinsertion(prob_info, aggressive, deadline=repair_deadline, max_passes=4))
            if time.monotonic() < repair_deadline - 0.5:
                seeds.append(stabilize_solution(prob_info, aggressive, deadline=repair_deadline))
    if total_budget < 120.0:
        return seeds
    return _rank_feasible_seeds(prob_info, seeds)


def _construct_release_batch_seed(prob_info: dict, deadline: float, use_fast_release: bool = False) -> dict:
    from utils import Bay, Block, check_entry, check_exit

    blocks = prob_info["blocks"]
    bays = [Bay.from_dict(data, bay_id) for bay_id, data in enumerate(prob_info["bays"])]
    assignments: dict[int, dict] = {}
    bay_blocks = [[] for _ in bays]
    bay_schedules = [[] for _ in bays]
    bay_loads = [0.0 for _ in bays]
    entry_seq = 0

    block_order = sorted(
        range(len(blocks)),
        key=lambda bid: (
            int(blocks[bid]["release_time"]),
            int(blocks[bid]["due_date"]),
            -_fit_difficulty(blocks[bid]),
            bid,
        ),
    )

    for block_id in block_order:
        if time.monotonic() >= deadline - 0.5:
            break
        assignment = None
        if use_fast_release:
            assignment = _fast_release_assignment(
                prob_info,
                block_id,
                bays,
                bay_blocks,
                bay_schedules,
                bay_loads,
                prefer_bays=bool(prob_info.get("_prefer_release_bays")),
            )
        if assignment is None:
            assignment = _exact_release_assignment(
                prob_info,
                block_id,
                bays,
                bay_blocks,
                bay_schedules,
                bay_loads,
                deadline,
                prefer_bays=bool(prob_info.get("_prefer_release_bays")),
            )
        if assignment is None:
            ranked = _ranked_insertions(
                prob_info,
                block_id,
                bays,
                bay_blocks,
                bay_schedules,
                bay_loads,
                deadline,
                require_access=True,
                limit=1,
                use_exit_blocking_penalty=False,
            )
            if ranked:
                assignment = ranked[0][1]
            else:
                assignment = _serial_append_assignment(prob_info, block_id, bays, bay_schedules, bay_loads)

        assignment = {**assignment, "_seq": entry_seq}
        entry_seq += 1
        assignments[block_id] = assignment
        _append_assignment_to_state(assignment, blocks, bay_blocks, bay_schedules, bay_loads)

    for block_id in range(len(blocks)):
        if block_id in assignments:
            continue
        assignment = _serial_append_assignment(prob_info, block_id, bays, bay_schedules, bay_loads)
        assignment = {**assignment, "_seq": entry_seq}
        entry_seq += 1
        assignments[block_id] = assignment
        _append_assignment_to_state(assignment, blocks, bay_blocks, bay_schedules, bay_loads)

    return {"operations": _build_operations(assignments.values())}


def solve_bootstrap_alns(prob_info: dict, deadline: float) -> dict:
    """Run essential obj1 bootstrap phases, then hand off to generic ALNS."""

    from utils import check_feasibility
    import os

    started_at = time.monotonic()
    total_budget = max(0.0, deadline - started_at)
    trace_enabled = bool(os.environ.get("OGC_ALNS_TRACE"))

    seed_budget = min(45.0, max(18.0, total_budget * 0.12))
    seed_deadline = min(deadline - 1.0, started_at + seed_budget)
    seed_candidates = []
    if os.environ.get("OGC_BOOTSTRAP_EDD_SEED", "").strip() == "1" and time.monotonic() < seed_deadline - 2.0:
        edd_seed = build_edd_master_seed(
            prob_info,
            min(seed_deadline, time.monotonic() + max(8.0, (seed_deadline - time.monotonic()) * 0.45)),
            max_rounds=2,
        )
        if edd_seed is not None:
            seed_candidates.append(edd_seed)
    if time.monotonic() < seed_deadline - 0.2:
        seed_candidates.append(_construct_preference_layer_seed(prob_info, seed_deadline))
    if not seed_candidates:
        seed_candidates.append(build_initial_solution(prob_info, deadline=seed_deadline))

    ranked_seeds = _rank_feasible_seeds(prob_info, seed_candidates)
    current = ranked_seeds[0] if ranked_seeds else seed_candidates[-1]
    current_result = check_feasibility(prob_info, current)
    if not current_result.get("feasible"):
        current = stabilize_solution(prob_info, current, deadline=min(deadline - 1.0, time.monotonic() + 10.0))
        current_result = check_feasibility(prob_info, current)
    if not current_result.get("feasible"):
        return current

    initial_result = current_result
    initial_obj3 = float(current_result.get("obj3") or 0.0)
    obj3_cap = _hierarchical_obj3_cap(prob_info, initial_obj3, total_budget)
    bootstrap_budget = min(max(90.0, total_budget * 0.42), min(360.0, total_budget * 0.55))
    bootstrap_deadline = min(deadline - 3.0, started_at + bootstrap_budget)
    if trace_enabled:
        _trace_alns_event(
            "bootstrap_alns_start",
            total_budget=total_budget,
            seed_budget=seed_budget,
            bootstrap_budget=bootstrap_budget,
            initial_objective=current_result.get("objective"),
            initial_obj1=current_result.get("obj1"),
            initial_obj2=current_result.get("obj2"),
            initial_obj3=current_result.get("obj3"),
            obj3_cap=obj3_cap,
        )

    if time.monotonic() < bootstrap_deadline - 3.0:
        current, current_result = _bottleneck_preference_relaxation(
            prob_info,
            current,
            current_result,
            min(bootstrap_deadline, time.monotonic() + max(25.0, (bootstrap_deadline - time.monotonic()) * 0.36)),
            obj3_cap=obj3_cap,
            trace_enabled=trace_enabled,
        )
    if time.monotonic() < bootstrap_deadline - 3.0:
        current, current_result = _hierarchical_preference_recovery(
            prob_info,
            current,
            current_result,
            min(bootstrap_deadline, time.monotonic() + max(12.0, (bootstrap_deadline - time.monotonic()) * 0.22)),
            trace_enabled=trace_enabled,
        )
    if (
        time.monotonic() < bootstrap_deadline - 3.0
        and float(current_result.get("obj3") or math.inf) < obj3_cap - 1e-6
    ):
        current, current_result = _bottleneck_preference_relaxation(
            prob_info,
            current,
            current_result,
            min(bootstrap_deadline, time.monotonic() + max(18.0, (bootstrap_deadline - time.monotonic()) * 0.34)),
            obj3_cap=obj3_cap,
            trace_enabled=trace_enabled,
        )
    if time.monotonic() < bootstrap_deadline - 3.0:
        current, current_result = _same_bay_tardiness_relayout(
            prob_info,
            current,
            current_result,
            min(bootstrap_deadline, time.monotonic() + max(12.0, (bootstrap_deadline - time.monotonic()) * 0.28)),
            trace_enabled=trace_enabled,
        )
    if time.monotonic() < bootstrap_deadline - 3.0:
        current, current_result = _same_bay_cluster_relayout(
            prob_info,
            current,
            current_result,
            min(bootstrap_deadline, time.monotonic() + max(10.0, (bootstrap_deadline - time.monotonic()) * 0.25)),
            trace_enabled=trace_enabled,
        )
    if time.monotonic() < bootstrap_deadline - 3.0:
        current, current_result = _exit_path_blocker_cluster_relayout(
            prob_info,
            current,
            current_result,
            min(bootstrap_deadline, time.monotonic() + max(10.0, (bootstrap_deadline - time.monotonic()) * 0.28)),
            trace_enabled=trace_enabled,
        )
    if time.monotonic() < bootstrap_deadline - 3.0:
        current, current_result = _objective_safe_left_shift_polish(
            prob_info,
            current,
            current_result,
            min(bootstrap_deadline, time.monotonic() + max(6.0, (bootstrap_deadline - time.monotonic()) * 0.35)),
        )
    if time.monotonic() < bootstrap_deadline - 3.0:
        current, current_result = _objective_safe_cluster_left_shift_polish(
            prob_info,
            current,
            current_result,
            min(bootstrap_deadline, time.monotonic() + max(6.0, (bootstrap_deadline - time.monotonic()) * 0.35)),
        )

    if trace_enabled:
        _trace_alns_event(
            "bootstrap_alns_handoff",
            elapsed=time.monotonic() - started_at,
            initial_objective=initial_result.get("objective"),
            handoff_objective=current_result.get("objective"),
            initial_obj1=initial_result.get("obj1"),
            handoff_obj1=current_result.get("obj1"),
            initial_obj2=initial_result.get("obj2"),
            handoff_obj2=current_result.get("obj2"),
            initial_obj3=initial_result.get("obj3"),
            handoff_obj3=current_result.get("obj3"),
            remaining_seconds=deadline - time.monotonic(),
        )

    if time.monotonic() >= deadline - 5.0:
        return current
    alns_solution = solve_alns(
        prob_info,
        deadline,
        fallback_solution=current,
        use_internal_seeds=False,
    )
    alns_result = check_feasibility(prob_info, alns_solution)
    if alns_result.get("feasible") and _hierarchical_preference_better(current_result, alns_result):
        return alns_solution
    return current


def solve_hierarchical_preference(prob_info: dict, deadline: float) -> dict:
    """Obj3-first hierarchical search.

    Build a low-preference-penalty feasible seed, then reduce tardiness while
    keeping the preference loss bounded. This is an experimental complement to
    the official-objective ALNS path: it searches a different basin instead of
    immediately discarding low-obj3/high-obj1 solutions.
    """

    from utils import check_feasibility
    import os

    started_at = time.monotonic()
    trace_enabled = bool(os.environ.get("OGC_ALNS_TRACE"))
    total_budget = max(0.0, deadline - started_at)
    if len(prob_info["blocks"]) >= 150 and total_budget >= 60.0:
        seed_budget = min(60.0, max(25.0, total_budget * 0.12))
    else:
        seed_budget = min(30.0, max(5.0, total_budget * 0.15))
    seed_deadline = min(deadline - 1.0, started_at + seed_budget)
    seed_candidates = []
    if len(prob_info["blocks"]) >= 150 and total_budget >= 240.0 and time.monotonic() < seed_deadline - 2.0:
        edd_seed_deadline = min(
            seed_deadline,
            time.monotonic() + max(8.0, min(seed_budget * 0.55, seed_deadline - time.monotonic())),
        )
        edd_seed = build_edd_master_seed(prob_info, edd_seed_deadline)
        if edd_seed is not None:
            seed_candidates.append(edd_seed)
    if time.monotonic() < seed_deadline - 0.2:
        seed_candidates.append(_construct_preference_layer_seed(prob_info, seed_deadline))
    elif not seed_candidates:
        seed_candidates.append(_construct_preference_layer_seed(prob_info, min(deadline - 1.0, time.monotonic() + 2.0)))

    ranked_seeds = _rank_feasible_seeds(prob_info, seed_candidates) if seed_candidates else []
    current = ranked_seeds[0] if ranked_seeds else seed_candidates[-1]
    current_result = check_feasibility(prob_info, current)
    if not current_result.get("feasible"):
        current = stabilize_solution(prob_info, current, deadline=min(deadline - 1.0, time.monotonic() + 10.0))
        current_result = check_feasibility(prob_info, current)
    if not current_result.get("feasible"):
        return current

    initial_obj3 = float(current_result.get("obj3") or 0.0)
    obj3_cap = _hierarchical_obj3_cap(prob_info, initial_obj3, total_budget)
    if trace_enabled:
        _trace_alns_event(
            "hierarchical_obj3_cap",
            initial_obj3=initial_obj3,
            obj3_cap=obj3_cap,
            total_budget=total_budget,
        )
    current, current_result = _bottleneck_preference_relaxation(
        prob_info,
        current,
        current_result,
        deadline,
        obj3_cap=obj3_cap,
        trace_enabled=trace_enabled,
    )
    if time.monotonic() < deadline - 3.0:
        recovery_deadline = min(deadline - 1.0, time.monotonic() + max(8.0, (deadline - time.monotonic()) * 0.35))
        current, current_result = _hierarchical_preference_recovery(
            prob_info,
            current,
            current_result,
            recovery_deadline,
            trace_enabled=trace_enabled,
        )
    if time.monotonic() < deadline - 3.0 and float(current_result.get("obj3") or math.inf) < obj3_cap - 1e-6:
        current, current_result = _bottleneck_preference_relaxation(
            prob_info,
            current,
            current_result,
            _limited_stage_deadline(deadline, min_seconds=20.0, fraction=0.45),
            obj3_cap=obj3_cap,
            trace_enabled=trace_enabled,
        )
    if time.monotonic() < deadline - 3.0:
        relayout_deadline = min(deadline - 1.0, time.monotonic() + max(8.0, (deadline - time.monotonic()) * 0.35))
        current, current_result = _same_bay_tardiness_relayout(
            prob_info,
            current,
            current_result,
            relayout_deadline,
            trace_enabled=trace_enabled,
        )
    if time.monotonic() < deadline - 3.0:
        current, current_result = _same_bay_cluster_relayout(
            prob_info,
            current,
            current_result,
            min(deadline - 1.0, time.monotonic() + max(8.0, (deadline - time.monotonic()) * 0.35)),
            trace_enabled=trace_enabled,
        )
    if time.monotonic() < deadline - 3.0:
        current, current_result = _exit_path_blocker_cluster_relayout(
            prob_info,
            current,
            current_result,
            min(deadline - 1.0, time.monotonic() + max(8.0, (deadline - time.monotonic()) * 0.35)),
            trace_enabled=trace_enabled,
        )
    if time.monotonic() < deadline - 3.0:
        polish_deadline = min(deadline - 1.0, time.monotonic() + max(8.0, (deadline - time.monotonic()) * 0.45))
        before_polish = current_result
        current, current_result = _objective_safe_left_shift_polish(
            prob_info,
            current,
            current_result,
            polish_deadline,
        )
        if time.monotonic() < deadline - 3.0:
            current, current_result = _objective_safe_cluster_left_shift_polish(
                prob_info,
                current,
                current_result,
                min(deadline - 1.0, time.monotonic() + max(6.0, (deadline - time.monotonic()) * 0.35)),
            )
        if trace_enabled:
            _trace_alns_event(
                "hierarchical_schedule_polish",
                initial_objective=before_polish.get("objective"),
                final_objective=current_result.get("objective"),
                initial_obj1=before_polish.get("obj1"),
                final_obj1=current_result.get("obj1"),
                initial_obj2=before_polish.get("obj2"),
                final_obj2=current_result.get("obj2"),
                initial_obj3=before_polish.get("obj3"),
                final_obj3=current_result.get("obj3"),
            )
    if total_budget >= 500.0:
        extra_cycles = 8
    elif total_budget >= 300.0:
        extra_cycles = 5
    elif total_budget >= 180.0:
        extra_cycles = 3
    else:
        extra_cycles = 1
    for extra_cycle in range(extra_cycles):
        if time.monotonic() >= deadline - 5.0:
            break
        cycle_before = current_result
        current, current_result = _hierarchical_preference_recovery(
            prob_info,
            current,
            current_result,
            min(deadline - 1.0, time.monotonic() + max(8.0, (deadline - time.monotonic()) * 0.25)),
            trace_enabled=trace_enabled,
        )
        if time.monotonic() < deadline - 4.0 and float(current_result.get("obj3") or math.inf) < obj3_cap - 1e-6:
            current, current_result = _bottleneck_preference_relaxation(
                prob_info,
                current,
                current_result,
                _limited_stage_deadline(deadline, min_seconds=12.0, fraction=0.22),
                obj3_cap=obj3_cap,
                trace_enabled=trace_enabled,
            )
        if time.monotonic() < deadline - 4.0:
            current, current_result = _same_bay_tardiness_relayout(
                prob_info,
                current,
                current_result,
                min(deadline - 1.0, time.monotonic() + max(8.0, (deadline - time.monotonic()) * 0.35)),
                trace_enabled=trace_enabled,
            )
        if time.monotonic() < deadline - 4.0:
            current, current_result = _same_bay_cluster_relayout(
                prob_info,
                current,
                current_result,
                min(deadline - 1.0, time.monotonic() + max(8.0, (deadline - time.monotonic()) * 0.35)),
                trace_enabled=trace_enabled,
            )
        if time.monotonic() < deadline - 4.0:
            current, current_result = _exit_path_blocker_cluster_relayout(
                prob_info,
                current,
                current_result,
                min(deadline - 1.0, time.monotonic() + max(8.0, (deadline - time.monotonic()) * 0.35)),
                trace_enabled=trace_enabled,
            )
        if time.monotonic() < deadline - 4.0:
            current, current_result = _objective_safe_left_shift_polish(
                prob_info,
                current,
                current_result,
                min(deadline - 1.0, time.monotonic() + max(8.0, (deadline - time.monotonic()) * 0.45)),
            )
        if trace_enabled:
            _trace_alns_event(
                "hierarchical_extra_cycle",
                cycle=extra_cycle + 1,
                initial_objective=cycle_before.get("objective"),
                final_objective=current_result.get("objective"),
                initial_obj1=cycle_before.get("obj1"),
                final_obj1=current_result.get("obj1"),
                initial_obj2=cycle_before.get("obj2"),
                final_obj2=current_result.get("obj2"),
                initial_obj3=cycle_before.get("obj3"),
                final_obj3=current_result.get("obj3"),
            )
        if not _hierarchical_preference_better(cycle_before, current_result):
            break
    if total_budget >= 500.0 and time.monotonic() < deadline - 5.0:
        current, current_result = _exact_tardy_probe_polish(
            prob_info,
            current,
            current_result,
            _limited_stage_deadline(deadline, min_seconds=25.0, fraction=0.08, reserve_seconds=20.0),
            obj3_cap=obj3_cap,
            trace_enabled=trace_enabled,
        )
    if total_budget >= 500.0 and time.monotonic() < deadline - 5.0:
        current, current_result = _late_hierarchical_recovery_polish(
            prob_info,
            current,
            current_result,
            deadline,
            obj3_cap=obj3_cap,
            trace_enabled=trace_enabled,
        )
    if total_budget >= 500.0 and time.monotonic() < deadline - 5.0:
        current, current_result = _hierarchical_tail_obj1_polish(
            prob_info,
            current,
            current_result,
            min(deadline - 1.0, time.monotonic() + 40.0),
            total_budget=total_budget,
            trace_enabled=trace_enabled,
            label="pre_exact",
        )
    if total_budget >= 500.0 and time.monotonic() < deadline - 5.0:
        current, current_result = _exact_low_obj3_cycle_polish(
            prob_info,
            current,
            current_result,
            deadline,
            obj3_cap=obj3_cap,
            trace_enabled=trace_enabled,
        )
    if total_budget >= 500.0 and float(current_result.get("obj1") or math.inf) <= 25.0 and time.monotonic() < deadline - 360.0:
        current, current_result = _final_due_window_relocation_polish(
            prob_info,
            current,
            current_result,
            min(deadline - 300.0, time.monotonic() + 260.0),
            trace_enabled=trace_enabled,
        )
    if total_budget >= 1000.0 and float(current_result.get("obj1") or math.inf) <= 1e-6 and time.monotonic() < deadline - 90.0:
        current, current_result = _zero_tardiness_obj3_recovery_polish(
            prob_info,
            current,
            current_result,
            min(deadline - 30.0, time.monotonic() + 520.0),
            trace_enabled=trace_enabled,
        )
    if total_budget >= 500.0 and float(current_result.get("obj1") or math.inf) > 5.0 and time.monotonic() < deadline - 3.0:
        current, current_result = _hierarchical_tail_obj1_polish(
            prob_info,
            current,
            current_result,
            deadline - 0.8,
            total_budget=total_budget,
            trace_enabled=trace_enabled,
            label="final",
        )
    if total_budget >= 500.0 and float(current_result.get("obj1") or math.inf) <= 20.0 and time.monotonic() < deadline - 3.0:
        current, current_result = _final_due_window_relocation_polish(
            prob_info,
            current,
            current_result,
            deadline - 0.8,
            trace_enabled=trace_enabled,
        )
    if total_budget >= 500.0 and float(current_result.get("obj1") or math.inf) <= 20.0 and time.monotonic() < deadline - 3.0:
        current, current_result = _final_entry_blocker_relocation_polish(
            prob_info,
            current,
            current_result,
            deadline - 0.8,
            trace_enabled=trace_enabled,
        )
    if total_budget >= 500.0 and float(current_result.get("obj1") or math.inf) <= 20.0 and time.monotonic() < deadline - 3.0:
        current, current_result = _final_tardy_grid_relocation_polish(
            prob_info,
            current,
            current_result,
            deadline - 0.8,
            trace_enabled=trace_enabled,
        )
    if total_budget >= 1000.0 and float(current_result.get("obj1") or math.inf) <= 1e-6 and time.monotonic() < deadline - 3.0:
        current, current_result = _zero_tardiness_obj3_recovery_polish(
            prob_info,
            current,
            current_result,
            deadline - 0.8,
            trace_enabled=trace_enabled,
        )
    if total_budget >= 1500.0 and _obj1_low_enough_for_secondary(current_result) and time.monotonic() < deadline - 3.0:
        current, current_result = _objective_safe_preference_polish(
            prob_info,
            current,
            current_result,
            deadline - 0.8,
            trace_enabled=trace_enabled,
        )
    if total_budget >= 500.0 and _obj1_low_enough_for_secondary(current_result) and time.monotonic() < deadline - 3.0:
        current, current_result = _objective_safe_fixed_schedule_preference_swap_polish(
            prob_info,
            current,
            current_result,
            deadline - 0.8,
            trace_enabled=trace_enabled,
        )
    best = current
    best_result = current_result
    iteration = 0
    checked = 0
    accepted = 0
    local_checks = 0
    full_checks = 0
    rng = random.Random(_stable_seed(prob_info) + 7919)
    oracle = FastFeasibilityOracle(prob_info, verify_interval=_ORACLE_VERIFY_INTERVAL)

    skip_generic_search = total_budget >= 500.0 and best_result.get("feasible")
    while not skip_generic_search and time.monotonic() < deadline - 1.0:
        iteration += 1
        assignments = _assignments_from_solution(current)
        if not assignments:
            break
        remove_count = 5 if len(prob_info["blocks"]) >= 150 else 3
        if iteration % 7 == 0:
            clusters = _same_bay_tardy_clusters(
                prob_info,
                assignments,
                max_clusters=1,
                cluster_size=remove_count,
            )
            removed = clusters[0] if clusters else _critical_due_cluster_removal(prob_info, assignments, remove_count)
        elif iteration % 6 == 0:
            removed = _random_removal(assignments, max(3, remove_count - 1), rng)
        elif iteration % 4 == 0:
            removed = _worst_preference_removal(prob_info, assignments, max(3, remove_count - 1))
        elif iteration % 3 == 0:
            removed = _access_blocker_removal(prob_info, assignments, remove_count)
        else:
            removed = _worst_tardiness_removal(prob_info, assignments, remove_count)

        candidate = _repair_removed(
            prob_info,
            assignments,
            removed,
            deadline,
            use_regret=iteration % 5 == 0 or len(removed) >= 5,
            allow_timeout_fallback=False,
            use_exit_blocking_penalty=True,
            prioritize_tardy_first=True,
            use_multi_slot=True,
            use_preference_bias=True,
        )
        if candidate is None:
            continue

        candidate_result, used_full_check = _candidate_result_with_periodic_full_check(
            prob_info,
            assignments,
            candidate,
            removed,
            iteration,
            check_feasibility,
            oracle=oracle,
        )
        checked += 1
        if used_full_check:
            full_checks += 1
        else:
            local_checks += 1
        if not candidate_result.get("feasible"):
            continue
        candidate_obj3 = float(candidate_result.get("obj3") or math.inf)
        if candidate_obj3 > obj3_cap:
            continue
        if _hierarchical_preference_better(current_result, candidate_result):
            current = candidate
            current_result = candidate_result
            accepted += 1
        if _hierarchical_preference_better(best_result, candidate_result):
            best = candidate
            best_result = candidate_result

    if trace_enabled:
        _trace_alns_event(
            "hierarchical_preference",
            elapsed=time.monotonic() - started_at,
            iterations=iteration,
            checks=checked,
            local_checks=local_checks,
            full_checks=full_checks,
            accepted=accepted,
            **oracle.summary(),
            obj3_cap=obj3_cap,
            initial_obj3=initial_obj3,
            final_objective=best_result.get("objective"),
            final_obj1=best_result.get("obj1"),
            final_obj2=best_result.get("obj2"),
            final_obj3=best_result.get("obj3"),
        )
    return best


def _construct_preference_layer_seed(prob_info: dict, deadline: float) -> dict:
    from utils import Bay, Block

    blocks = prob_info["blocks"]
    bays = [Bay.from_dict(data, bay_id) for bay_id, data in enumerate(prob_info["bays"])]
    assignments: dict[int, dict] = {}
    bay_blocks = [[] for _ in bays]
    bay_schedules = [[] for _ in bays]
    bay_loads = [0.0 for _ in bays]
    entry_seq = 0

    block_order = sorted(
        range(len(blocks)),
        key=lambda bid: (
            int(blocks[bid]["due_date"]),
            int(blocks[bid]["release_time"]),
            int(blocks[bid]["processing_time"]),
            bid,
        ),
    )

    for block_id in block_order:
        block_data = blocks[block_id]
        assignment = None
        for bay_id in _preferred_fit_bay_order(block_data, bays):
            assignment = _active_aware_preference_assignment(
                prob_info,
                block_id,
                bay_id,
                bays,
                bay_blocks,
                bay_schedules,
                deadline,
            )
            if assignment is not None:
                assignment = {**assignment, "_seq": entry_seq}
                break

        if assignment is None:
            assignment = _preference_empty_window_assignment(
                block_id,
                block_data,
                bays,
                bay_schedules,
                entry_seq,
            )

        assignments[block_id] = assignment
        entry_seq += 1
        _append_assignment_to_state(assignment, blocks, bay_blocks, bay_schedules, bay_loads)
        if time.monotonic() >= deadline - 0.05:
            break

    for block_id in range(len(blocks)):
        if block_id in assignments:
            continue
        assignment = _preference_empty_window_assignment(
            block_id,
            blocks[block_id],
            bays,
            bay_schedules,
            entry_seq,
        )
        assignments[block_id] = assignment
        entry_seq += 1
        _append_assignment_to_state(assignment, blocks, bay_blocks, bay_schedules, bay_loads)

    return {"operations": _build_operations(assignments.values())}


def _candidate_result_with_periodic_full_check(
    prob_info: dict,
    incumbent_assignments: dict[int, dict],
    candidate_solution: dict,
    changed_ids: Iterable[int],
    iteration: int,
    check_feasibility,
    oracle: FastFeasibilityOracle | None = None,
) -> tuple[dict, bool]:
    if _FULL_CHECK_INTERVAL > 0 and iteration % _FULL_CHECK_INTERVAL == 0:
        return check_feasibility(prob_info, candidate_solution), True
    candidate_assignments = _assignments_from_solution(candidate_solution)
    if oracle is not None:
        oracle_result = oracle.candidate_result(incumbent_assignments, candidate_assignments, changed_ids)
        if oracle.should_verify_with_official(oracle_result):
            official_result = check_feasibility(prob_info, candidate_solution)
            oracle.record_official_verification(oracle_result, official_result)
            return official_result, True
        return oracle_result, False
    return _local_candidate_result(prob_info, incumbent_assignments, candidate_assignments, changed_ids), False


def _active_aware_preference_assignment(
    prob_info: dict,
    block_id: int,
    bay_id: int,
    bays,
    bay_blocks,
    bay_schedules,
    deadline: float,
) -> dict | None:
    from utils import Block, check_entry, check_exit

    if time.monotonic() >= deadline - 0.05:
        return None

    blocks = prob_info["blocks"]
    block_data = blocks[block_id]
    bay = bays[bay_id]
    release_time = int(block_data["release_time"])
    processing_time = int(block_data["processing_time"])
    due_time = int(block_data["due_date"])
    candidate_entries = {release_time}
    due_entry = due_time - processing_time
    if due_entry >= release_time:
        candidate_entries.add(due_entry)
    for entry_time, exit_time in bay_schedules[bay_id]:
        if entry_time >= release_time:
            candidate_entries.add(int(entry_time))
        if exit_time >= release_time:
            candidate_entries.add(int(exit_time))

    best = None
    for entry_time in sorted(candidate_entries):
        if time.monotonic() >= deadline - 0.05:
            break
        entry_time = max(release_time, int(entry_time))
        exit_time = entry_time + processing_time
        active_blocks = [
            other
            for other, (other_entry, other_exit) in zip(bay_blocks[bay_id], bay_schedules[bay_id])
            if entry_time < other_exit and other_entry < exit_time
        ]
        present_at_entry = [
            other
            for other, (other_entry, other_exit) in zip(bay_blocks[bay_id], bay_schedules[bay_id])
            if other_entry <= entry_time < other_exit
        ]
        present_at_exit = [
            other
            for other, (other_entry, other_exit) in zip(bay_blocks[bay_id], bay_schedules[bay_id])
            if other_entry < exit_time < other_exit
        ]
        for orient_idx in range(len(block_data.get("shape", []))):
            bbox = orientation_bbox(block_data, orient_idx)
            for x, y in _candidate_positions(bay, active_blocks, bbox)[:80]:
                block = Block(block_id, block_data, x=int(x), y=int(y), orient_idx=int(orient_idx))
                if not bay.contains_block(block):
                    continue
                if check_entry(bay, present_at_entry, block, fast=True):
                    continue
                if check_exit(bay, [block, *present_at_exit], block, fast=True):
                    continue
                if any(_cached_pair_collision(bay, block, other) for other in active_blocks):
                    continue
                if _blocks_earlier_due_exits(
                    bay,
                    block,
                    bay_blocks[bay_id],
                    bay_schedules[bay_id],
                    entry_time,
                    exit_time,
                ):
                    continue
                score = (
                    max(0, exit_time - due_time),
                    exit_time,
                    y + bbox[3],
                    orient_idx,
                    x,
                    y,
                )
                assignment = {
                    "block_id": block_id,
                    "bay_id": int(bay_id),
                    "x": int(x),
                    "y": int(y),
                    "orient_idx": int(orient_idx),
                    "entry_time": int(entry_time),
                    "exit_time": int(exit_time),
                }
                if best is None or score < best[0]:
                    best = (score, assignment)
        if best is not None and best[0][0] <= 0:
            break

    return None if best is None else best[1]


def _local_candidate_result(
    prob_info: dict,
    incumbent_assignments: dict[int, dict],
    candidate_assignments: dict[int, dict],
    changed_ids: Iterable[int],
) -> dict:
    local_ok = _local_feasibility_check(prob_info, incumbent_assignments, candidate_assignments, changed_ids)
    if not local_ok:
        return _infeasible_result(stage=5)
    return _objective_result_from_assignments(prob_info, candidate_assignments)


def _local_feasibility_check(
    prob_info: dict,
    incumbent_assignments: dict[int, dict],
    candidate_assignments: dict[int, dict],
    changed_ids: Iterable[int],
) -> bool:
    from utils import Bay, Block, check_entry, check_exit

    blocks = prob_info["blocks"]
    if len(candidate_assignments) != len(blocks):
        return False

    affected_bays: set[int] = set()
    for block_id in changed_ids:
        if block_id in incumbent_assignments and "bay_id" in incumbent_assignments[block_id]:
            affected_bays.add(int(incumbent_assignments[block_id]["bay_id"]))
        if block_id in candidate_assignments and "bay_id" in candidate_assignments[block_id]:
            affected_bays.add(int(candidate_assignments[block_id]["bay_id"]))
    if not affected_bays:
        return False

    bays = [Bay.from_dict(data, bay_id) for bay_id, data in enumerate(prob_info["bays"])]
    bay_rows: dict[int, list[tuple[int, dict, object]]] = {bay_id: [] for bay_id in affected_bays}
    for block_id, assignment in candidate_assignments.items():
        required = {"block_id", "bay_id", "x", "y", "orient_idx", "entry_time", "exit_time"}
        if not required.issubset(assignment):
            return False
        bay_id = int(assignment["bay_id"])
        if bay_id < 0 or bay_id >= len(bays):
            return False
        block_data = blocks[block_id]
        entry_time = int(assignment["entry_time"])
        exit_time = int(assignment["exit_time"])
        if entry_time < int(block_data["release_time"]):
            return False
        if exit_time - entry_time != int(block_data["processing_time"]):
            return False
        orient_idx = int(assignment["orient_idx"])
        if orient_idx < 0 or orient_idx >= len(block_data.get("shape", [])):
            return False
        if bay_id not in affected_bays:
            continue
        block = Block(
            block_id,
            block_data,
            x=int(assignment["x"]),
            y=int(assignment["y"]),
            orient_idx=orient_idx,
        )
        bay = bays[bay_id]
        if not bay.contains_block(block):
            return False
        bay_rows[bay_id].append((block_id, assignment, block))

    for bay_id, rows in bay_rows.items():
        bay = bays[bay_id]
        for index, (block_id, assignment, block) in enumerate(rows):
            entry_time = int(assignment["entry_time"])
            exit_time = int(assignment["exit_time"])
            entry_seq = int(assignment.get("_seq", block_id))

            for other_id, other_assignment, other_block in rows[index + 1 :]:
                other_entry = int(other_assignment["entry_time"])
                other_exit = int(other_assignment["exit_time"])
                if entry_time < other_exit and other_entry < exit_time:
                    if _cached_pair_collision(bay, block, other_block):
                        return False

            present_at_entry = [
                other_block
                for other_id, other_assignment, other_block in rows
                if other_id != block_id
                and _is_present_before_entry(
                    other_assignment,
                    entry_time,
                    entry_seq,
                    other_id,
                )
            ]
            if check_entry(bay, present_at_entry, block, fast=True):
                return False

            present_at_exit = [
                block,
                *[
                    other_block
                    for other_id, other_assignment, other_block in rows
                    if other_id != block_id
                    and _is_present_at_exit_before_removal(
                        other_assignment,
                        exit_time,
                        block_id,
                        other_id,
                    )
                ],
            ]
            if check_exit(bay, present_at_exit, block, fast=True):
                return False

    return True


def _is_present_before_entry(
    assignment: dict,
    entry_time: int,
    entry_seq: int,
    block_id: int,
) -> bool:
    other_entry = int(assignment["entry_time"])
    other_exit = int(assignment["exit_time"])
    if other_entry < entry_time < other_exit:
        return True
    if other_entry == entry_time and other_exit > entry_time:
        return int(assignment.get("_seq", block_id)) < entry_seq
    return False


def _is_present_at_exit_before_removal(
    assignment: dict,
    exit_time: int,
    target_id: int,
    other_id: int,
) -> bool:
    other_entry = int(assignment["entry_time"])
    other_exit = int(assignment["exit_time"])
    if other_entry < exit_time < other_exit:
        return True
    if other_entry < exit_time and other_exit == exit_time:
        return other_id > target_id
    return False


def _objective_result_from_assignments(prob_info: dict, assignments: dict[int, dict]) -> dict:
    blocks = prob_info["blocks"]
    bays = prob_info["bays"]
    weights = prob_info.get("weights", {})
    w1 = float(weights.get("w1", 1.0))
    w2 = float(weights.get("w2", 1.0))
    w3 = float(weights.get("w3", 1.0))

    obj1 = 0.0
    obj3 = 0.0
    bay_loads = [0.0 for _ in bays]
    for block_id, assignment in assignments.items():
        bay_id = int(assignment["bay_id"])
        if bay_id < 0 or bay_id >= len(bays):
            return _infeasible_result(stage=5)
        block_data = blocks[block_id]
        obj1 += max(0.0, int(assignment["exit_time"]) - int(block_data["due_date"]))
        bay_loads[bay_id] += float(block_data["workload"])
        preferences = block_data["bay_preferences"]
        obj3 += max(preferences) - preferences[bay_id]

    bay_areas = [float(bay["width"]) * float(bay["height"]) for bay in bays]
    avg_area = sum(bay_areas) / len(bay_areas)
    bay_weights = [avg_area / area for area in bay_areas]
    if len(bays) >= 2:
        obj2 = math.floor(
            max(
                abs(bay_weights[first] * bay_loads[first] - bay_weights[second] * bay_loads[second])
                for first in range(len(bays))
                for second in range(len(bays))
                if first != second
            )
        )
    else:
        obj2 = 0.0
    objective = w1 * obj1 + w2 * obj2 + w3 * obj3
    return {
        "feasible": True,
        "stage": 5,
        "violations": [],
        "objective": objective,
        "obj1": obj1,
        "obj2": obj2,
        "obj3": obj3,
    }


def _infeasible_result(stage: int) -> dict:
    return {
        "feasible": False,
        "stage": stage,
        "violations": [],
        "objective": None,
        "obj1": None,
        "obj2": None,
        "obj3": None,
    }


def _preferred_fit_bay_order(block_data: dict, bays) -> list[int]:
    preferences = block_data["bay_preferences"]
    rows = []
    for bay_id, bay in enumerate(bays):
        for orient_idx in range(len(block_data.get("shape", []))):
            bbox = orientation_bbox(block_data, orient_idx)
            x, y = lower_left_integer_position(bbox)
            try:
                from utils import Block

                if bay.contains_block(Block(-1, block_data, x=int(x), y=int(y), orient_idx=int(orient_idx))):
                    rows.append((-preferences[bay_id], bay_id))
                    break
            except Exception:
                continue
    if rows:
        return [bay_id for _, bay_id in sorted(rows)]
    return sorted(range(len(bays)), key=lambda bay_id: (-preferences[bay_id], bay_id))


def _compact_position_candidates(bay, placed_blocks, bbox) -> list[tuple[int, int]]:
    min_x, min_y, max_x, max_y = bbox
    width = max_x - min_x
    height = max_y - min_y
    lower_left = lower_left_integer_position(bbox)
    candidates = [
        lower_left,
        (
            max(0, int((bay.width - width) // 2 - min_x)),
            max(0, int((bay.height - height) // 2 - min_y)),
        ),
        (max(0, int(bay.width - width - min_x)), lower_left[1]),
        (lower_left[0], max(0, int(bay.height - height - min_y))),
    ]
    candidates.extend(_candidate_positions(bay, placed_blocks, bbox)[:8])
    seen: set[tuple[int, int]] = set()
    result = []
    for x, y in candidates:
        point = (int(x), int(y))
        if point in seen:
            continue
        seen.add(point)
        result.append(point)
    return result


def _preference_empty_window_assignment(
    block_id: int,
    block_data: dict,
    bays,
    bay_schedules,
    entry_seq: int,
) -> dict:
    for bay_id in _preferred_fit_bay_order(block_data, bays):
        bay = bays[bay_id]
        for orient_idx in range(len(block_data.get("shape", []))):
            bbox = orientation_bbox(block_data, orient_idx)
            x, y = lower_left_integer_position(bbox)
            try:
                from utils import Block

                if not bay.contains_block(Block(block_id, block_data, x=int(x), y=int(y), orient_idx=int(orient_idx))):
                    continue
            except Exception:
                continue
            entry_time = _empty_bay_entry(
                bay_schedules[bay_id],
                int(block_data["release_time"]),
                int(block_data["processing_time"]),
            )
            return {
                "block_id": block_id,
                "bay_id": int(bay_id),
                "x": int(x),
                "y": int(y),
                "orient_idx": int(orient_idx),
                "entry_time": int(entry_time),
                "exit_time": int(entry_time) + int(block_data["processing_time"]),
                "_seq": entry_seq,
            }

    bay_id = max(range(len(bays)), key=lambda idx: (block_data["bay_preferences"][idx], -idx))
    entry_time = _empty_bay_entry(
        bay_schedules[bay_id],
        int(block_data["release_time"]),
        int(block_data["processing_time"]),
    )
    return {
        "block_id": block_id,
        "bay_id": int(bay_id),
        "x": 0,
        "y": 0,
        "orient_idx": 0,
        "entry_time": int(entry_time),
        "exit_time": int(entry_time) + int(block_data["processing_time"]),
        "_seq": entry_seq,
    }


def _hierarchical_preference_better(current_result: dict, candidate_result: dict) -> bool:
    if not candidate_result.get("feasible"):
        return False
    if not current_result.get("feasible"):
        return True
    current_obj1 = float(current_result.get("obj1") or math.inf)
    candidate_obj1 = float(candidate_result.get("obj1") or math.inf)
    current_objective = float(current_result.get("objective") or math.inf)
    candidate_objective = float(candidate_result.get("objective") or math.inf)
    current_obj3 = float(current_result.get("obj3") or math.inf)
    candidate_obj3 = float(candidate_result.get("obj3") or math.inf)
    return (
        candidate_obj1,
        candidate_objective,
        candidate_obj3,
    ) < (
        current_obj1,
        current_objective,
        current_obj3,
    )


def _hierarchical_obj3_cap(prob_info: dict, initial_obj3: float, total_budget: float) -> float:
    base_cap = min(max(1000.0, initial_obj3 + 500.0), initial_obj3 + 1500.0)
    if total_budget < 500.0:
        return base_cap

    weights = prob_info.get("weights", {})
    w1 = float(weights.get("w1", 1.0))
    w3 = max(1.0, float(weights.get("w3", 1.0)))
    one_tardy_unit_in_obj3 = w1 / w3
    if total_budget >= 1500.0:
        long_run_extra = min(650.0, max(240.0, one_tardy_unit_in_obj3 * 2.0))
        return min(base_cap + long_run_extra, initial_obj3 + 2200.0)
    long_run_extra = min(300.0, max(120.0, one_tardy_unit_in_obj3))
    return min(base_cap + long_run_extra, initial_obj3 + 1800.0)


def _limited_stage_deadline(deadline: float, *, min_seconds: float, fraction: float, reserve_seconds: float = 1.0) -> float:
    now = time.monotonic()
    stage_end = deadline - reserve_seconds
    if now >= stage_end:
        return now
    remaining = stage_end - now
    stage_seconds = max(min_seconds, remaining * fraction)
    return min(stage_end, now + stage_seconds)


def _bottleneck_preference_relaxation(
    prob_info: dict,
    solution: dict,
    initial_result: dict,
    deadline: float,
    *,
    obj3_cap: float,
    trace_enabled: bool = False,
) -> tuple[dict, dict]:
    from utils import Bay, check_feasibility

    if not initial_result.get("feasible"):
        return solution, initial_result

    started_at = time.monotonic()
    current = solution
    current_result = initial_result
    blocks = prob_info["blocks"]
    bays = [Bay.from_dict(data, bay_id) for bay_id, data in enumerate(prob_info["bays"])]
    n_blocks = len(blocks)
    max_rounds = 80 if n_blocks >= 150 else 40
    top_blocks = 10 if n_blocks >= 150 else 8
    top_targets = 4 if n_blocks >= 150 else 4
    quick_checks = 0
    official_checks = 0
    feasible_checks = 0
    accepted = 0

    while accepted < max_rounds and time.monotonic() < deadline - 1.0:
        assignments = _assignments_from_solution(current)
        if not assignments:
            break
        bay_rows = _tardy_rows_by_bay(prob_info, assignments)
        if not bay_rows:
            break

        current_obj1 = float(current_result.get("obj1") or math.inf)
        current_obj3 = float(current_result.get("obj3") or math.inf)
        best_candidate = None
        best_candidate_result = None
        best_move = None

        for source_bay, _bay_tardiness, rows in bay_rows[:3]:
            if time.monotonic() >= deadline - 1.0:
                break
            for block_id, _tardiness in rows[:top_blocks]:
                if time.monotonic() >= deadline - 1.0:
                    break
                original = assignments[block_id]
                current_bay = int(original["bay_id"])
                if current_bay != source_bay:
                    continue
                block_data = blocks[block_id]
                preferences = block_data["bay_preferences"]
                best_pref = max(preferences)
                current_regret = best_pref - preferences[current_bay]
                bay_blocks, bay_schedules, _bay_loads = _state_from_assignments(
                    prob_info,
                    {bid: data for bid, data in assignments.items() if bid != block_id},
                    bays,
                )
                target_bays = sorted(
                    (bay_id for bay_id in range(len(bays)) if bay_id != current_bay),
                    key=lambda bay_id: (
                        best_pref - preferences[bay_id],
                        sum(max(0, int(data["exit_time"]) - int(blocks[bid]["due_date"]))
                            for bid, data in assignments.items()
                            if int(data["bay_id"]) == bay_id),
                        -preferences[bay_id],
                        bay_id,
                    ),
                )
                for target_bay in target_bays[:top_targets]:
                    if time.monotonic() >= deadline - 1.0:
                        break
                    new_regret = best_pref - preferences[target_bay]
                    if current_obj3 + new_regret - current_regret > obj3_cap:
                        continue
                    assignment = _active_aware_preference_assignment(
                        prob_info,
                        block_id,
                        target_bay,
                        bays,
                        bay_blocks,
                        bay_schedules,
                        min(deadline, time.monotonic() + 2.0),
                    )
                    if assignment is None:
                        continue
                    assignment = {**assignment, "_seq": int(original.get("_seq", block_id))}
                    candidate_assignments = {bid: dict(data) for bid, data in assignments.items()}
                    candidate_assignments[block_id] = assignment
                    quick_result = _objective_result_from_assignments(prob_info, candidate_assignments)
                    quick_checks += 1
                    if not quick_result.get("feasible"):
                        continue
                    if float(quick_result.get("obj3") or math.inf) > obj3_cap:
                        continue
                    if float(quick_result.get("obj1") or math.inf) >= current_obj1:
                        continue
                    if not _hierarchical_preference_better(current_result, quick_result):
                        continue
                    candidate = {"operations": _build_operations(candidate_assignments.values())}
                    official_checks += 1
                    candidate_result = check_feasibility(prob_info, candidate)
                    if candidate_result.get("feasible"):
                        feasible_checks += 1
                    if not candidate_result.get("feasible"):
                        continue
                    if float(candidate_result.get("obj3") or math.inf) > obj3_cap:
                        continue
                    if not _hierarchical_preference_better(current_result, candidate_result):
                        continue
                    best_candidate = candidate
                    best_candidate_result = candidate_result
                    best_move = (block_id, current_bay, target_bay)
                    break
                if best_candidate is not None:
                    break
            if best_candidate is not None:
                break

        if best_candidate is None or best_candidate_result is None:
            break
        current = best_candidate
        current_result = best_candidate_result
        accepted += 1
        if trace_enabled:
            block_id, source_bay, target_bay = best_move
            _trace_alns_event(
                "bottleneck_preference_accept",
                accepted=accepted,
                block_id=block_id,
                source_bay=source_bay,
                target_bay=target_bay,
                objective=current_result.get("objective"),
                obj1=current_result.get("obj1"),
                obj2=current_result.get("obj2"),
                obj3=current_result.get("obj3"),
            )

    if trace_enabled:
        _trace_alns_event(
            "bottleneck_preference_relaxation",
            elapsed=time.monotonic() - started_at,
            accepted=accepted,
            quick_checks=quick_checks,
            official_checks=official_checks,
            feasible_checks=feasible_checks,
            obj3_cap=obj3_cap,
            initial_objective=initial_result.get("objective"),
            final_objective=current_result.get("objective"),
            initial_obj1=initial_result.get("obj1"),
            final_obj1=current_result.get("obj1"),
            initial_obj2=initial_result.get("obj2"),
            final_obj2=current_result.get("obj2"),
            initial_obj3=initial_result.get("obj3"),
            final_obj3=current_result.get("obj3"),
        )
    return current, current_result


def _late_hierarchical_recovery_polish(
    prob_info: dict,
    solution: dict,
    initial_result: dict,
    deadline: float,
    *,
    obj3_cap: float,
    trace_enabled: bool = False,
) -> tuple[dict, dict]:
    started_at = time.monotonic()
    current = solution
    current_result = initial_result
    improvements = 0

    while time.monotonic() < deadline - 5.0:
        before = current_result
        current, current_result = _hierarchical_preference_recovery(
            prob_info,
            current,
            current_result,
            min(deadline - 1.0, time.monotonic() + 55.0),
            trace_enabled=trace_enabled,
        )
        if time.monotonic() < deadline - 5.0 and float(current_result.get("obj3") or math.inf) < obj3_cap - 1e-6:
            current, current_result = _bottleneck_preference_relaxation(
                prob_info,
                current,
                current_result,
                min(deadline - 1.0, time.monotonic() + 35.0),
                obj3_cap=obj3_cap,
                trace_enabled=trace_enabled,
            )
        if time.monotonic() < deadline - 5.0:
            current, current_result = _exit_path_blocker_cluster_relayout(
                prob_info,
                current,
                current_result,
                min(deadline - 1.0, time.monotonic() + 25.0),
                trace_enabled=trace_enabled,
            )
        if time.monotonic() < deadline - 5.0:
            current, current_result = _same_bay_tardiness_relayout(
                prob_info,
                current,
                current_result,
                min(deadline - 1.0, time.monotonic() + 20.0),
                trace_enabled=trace_enabled,
            )
        if time.monotonic() < deadline - 5.0:
            current, current_result = _same_bay_cluster_relayout(
                prob_info,
                current,
                current_result,
                min(deadline - 1.0, time.monotonic() + 20.0),
                trace_enabled=trace_enabled,
            )
        if time.monotonic() < deadline - 5.0:
            current, current_result = _objective_safe_left_shift_polish(
                prob_info,
                current,
                current_result,
                min(deadline - 1.0, time.monotonic() + 18.0),
            )
        if not _hierarchical_preference_better(before, current_result):
            break
        improvements += 1

    if trace_enabled:
        _trace_alns_event(
            "late_hierarchical_recovery_polish",
            elapsed=time.monotonic() - started_at,
            improvements=improvements,
            initial_objective=initial_result.get("objective"),
            final_objective=current_result.get("objective"),
            initial_obj1=initial_result.get("obj1"),
            final_obj1=current_result.get("obj1"),
            initial_obj2=initial_result.get("obj2"),
            final_obj2=current_result.get("obj2"),
            initial_obj3=initial_result.get("obj3"),
            final_obj3=current_result.get("obj3"),
        )
    return current, current_result


def _hierarchical_tail_obj1_polish(
    prob_info: dict,
    solution: dict,
    initial_result: dict,
    deadline: float,
    *,
    total_budget: float,
    trace_enabled: bool = False,
    label: str,
) -> tuple[dict, dict]:
    if not initial_result.get("feasible") or float(initial_result.get("obj1") or 0.0) <= 1e-6:
        return solution, initial_result
    if time.monotonic() >= deadline - 1.0:
        return solution, initial_result

    started_at = time.monotonic()
    polished, polished_result = _objective_safe_obj1_polish(
        prob_info,
        solution,
        initial_result,
        deadline,
        total_budget=total_budget,
    )
    if not _objective_safe_obj1_better(initial_result, polished_result):
        polished = solution
        polished_result = initial_result
    if trace_enabled:
        _trace_alns_event(
            "hierarchical_tail_obj1_polish",
            label=label,
            elapsed=time.monotonic() - started_at,
            initial_objective=initial_result.get("objective"),
            final_objective=polished_result.get("objective"),
            initial_obj1=initial_result.get("obj1"),
            final_obj1=polished_result.get("obj1"),
            initial_obj2=initial_result.get("obj2"),
            final_obj2=polished_result.get("obj2"),
            initial_obj3=initial_result.get("obj3"),
            final_obj3=polished_result.get("obj3"),
        )
    return polished, polished_result


def _exact_low_obj3_cycle_polish(
    prob_info: dict,
    solution: dict,
    initial_result: dict,
    deadline: float,
    *,
    obj3_cap: float,
    trace_enabled: bool = False,
) -> tuple[dict, dict]:
    if not initial_result.get("feasible"):
        return solution, initial_result

    started_at = time.monotonic()
    current = solution
    current_result = initial_result
    cycles = 0
    tardy_improvements = 0
    obj3_improvements = 0

    while time.monotonic() < deadline - 4.0 and cycles < 6:
        cycles += 1
        changed = False

        if float(current_result.get("obj3") or math.inf) > obj3_cap - 40.0:
            while time.monotonic() < deadline - 4.0:
                candidate, candidate_result, stats = _exact_obj3_recovery_step(
                    prob_info,
                    current,
                    current_result,
                    min(deadline - 1.0, time.monotonic() + 70.0),
                )
                if candidate is None or candidate_result is None:
                    break
                current = candidate
                current_result = candidate_result
                changed = True
                obj3_improvements += 1
                if trace_enabled:
                    _trace_alns_event(
                        "exact_obj3_recovery_accept",
                        phase="pre_tardy",
                        improvements=obj3_improvements,
                        local_candidates=stats.get("local_candidates"),
                        official_checks=stats.get("official_checks"),
                        objective=current_result.get("objective"),
                        obj1=current_result.get("obj1"),
                        obj2=current_result.get("obj2"),
                        obj3=current_result.get("obj3"),
                        move=stats.get("move"),
                    )
                if float(current_result.get("obj3") or math.inf) <= obj3_cap - 120.0:
                    break

        while time.monotonic() < deadline - 4.0:
            step_obj3_cap = _objective_tradeoff_obj3_cap(prob_info, current_result, obj3_cap)
            candidate, candidate_result, stats = _exact_tardy_reinsert_step(
                prob_info,
                current,
                current_result,
                min(deadline - 1.0, time.monotonic() + 70.0),
                obj3_cap=step_obj3_cap,
            )
            if candidate is None or candidate_result is None:
                break
            current = candidate
            current_result = candidate_result
            changed = True
            tardy_improvements += 1
            if trace_enabled:
                _trace_alns_event(
                    "exact_tardy_reinsert_accept",
                    improvements=tardy_improvements,
                    local_candidates=stats.get("local_candidates"),
                    official_checks=stats.get("official_checks"),
                    objective=current_result.get("objective"),
                    obj1=current_result.get("obj1"),
                    obj2=current_result.get("obj2"),
                    obj3=current_result.get("obj3"),
                    move=stats.get("move"),
                )

        while time.monotonic() < deadline - 4.0:
            candidate, candidate_result, stats = _exact_obj3_recovery_step(
                prob_info,
                current,
                current_result,
                min(deadline - 1.0, time.monotonic() + 70.0),
            )
            if candidate is None or candidate_result is None:
                break
            current = candidate
            current_result = candidate_result
            changed = True
            obj3_improvements += 1
            if trace_enabled:
                _trace_alns_event(
                    "exact_obj3_recovery_accept",
                    improvements=obj3_improvements,
                    local_candidates=stats.get("local_candidates"),
                    official_checks=stats.get("official_checks"),
                    objective=current_result.get("objective"),
                    obj1=current_result.get("obj1"),
                    obj2=current_result.get("obj2"),
                    obj3=current_result.get("obj3"),
                    move=stats.get("move"),
                )
            if float(current_result.get("obj3") or math.inf) <= obj3_cap - 120.0:
                break

        if not changed:
            break

    if trace_enabled:
        _trace_alns_event(
            "exact_low_obj3_cycle_polish",
            elapsed=time.monotonic() - started_at,
            cycles=cycles,
            tardy_improvements=tardy_improvements,
            obj3_improvements=obj3_improvements,
            initial_objective=initial_result.get("objective"),
            final_objective=current_result.get("objective"),
            initial_obj1=initial_result.get("obj1"),
            final_obj1=current_result.get("obj1"),
            initial_obj2=initial_result.get("obj2"),
            final_obj2=current_result.get("obj2"),
            initial_obj3=initial_result.get("obj3"),
            final_obj3=current_result.get("obj3"),
        )
    return current, current_result


def _exact_tardy_probe_polish(
    prob_info: dict,
    solution: dict,
    initial_result: dict,
    deadline: float,
    *,
    obj3_cap: float,
    trace_enabled: bool = False,
) -> tuple[dict, dict]:
    if not initial_result.get("feasible"):
        return solution, initial_result

    started_at = time.monotonic()
    current = solution
    current_result = initial_result
    improvements = 0
    local_candidates = 0
    official_checks = 0

    while time.monotonic() < deadline - 2.0 and improvements < 4:
        step_obj3_cap = _objective_tradeoff_obj3_cap(prob_info, current_result, obj3_cap)
        candidate, candidate_result, stats = _exact_tardy_reinsert_step(
            prob_info,
            current,
            current_result,
            min(deadline - 1.0, time.monotonic() + 45.0),
            obj3_cap=step_obj3_cap,
        )
        local_candidates += int(stats.get("local_candidates") or 0)
        official_checks += int(stats.get("official_checks") or 0)
        if candidate is None or candidate_result is None:
            break
        current = candidate
        current_result = candidate_result
        improvements += 1

    if trace_enabled:
        _trace_alns_event(
            "exact_tardy_probe_polish",
            elapsed=time.monotonic() - started_at,
            improvements=improvements,
            local_candidates=local_candidates,
            official_checks=official_checks,
            initial_objective=initial_result.get("objective"),
            final_objective=current_result.get("objective"),
            initial_obj1=initial_result.get("obj1"),
            final_obj1=current_result.get("obj1"),
            initial_obj2=initial_result.get("obj2"),
            final_obj2=current_result.get("obj2"),
            initial_obj3=initial_result.get("obj3"),
            final_obj3=current_result.get("obj3"),
        )
    return current, current_result


def _zero_tardiness_obj3_recovery_polish(
    prob_info: dict,
    solution: dict,
    initial_result: dict,
    deadline: float,
    *,
    trace_enabled: bool = False,
) -> tuple[dict, dict]:
    if not initial_result.get("feasible") or float(initial_result.get("obj1") or math.inf) > 1e-6:
        return solution, initial_result

    started_at = time.monotonic()
    current = solution
    current_result = initial_result
    improvements = 0
    local_candidates = 0
    official_checks = 0
    while time.monotonic() < deadline - 3.0 and improvements < 16:
        candidate, candidate_result, stats = _exact_obj3_recovery_step(
            prob_info,
            current,
            current_result,
            min(deadline - 1.0, time.monotonic() + 75.0),
        )
        local_candidates += int(stats.get("local_candidates") or 0)
        official_checks += int(stats.get("official_checks") or 0)
        if candidate is None or candidate_result is None:
            break
        if not _objective_safe_secondary_better(current_result, candidate_result):
            break
        current = candidate
        current_result = candidate_result
        improvements += 1

    if trace_enabled:
        _trace_alns_event(
            "zero_tardiness_obj3_recovery_polish",
            elapsed=time.monotonic() - started_at,
            improvements=improvements,
            local_candidates=local_candidates,
            official_checks=official_checks,
            initial_objective=initial_result.get("objective"),
            final_objective=current_result.get("objective"),
            initial_obj1=initial_result.get("obj1"),
            final_obj1=current_result.get("obj1"),
            initial_obj2=initial_result.get("obj2"),
            final_obj2=current_result.get("obj2"),
            initial_obj3=initial_result.get("obj3"),
            final_obj3=current_result.get("obj3"),
        )
    return current, current_result


def _objective_tradeoff_obj3_cap(prob_info: dict, current_result: dict, base_cap: float) -> float:
    if not current_result.get("feasible"):
        return base_cap
    current_obj1 = float(current_result.get("obj1") or 0.0)
    current_obj3 = current_result.get("obj3")
    if current_obj1 <= 1e-6 or current_obj3 is None:
        return base_cap

    weights = prob_info.get("weights", {})
    w1 = float(weights.get("w1", 1.0))
    w3 = max(1.0, float(weights.get("w3", 1.0)))
    objective_safe_extra = min(1200.0, current_obj1 * w1 / w3)
    return max(base_cap, float(current_obj3) + objective_safe_extra)


def _final_entry_blocker_relocation_polish(
    prob_info: dict,
    solution: dict,
    initial_result: dict,
    deadline: float,
    *,
    trace_enabled: bool = False,
) -> tuple[dict, dict]:
    from utils import Bay, Block, check_entry, check_exit, check_feasibility

    if not initial_result.get("feasible"):
        return solution, initial_result

    started_at = time.monotonic()
    current = solution
    current_result = initial_result
    checks = 0
    blocker_candidates = 0
    improvements = 0
    blocks = prob_info["blocks"]
    bays = [Bay.from_dict(data, bay_id) for bay_id, data in enumerate(prob_info["bays"])]

    while time.monotonic() < deadline - 2.0 and improvements < 4 and checks < 500:
        assignments = _assignments_from_solution(current)
        tardy_ids = sorted(
            (
                block_id
                for block_id, assignment in assignments.items()
                if int(assignment["exit_time"]) > int(blocks[block_id]["due_date"])
            ),
            key=lambda bid: (
                -(int(assignments[bid]["exit_time"]) - int(blocks[bid]["due_date"])),
                int(blocks[bid]["due_date"]),
                bid,
            ),
        )
        if not tardy_ids:
            break

        best_candidate = None
        best_candidate_result = None
        first_seq = min(int(data.get("_seq", bid)) for bid, data in assignments.items()) - 1
        for block_id in tardy_ids:
            if time.monotonic() >= deadline - 2.0 or checks >= 500:
                break
            original = assignments[block_id]
            block_data = blocks[block_id]
            processing_time = int(block_data["processing_time"])
            due_time = int(block_data["due_date"])
            original_entry = int(original["entry_time"])
            original_exit = int(original["exit_time"])
            earliest_entry = max(int(block_data["release_time"]), due_time - processing_time)
            entry_times = [
                entry_time
                for entry_time in range(earliest_entry, original_entry)
                if entry_time + processing_time < original_exit
            ]
            if not entry_times:
                continue

            bay_id = int(original["bay_id"])
            bay = bays[bay_id]
            target_block = Block(
                block_id,
                block_data,
                x=int(original["x"]),
                y=int(original["y"]),
                orient_idx=int(original["orient_idx"]),
            )
            for desired_entry in entry_times:
                if time.monotonic() >= deadline - 2.0 or checks >= 500:
                    break
                desired_exit = desired_entry + processing_time
                shifted_assignments = {bid: dict(data) for bid, data in assignments.items()}
                shifted_assignments[block_id] = {
                    **original,
                    "entry_time": int(desired_entry),
                    "exit_time": int(desired_exit),
                    "_seq": int(first_seq),
                }
                exit_blocks = _active_blocks_for_grid_check(
                    prob_info,
                    shifted_assignments,
                    bay_id,
                    desired_exit,
                    exclude_id=block_id,
                )
                if check_exit(bay, exit_blocks + [target_block], target_block, fast=True):
                    continue

                entry_blocks = _active_blocks_for_grid_check(
                    prob_info,
                    shifted_assignments,
                    bay_id,
                    desired_entry,
                    exclude_id=block_id,
                    include_starting_at_time=False,
                )
                obstructions = check_entry(bay, entry_blocks, target_block, fast=False)
                blocker_ids = _obstruction_block_ids(obstructions, exclude_id=block_id)
                if not blocker_ids:
                    candidate = {"operations": _build_operations(shifted_assignments.values())}
                    checks += 1
                    candidate_result = check_feasibility(prob_info, candidate)
                    if _objective_safe_obj1_better(current_result, candidate_result):
                        best_candidate = candidate
                        best_candidate_result = candidate_result
                    break

                for blocker_id in blocker_ids[:8]:
                    if time.monotonic() >= deadline - 2.0 or checks >= 500:
                        break
                    for blocker_assignment in _fixed_schedule_grid_relocations(
                        prob_info,
                        shifted_assignments,
                        blocker_id,
                        deadline,
                        max_results=12,
                    ):
                        if time.monotonic() >= deadline - 2.0 or checks >= 500:
                            break
                        blocker_candidates += 1
                        candidate_assignments = {bid: dict(data) for bid, data in shifted_assignments.items()}
                        candidate_assignments[blocker_id] = blocker_assignment
                        candidate = {"operations": _build_operations(candidate_assignments.values())}
                        checks += 1
                        candidate_result = check_feasibility(prob_info, candidate)
                        if not _objective_safe_obj1_better(current_result, candidate_result):
                            continue
                        if best_candidate_result is None or _obj1_polish_rank(candidate_result) < _obj1_polish_rank(
                            best_candidate_result
                        ):
                            best_candidate = candidate
                            best_candidate_result = candidate_result
                        break
                    if best_candidate is not None:
                        break
                if best_candidate is not None:
                    break
            if best_candidate is not None:
                break

        if best_candidate is None or best_candidate_result is None:
            break
        current = best_candidate
        current_result = best_candidate_result
        improvements += 1

    if trace_enabled:
        _trace_alns_event(
            "final_entry_blocker_relocation_polish",
            elapsed=time.monotonic() - started_at,
            checks=checks,
            blocker_candidates=blocker_candidates,
            improvements=improvements,
            initial_objective=initial_result.get("objective"),
            final_objective=current_result.get("objective"),
            initial_obj1=initial_result.get("obj1"),
            final_obj1=current_result.get("obj1"),
            initial_obj2=initial_result.get("obj2"),
            final_obj2=current_result.get("obj2"),
            initial_obj3=initial_result.get("obj3"),
            final_obj3=current_result.get("obj3"),
        )
    return current, current_result


def _obstruction_block_ids(obstructions, *, exclude_id: int) -> list[int]:
    block_ids = []
    seen = set()
    for obstruction in obstructions:
        block_id = int(obstruction.existing_block.block_id)
        if block_id == exclude_id or block_id in seen:
            continue
        seen.add(block_id)
        block_ids.append(block_id)
    return block_ids


def _fixed_schedule_grid_relocations(
    prob_info: dict,
    assignments: dict[int, dict],
    block_id: int,
    deadline: float,
    *,
    max_results: int,
) -> list[dict]:
    from utils import Bay, Block, check_entry, check_exit

    blocks = prob_info["blocks"]
    block_data = blocks[block_id]
    original = assignments[block_id]
    preferences = block_data["bay_preferences"]
    best_pref = max(preferences)
    current_bay = int(original["bay_id"])
    bay_order = sorted(
        range(len(prob_info["bays"])),
        key=lambda bay_id: (
            bay_id != current_bay,
            best_pref - preferences[bay_id],
            bay_id,
        ),
    )

    candidates = []
    for bay_id in bay_order:
        if time.monotonic() >= deadline - 2.0:
            break
        bay = Bay.from_dict(prob_info["bays"][bay_id], bay_id)
        entry_time = int(original["entry_time"])
        exit_time = int(original["exit_time"])
        entry_blocks = _active_blocks_for_grid_check(
            prob_info,
            assignments,
            bay_id,
            entry_time,
            exclude_id=block_id,
        )
        exit_blocks = _active_blocks_for_grid_check(
            prob_info,
            assignments,
            bay_id,
            exit_time,
            exclude_id=block_id,
        )
        for orient_idx in range(len(block_data.get("shape", []))):
            if time.monotonic() >= deadline - 2.0:
                break
            for x in range(bay.width + 1):
                if time.monotonic() >= deadline - 2.0:
                    break
                for y in range(bay.height + 1):
                    if time.monotonic() >= deadline - 2.0:
                        break
                    if bay_id == current_bay and orient_idx == int(original["orient_idx"]) and x == int(original["x"]) and y == int(original["y"]):
                        continue
                    block = Block(block_id, block_data, x=x, y=y, orient_idx=orient_idx)
                    if not bay.contains_block(block):
                        continue
                    if check_entry(bay, entry_blocks, block, fast=True):
                        continue
                    if check_exit(bay, exit_blocks + [block], block, fast=True):
                        continue
                    regret = best_pref - preferences[bay_id]
                    candidates.append(
                        (
                            regret,
                            bay_id != current_bay,
                            y,
                            x,
                            {
                                **original,
                                "bay_id": int(bay_id),
                                "x": int(x),
                                "y": int(y),
                                "orient_idx": int(orient_idx),
                            },
                        )
                    )
                    if len(candidates) >= max_results:
                        candidates.sort(key=lambda item: item[:4])
                        return [candidate for *_score, candidate in candidates]
    candidates.sort(key=lambda item: item[:4])
    return [candidate for *_score, candidate in candidates]


def _fixed_schedule_grid_relocations_to_bay(
    prob_info: dict,
    assignments: dict[int, dict],
    block_id: int,
    bay_id: int,
    *,
    excluded_ids: set[int],
    deadline: float,
    max_results: int,
) -> list[dict]:
    from utils import Bay, Block, check_entry, check_exit

    blocks = prob_info["blocks"]
    block_data = blocks[block_id]
    original = assignments[block_id]
    bay = Bay.from_dict(prob_info["bays"][bay_id], bay_id)
    preferences = block_data["bay_preferences"]
    regret = max(preferences) - preferences[bay_id]
    entry_time = int(original["entry_time"])
    exit_time = int(original["exit_time"])
    entry_blocks = _active_blocks_for_grid_check_excluding(
        prob_info,
        assignments,
        bay_id,
        entry_time,
        excluded_ids=excluded_ids,
    )
    exit_blocks = _active_blocks_for_grid_check_excluding(
        prob_info,
        assignments,
        bay_id,
        exit_time,
        excluded_ids=excluded_ids,
    )

    candidates = []
    for orient_idx in range(len(block_data.get("shape", []))):
        if time.monotonic() >= deadline - 2.0:
            break
        for x in range(bay.width + 1):
            if time.monotonic() >= deadline - 2.0:
                break
            for y in range(bay.height + 1):
                if time.monotonic() >= deadline - 2.0:
                    break
                block = Block(block_id, block_data, x=x, y=y, orient_idx=orient_idx)
                if not bay.contains_block(block):
                    continue
                if check_entry(bay, entry_blocks, block, fast=True):
                    continue
                if check_exit(bay, exit_blocks + [block], block, fast=True):
                    continue
                candidates.append(
                    (
                        regret,
                        y,
                        x,
                        orient_idx,
                        {
                            **original,
                            "bay_id": int(bay_id),
                            "x": int(x),
                            "y": int(y),
                            "orient_idx": int(orient_idx),
                        },
                    )
                )
                if len(candidates) >= max_results:
                    candidates.sort(key=lambda item: item[:4])
                    return [candidate for *_score, candidate in candidates]
    candidates.sort(key=lambda item: item[:4])
    return [candidate for *_score, candidate in candidates]


def _active_blocks_for_grid_check_excluding(
    prob_info: dict,
    assignments: dict[int, dict],
    bay_id: int,
    at_time: int,
    *,
    excluded_ids: set[int],
) -> list:
    from utils import Block

    blocks = prob_info["blocks"]
    active = []
    for other_id, other in assignments.items():
        if other_id in excluded_ids or int(other["bay_id"]) != bay_id:
            continue
        if int(other["entry_time"]) <= at_time < int(other["exit_time"]):
            active.append(
                Block(
                    other_id,
                    blocks[other_id],
                    x=int(other["x"]),
                    y=int(other["y"]),
                    orient_idx=int(other["orient_idx"]),
                )
            )
    return active


def _final_tardy_grid_relocation_polish(
    prob_info: dict,
    solution: dict,
    initial_result: dict,
    deadline: float,
    *,
    trace_enabled: bool = False,
) -> tuple[dict, dict]:
    from utils import Bay, Block, check_entry, check_exit, check_feasibility

    if not initial_result.get("feasible"):
        return solution, initial_result

    started_at = time.monotonic()
    current = solution
    current_result = initial_result
    checks = 0
    improving_checks = 0
    improvements = 0
    blocks = prob_info["blocks"]
    bays = [Bay.from_dict(data, bay_id) for bay_id, data in enumerate(prob_info["bays"])]

    while time.monotonic() < deadline - 2.0:
        assignments = _assignments_from_solution(current)
        tardy_ids = sorted(
            (
                block_id
                for block_id, assignment in assignments.items()
                if int(assignment["exit_time"]) > int(blocks[block_id]["due_date"])
            ),
            key=lambda bid: (
                -(int(assignments[bid]["exit_time"]) - int(blocks[bid]["due_date"])),
                int(blocks[bid]["due_date"]),
                bid,
            ),
        )
        if not tardy_ids:
            break

        best_candidate = None
        best_candidate_result = None
        for block_id in tardy_ids:
            if time.monotonic() >= deadline - 2.0:
                break
            block_data = blocks[block_id]
            due_time = int(block_data["due_date"])
            processing_time = int(block_data["processing_time"])
            desired_entry = max(int(block_data["release_time"]), due_time - processing_time)
            desired_exit = desired_entry + processing_time
            if desired_exit > due_time:
                continue

            original = assignments[block_id]
            preferences = block_data["bay_preferences"]
            bay_order = sorted(range(len(bays)), key=lambda bay_id: (max(preferences) - preferences[bay_id], bay_id))
            original_entry = int(original["entry_time"])
            original_exit = int(original["exit_time"])
            earliest_entry = max(int(block_data["release_time"]), due_time - processing_time)
            entry_times = [
                entry_time
                for entry_time in range(earliest_entry, original_entry)
                if entry_time + processing_time < original_exit
            ]
            if not entry_times:
                continue
            first_seq = min(int(data.get("_seq", bid)) for bid, data in assignments.items()) - 1
            for desired_entry in entry_times:
                if time.monotonic() >= deadline - 2.0:
                    break
                desired_exit = desired_entry + processing_time
                for bay_id in bay_order:
                    if time.monotonic() >= deadline - 2.0:
                        break
                    bay = bays[bay_id]
                    entry_blocks = _active_blocks_for_grid_check(
                        prob_info,
                        assignments,
                        bay_id,
                        desired_entry,
                        exclude_id=block_id,
                        include_starting_at_time=False,
                    )
                    exit_blocks = _active_blocks_for_grid_check(
                        prob_info,
                        assignments,
                        bay_id,
                        desired_exit,
                        exclude_id=block_id,
                    )
                    for orient_idx in range(len(block_data.get("shape", []))):
                        if time.monotonic() >= deadline - 2.0:
                            break
                        for x in range(bay.width + 1):
                            if time.monotonic() >= deadline - 2.0:
                                break
                            for y in range(bay.height + 1):
                                if time.monotonic() >= deadline - 2.0:
                                    break
                                block = Block(block_id, block_data, x=x, y=y, orient_idx=orient_idx)
                                if not bay.contains_block(block):
                                    continue
                                if check_entry(bay, entry_blocks, block, fast=True):
                                    continue
                                if check_exit(bay, exit_blocks + [block], block, fast=True):
                                    continue
                                candidate_assignments = {bid: dict(data) for bid, data in assignments.items()}
                                candidate_assignments[block_id] = {
                                    **original,
                                    "bay_id": int(bay_id),
                                    "x": int(x),
                                    "y": int(y),
                                    "orient_idx": int(orient_idx),
                                    "entry_time": int(desired_entry),
                                    "exit_time": int(desired_exit),
                                    "_seq": int(first_seq),
                                }
                                candidate = {"operations": _build_operations(candidate_assignments.values())}
                                checks += 1
                                candidate_result = check_feasibility(prob_info, candidate)
                                if not _objective_safe_obj1_better(current_result, candidate_result):
                                    continue
                                improving_checks += 1
                                if best_candidate_result is None or _obj1_polish_rank(candidate_result) < _obj1_polish_rank(
                                    best_candidate_result
                                ):
                                    best_candidate = candidate
                                    best_candidate_result = candidate_result
                                break
                            if best_candidate is not None:
                                break
                        if best_candidate is not None:
                            break
                    if best_candidate is not None:
                        break
                if best_candidate is not None:
                    break
            if best_candidate is not None:
                break

        if best_candidate is None or best_candidate_result is None:
            break
        current = best_candidate
        current_result = best_candidate_result
        improvements += 1

    if trace_enabled:
        _trace_alns_event(
            "final_tardy_grid_relocation_polish",
            elapsed=time.monotonic() - started_at,
            checks=checks,
            improving_checks=improving_checks,
            improvements=improvements,
            initial_objective=initial_result.get("objective"),
            final_objective=current_result.get("objective"),
            initial_obj1=initial_result.get("obj1"),
            final_obj1=current_result.get("obj1"),
            initial_obj2=initial_result.get("obj2"),
            final_obj2=current_result.get("obj2"),
            initial_obj3=initial_result.get("obj3"),
            final_obj3=current_result.get("obj3"),
        )
    return current, current_result


def _active_blocks_for_grid_check(
    prob_info: dict,
    assignments: dict[int, dict],
    bay_id: int,
    at_time: int,
    *,
    exclude_id: int,
    include_starting_at_time: bool = True,
) -> list:
    from utils import Block

    blocks = prob_info["blocks"]
    active = []
    for other_id, other in assignments.items():
        if other_id == exclude_id or int(other["bay_id"]) != bay_id:
            continue
        other_entry = int(other["entry_time"])
        other_exit = int(other["exit_time"])
        is_active = other_entry <= at_time < other_exit if include_starting_at_time else other_entry < at_time < other_exit
        if is_active:
            active.append(
                Block(
                    other_id,
                    blocks[other_id],
                    x=int(other["x"]),
                    y=int(other["y"]),
                    orient_idx=int(other["orient_idx"]),
                )
            )
    return active


def _exact_tardy_reinsert_step(
    prob_info: dict,
    solution: dict,
    current_result: dict,
    deadline: float,
    *,
    obj3_cap: float,
) -> tuple[dict | None, dict | None, dict]:
    from utils import Bay, Block, check_entry, check_exit, check_feasibility

    assignments = _assignments_from_solution(solution)
    if not assignments:
        return None, None, {"local_candidates": 0, "official_checks": 0}

    blocks = prob_info["blocks"]
    bays = [Bay.from_dict(data, bay_id) for bay_id, data in enumerate(prob_info["bays"])]
    current_obj1 = float(current_result.get("obj1") or math.inf)
    current_objective = float(current_result.get("objective") or math.inf)
    local_candidates = 0
    official_checks = 0

    tardy_ids = sorted(
        (
            block_id
            for block_id, assignment in assignments.items()
            if int(assignment["exit_time"]) > int(blocks[block_id]["due_date"])
        ),
        key=lambda bid: (
            int(blocks[bid]["due_date"]) > 20,
            -(int(assignments[bid]["exit_time"]) - int(blocks[bid]["due_date"])),
            int(blocks[bid]["due_date"]),
            bid,
        ),
    )

    for block_id in tardy_ids:
        if time.monotonic() >= deadline - 1.0:
            break
        original = assignments[block_id]
        block_data = blocks[block_id]
        preferences = block_data["bay_preferences"]
        current_bay = int(original["bay_id"])
        current_regret = max(preferences) - preferences[current_bay]
        target_bays = []
        for bay_id, preference in enumerate(preferences):
            new_regret = max(preferences) - preference
            if float(current_result.get("obj3") or math.inf) + new_regret - current_regret <= obj3_cap + 1e-9:
                target_bays.append((new_regret - current_regret, bay_id, new_regret))
        target_bays.sort()

        block_candidates: list[tuple[tuple[float, ...], dict, dict, tuple]] = []
        for _delta_regret, bay_id, new_regret in target_bays:
            if time.monotonic() >= deadline - 1.0:
                break
            for candidate_assignment in _exact_relocation_assignments(
                prob_info,
                assignments,
                block_id,
                bay_id,
                deadline,
                improve_tardiness=True,
                preserve_obj1=False,
            ):
                candidate_assignments = {bid: dict(data) for bid, data in assignments.items()}
                candidate_assignments[block_id] = candidate_assignment
                quick_result = _objective_result_from_assignments(prob_info, candidate_assignments)
                local_candidates += 1
                if not quick_result.get("feasible"):
                    continue
                if float(quick_result.get("obj3") or math.inf) > obj3_cap + 1e-9:
                    continue
                if float(quick_result.get("obj1") or math.inf) >= current_obj1 - 1e-9:
                    continue
                if float(quick_result.get("objective") or math.inf) >= current_objective - 1e-9:
                    continue
                move = (
                    block_id,
                    current_bay,
                    bay_id,
                    int(original["entry_time"]),
                    int(original["exit_time"]),
                    int(candidate_assignment["entry_time"]),
                    int(candidate_assignment["exit_time"]),
                    int(candidate_assignment["x"]),
                    int(candidate_assignment["y"]),
                    int(candidate_assignment["orient_idx"]),
                    new_regret - current_regret,
                )
                due_time = int(block_data["due_date"])
                if due_time <= 20:
                    rank = (
                        0.0,
                        float(due_time),
                        float(quick_result["obj1"]),
                        float(quick_result["objective"]),
                        float(quick_result["obj3"]),
                    )
                else:
                    rank = (
                        1.0,
                        float(quick_result["obj1"]),
                        float(quick_result["objective"]),
                        float(quick_result["obj3"]),
                        float(due_time),
                    )
                block_candidates.append((rank, candidate_assignments, quick_result, move))

        block_candidates.sort(key=lambda item: item[0])
        for _rank, candidate_assignments, _quick_result, move in block_candidates[:8]:
            candidate = {"operations": _build_operations(candidate_assignments.values())}
            official_checks += 1
            candidate_result = check_feasibility(prob_info, candidate)
            if not candidate_result.get("feasible"):
                continue
            if float(candidate_result.get("obj3") or math.inf) > obj3_cap + 1e-9:
                continue
            if float(candidate_result.get("obj1") or math.inf) >= current_obj1 - 1e-9:
                continue
            if float(candidate_result.get("objective") or math.inf) >= current_objective - 1e-9:
                continue
            return candidate, candidate_result, {
                "local_candidates": local_candidates,
                "official_checks": official_checks,
                "move": list(move),
            }

    return None, None, {"local_candidates": local_candidates, "official_checks": official_checks}


def _exact_obj3_recovery_step(
    prob_info: dict,
    solution: dict,
    current_result: dict,
    deadline: float,
) -> tuple[dict | None, dict | None, dict]:
    from utils import Bay, Block, check_entry, check_exit, check_feasibility

    assignments = _assignments_from_solution(solution)
    if not assignments:
        return None, None, {"local_candidates": 0, "official_checks": 0}

    blocks = prob_info["blocks"]
    current_obj1 = float(current_result.get("obj1") or math.inf)
    current_obj3 = float(current_result.get("obj3") or math.inf)
    current_objective = float(current_result.get("objective") or math.inf)
    candidates: list[tuple[tuple[float, float, float], dict, dict, tuple]] = []
    local_candidates = 0
    ranked_blocks = []
    for block_id, assignment in assignments.items():
        block_data = blocks[block_id]
        preferences = block_data["bay_preferences"]
        current_bay = int(assignment["bay_id"])
        current_regret = max(preferences) - preferences[current_bay]
        if current_regret <= 0:
            continue
        slack = int(block_data["due_date"]) - int(assignment["exit_time"])
        ranked_blocks.append((-current_regret, slack, block_id, current_regret, current_bay))
    ranked_blocks.sort()

    for _negative_regret, _slack, block_id, current_regret, current_bay in ranked_blocks[:220]:
        if time.monotonic() >= deadline - 1.0:
            break
        original = assignments[block_id]
        block_data = blocks[block_id]
        preferences = block_data["bay_preferences"]
        target_bays = sorted(
            (
                bay_id
                for bay_id, preference in enumerate(preferences)
                if max(preferences) - preference < current_regret
            ),
            key=lambda bay_id: (max(preferences) - preferences[bay_id], bay_id),
        )
        for bay_id in target_bays:
            if time.monotonic() >= deadline - 1.0:
                break
            new_regret = max(preferences) - preferences[bay_id]
            for candidate_assignment in _exact_relocation_assignments(
                prob_info,
                assignments,
                block_id,
                bay_id,
                deadline,
                improve_tardiness=False,
                preserve_obj1=True,
            ):
                candidate_assignments = {bid: dict(data) for bid, data in assignments.items()}
                candidate_assignments[block_id] = candidate_assignment
                quick_result = _objective_result_from_assignments(prob_info, candidate_assignments)
                local_candidates += 1
                if not quick_result.get("feasible"):
                    continue
                if float(quick_result.get("obj1") or math.inf) > current_obj1 + 1e-9:
                    continue
                if float(quick_result.get("obj3") or math.inf) >= current_obj3 - 1e-9:
                    continue
                if float(quick_result.get("objective") or math.inf) >= current_objective - 1e-9:
                    continue
                move = (
                    block_id,
                    current_bay,
                    bay_id,
                    current_regret,
                    new_regret,
                    int(original["entry_time"]),
                    int(original["exit_time"]),
                    int(candidate_assignment["entry_time"]),
                    int(candidate_assignment["exit_time"]),
                    int(candidate_assignment["x"]),
                    int(candidate_assignment["y"]),
                    int(candidate_assignment["orient_idx"]),
                )
                rank = (
                    float(quick_result["objective"]),
                    float(quick_result["obj1"]),
                    float(quick_result["obj3"]),
                )
                candidates.append((rank, candidate_assignments, quick_result, move))

    candidates.sort(key=lambda item: item[0])
    official_checks = 0
    for _rank, candidate_assignments, _quick_result, move in candidates[:32]:
        candidate = {"operations": _build_operations(candidate_assignments.values())}
        official_checks += 1
        candidate_result = check_feasibility(prob_info, candidate)
        if not candidate_result.get("feasible"):
            continue
        if float(candidate_result.get("obj1") or math.inf) > current_obj1 + 1e-9:
            continue
        if float(candidate_result.get("obj3") or math.inf) >= current_obj3 - 1e-9:
            continue
        if float(candidate_result.get("objective") or math.inf) >= current_objective - 1e-9:
            continue
        return candidate, candidate_result, {
            "local_candidates": local_candidates,
            "official_checks": official_checks,
            "move": list(move),
        }

    return None, None, {"local_candidates": local_candidates, "official_checks": official_checks}


def _exact_relocation_assignments(
    prob_info: dict,
    assignments: dict[int, dict],
    block_id: int,
    bay_id: int,
    deadline: float,
    *,
    improve_tardiness: bool,
    preserve_obj1: bool,
) -> list[dict]:
    from utils import Bay, Block, check_entry, check_exit

    if time.monotonic() >= deadline - 1.0:
        return []

    blocks = prob_info["blocks"]
    block_data = blocks[block_id]
    original = assignments[block_id]
    bay = Bay.from_dict(prob_info["bays"][bay_id], bay_id)
    placed_blocks = []
    schedule = []
    for other_id, other in assignments.items():
        if other_id == block_id or int(other["bay_id"]) != bay_id:
            continue
        placed_blocks.append(
            Block(
                other_id,
                blocks[other_id],
                x=int(other["x"]),
                y=int(other["y"]),
                orient_idx=int(other["orient_idx"]),
            )
        )
        schedule.append((int(other["entry_time"]), int(other["exit_time"])))

    release_time = int(block_data["release_time"])
    processing_time = int(block_data["processing_time"])
    due_time = int(block_data["due_date"])
    original_entry = int(original["entry_time"])
    original_exit = int(original["exit_time"])
    original_tardiness = max(0, original_exit - due_time)

    if improve_tardiness:
        latest_entry = min(original_entry - 1, due_time - processing_time)
        if latest_entry < release_time:
            return []
        entry_candidates = list(range(release_time, latest_entry + 1))
        entry_candidates.sort(reverse=True)
    else:
        latest_entry = due_time + original_tardiness - processing_time if preserve_obj1 else original_entry
        if latest_entry < release_time:
            return []
        if latest_entry - release_time > 24:
            entry_candidates = {release_time, latest_entry, original_entry}
            for entry_time, exit_time in schedule:
                if release_time <= entry_time <= latest_entry:
                    entry_candidates.add(int(entry_time))
                if release_time <= exit_time <= latest_entry:
                    entry_candidates.add(int(exit_time))
            entry_candidates = sorted(entry_candidates, reverse=True)
        else:
            entry_candidates = list(range(release_time, latest_entry + 1))
            entry_candidates.sort(reverse=True)

    result: list[dict] = []
    extra_positions = [(int(original["x"]), int(original["y"]))]
    for orient_idx in range(len(block_data.get("shape", []))):
        if time.monotonic() >= deadline - 1.0:
            break
        bbox = orientation_bbox(block_data, orient_idx)
        positions = _exact_position_candidates(bay, placed_blocks, bbox, extra_positions)
        for entry_time in entry_candidates:
            if time.monotonic() >= deadline - 1.0:
                break
            exit_time = int(entry_time) + processing_time
            present_at_entry = [
                other for other, (other_entry, other_exit) in zip(placed_blocks, schedule)
                if other_entry <= entry_time < other_exit
            ]
            present_at_exit = [
                other for other, (other_entry, other_exit) in zip(placed_blocks, schedule)
                if other_entry < exit_time < other_exit
            ]
            active_blocks = [
                other for other, (other_entry, other_exit) in zip(placed_blocks, schedule)
                if entry_time < other_exit and other_entry < exit_time
            ]
            for x, y in positions:
                block = Block(block_id, block_data, x=int(x), y=int(y), orient_idx=int(orient_idx))
                if not bay.contains_block(block):
                    continue
                if check_entry(bay, present_at_entry, block, fast=True):
                    continue
                if check_exit(bay, [block, *present_at_exit], block, fast=True):
                    continue
                if any(_cached_pair_collision(bay, block, other) for other in active_blocks):
                    continue
                if _blocks_earlier_due_exits(
                    bay,
                    block,
                    placed_blocks,
                    schedule,
                    int(entry_time),
                    int(exit_time),
                ):
                    continue
                result.append(
                    {
                        **original,
                        "bay_id": int(bay_id),
                        "x": int(x),
                        "y": int(y),
                        "orient_idx": int(orient_idx),
                        "entry_time": int(entry_time),
                        "exit_time": int(exit_time),
                    }
                )
    return result


def _exact_position_candidates(bay, placed_blocks, bbox, extra_positions: list[tuple[int, int]]) -> list[tuple[int, int]]:
    min_x, min_y, max_x, max_y = bbox
    width = max_x - min_x
    height = max_y - min_y
    xs = {0, int(max(0, math.floor((bay.width - width) / 2 - min_x)))}
    ys = {0, int(max(0, math.floor((bay.height - height) / 2 - min_y)))}
    for x, y in extra_positions:
        xs.add(int(x))
        ys.add(int(y))
    for block in placed_blocks:
        rect = block.bounding_rect()
        for value in (
            math.floor(rect[0] - max_x),
            math.ceil(rect[2] - min_x),
            math.floor(rect[0] - min_x),
            math.ceil(rect[2] - max_x),
            math.floor(rect[2] - max_x),
            math.ceil(rect[0] - min_x),
        ):
            xs.add(int(value))
        for value in (
            math.floor(rect[1] - max_y),
            math.ceil(rect[3] - min_y),
            math.floor(rect[1] - min_y),
            math.ceil(rect[3] - max_y),
            math.floor(rect[3] - max_y),
            math.ceil(rect[1] - min_y),
        ):
            ys.add(int(value))

    result = []
    for x in sorted(xs):
        if x + max_x > bay.width + 1e-6 or x + min_x < -1e-6:
            continue
        for y in sorted(ys):
            if y + max_y <= bay.height + 1e-6 and y + min_y >= -1e-6:
                result.append((int(x), int(y)))
    return result


def _tardy_rows_by_bay(prob_info: dict, assignments: dict[int, dict]) -> list[tuple[int, float, list[tuple[int, float]]]]:
    rows_by_bay: dict[int, list[tuple[int, float]]] = {}
    for block_id, assignment in assignments.items():
        due_time = int(prob_info["blocks"][block_id]["due_date"])
        tardiness = max(0.0, float(int(assignment["exit_time"]) - due_time))
        if tardiness <= 0.0:
            continue
        bay_id = int(assignment["bay_id"])
        rows_by_bay.setdefault(bay_id, []).append((block_id, tardiness))

    result = []
    for bay_id, rows in rows_by_bay.items():
        rows.sort(
            key=lambda item: (
                -item[1],
                int(prob_info["blocks"][item[0]]["due_date"]),
                item[0],
            )
        )
        result.append((bay_id, sum(tardiness for _block_id, tardiness in rows), rows))
    result.sort(key=lambda item: (-item[1], item[0]))
    return result


def _hierarchical_preference_recovery(
    prob_info: dict,
    solution: dict,
    initial_result: dict,
    deadline: float,
    *,
    trace_enabled: bool = False,
) -> tuple[dict, dict]:
    from utils import check_feasibility

    if not initial_result.get("feasible"):
        return solution, initial_result

    started_at = time.monotonic()
    current = solution
    current_result = initial_result
    max_checks = 80 if len(prob_info["blocks"]) >= 150 else 120
    max_improvements = 40 if len(prob_info["blocks"]) >= 150 else 60
    checks = 0
    feasible_checks = 0
    improvements = 0

    while time.monotonic() < deadline - 2.0 and checks < max_checks and improvements < max_improvements:
        assignments = _assignments_from_solution(current)
        accepted_candidate = None
        accepted_result = None
        accepted_move = None
        for block_id in _preference_polish_block_order(prob_info, assignments)[:64]:
            if time.monotonic() >= deadline - 2.0 or checks >= max_checks:
                break
            original_bay = int(assignments[block_id]["bay_id"])
            recovery_candidates = _active_preference_recovery_assignments(
                prob_info,
                assignments,
                block_id,
                deadline,
                max_bays=4,
            )
            recovery_candidates.extend(
                _preference_relocation_assignments(
                    prob_info,
                    assignments,
                    block_id,
                    deadline,
                    max_bays=3,
                    max_positions=20,
                )
            )
            for candidate_assignment in recovery_candidates:
                if time.monotonic() >= deadline - 2.0 or checks >= max_checks:
                    break
                candidate_assignments = {bid: dict(data) for bid, data in assignments.items()}
                candidate_assignments[block_id] = candidate_assignment
                quick_result = _objective_result_from_assignments(prob_info, candidate_assignments)
                if not _hierarchical_preference_recovery_better(current_result, quick_result):
                    continue
                candidate = {"operations": _build_operations(candidate_assignments.values())}
                checks += 1
                candidate_result = check_feasibility(prob_info, candidate)
                if candidate_result.get("feasible"):
                    feasible_checks += 1
                if not _hierarchical_preference_recovery_better(current_result, candidate_result):
                    continue
                accepted_candidate = candidate
                accepted_result = candidate_result
                accepted_move = (block_id, original_bay, int(candidate_assignment["bay_id"]))
                break
            if accepted_candidate is not None:
                break

        if accepted_candidate is None or accepted_result is None:
            break
        current = accepted_candidate
        current_result = accepted_result
        improvements += 1
        if trace_enabled:
            block_id, source_bay, target_bay = accepted_move
            _trace_alns_event(
                "hierarchical_preference_recovery_accept",
                improvements=improvements,
                block_id=block_id,
                source_bay=source_bay,
                target_bay=target_bay,
                objective=current_result.get("objective"),
                obj1=current_result.get("obj1"),
                obj2=current_result.get("obj2"),
                obj3=current_result.get("obj3"),
            )

    if trace_enabled:
        _trace_alns_event(
            "hierarchical_preference_recovery",
            elapsed=time.monotonic() - started_at,
            checks=checks,
            feasible_checks=feasible_checks,
            improvements=improvements,
            initial_objective=initial_result.get("objective"),
            final_objective=current_result.get("objective"),
            initial_obj1=initial_result.get("obj1"),
            final_obj1=current_result.get("obj1"),
            initial_obj2=initial_result.get("obj2"),
            final_obj2=current_result.get("obj2"),
            initial_obj3=initial_result.get("obj3"),
            final_obj3=current_result.get("obj3"),
        )
    return current, current_result


def _hierarchical_preference_recovery_better(current_result: dict, candidate_result: dict) -> bool:
    if not candidate_result.get("feasible") or not current_result.get("feasible"):
        return False
    current_obj1 = float(current_result.get("obj1") or math.inf)
    candidate_obj1 = float(candidate_result.get("obj1") or math.inf)
    current_obj3 = float(current_result.get("obj3") or math.inf)
    candidate_obj3 = float(candidate_result.get("obj3") or math.inf)
    current_objective = float(current_result.get("objective") or math.inf)
    candidate_objective = float(candidate_result.get("objective") or math.inf)
    return (
        candidate_obj1 <= current_obj1 + 1e-6
        and candidate_obj3 < current_obj3 - 1e-6
        and candidate_objective < current_objective - 1e-6
    )


def _active_preference_recovery_assignments(
    prob_info: dict,
    assignments: dict[int, dict],
    block_id: int,
    deadline: float,
    *,
    max_bays: int,
) -> list[dict]:
    from utils import Bay

    if time.monotonic() >= deadline - 2.0:
        return []
    blocks = prob_info["blocks"]
    block_data = blocks[block_id]
    original = assignments[block_id]
    current_bay = int(original["bay_id"])
    preferences = block_data["bay_preferences"]
    current_pref = preferences[current_bay]
    better_bays = [
        bay_id
        for bay_id in sorted(range(len(prob_info["bays"])), key=lambda idx: (-preferences[idx], idx))
        if bay_id != current_bay and preferences[bay_id] > current_pref
    ][:max_bays]
    if not better_bays:
        return []

    bays = [Bay.from_dict(data, bay_id) for bay_id, data in enumerate(prob_info["bays"])]
    bay_blocks, bay_schedules, _bay_loads = _state_from_assignments(
        prob_info,
        {bid: data for bid, data in assignments.items() if bid != block_id},
        bays,
    )
    result = []
    for bay_id in better_bays:
        if time.monotonic() >= deadline - 2.0:
            break
        assignment = _active_aware_preference_assignment(
            prob_info,
            block_id,
            bay_id,
            bays,
            bay_blocks,
            bay_schedules,
            min(deadline, time.monotonic() + 2.0),
        )
        if assignment is None:
            continue
        result.append({**assignment, "_seq": int(original.get("_seq", block_id))})
    return result


def _same_bay_tardiness_relayout(
    prob_info: dict,
    solution: dict,
    initial_result: dict,
    deadline: float,
    *,
    trace_enabled: bool = False,
) -> tuple[dict, dict]:
    from utils import Bay, check_feasibility

    if not initial_result.get("feasible"):
        return solution, initial_result

    started_at = time.monotonic()
    current = solution
    current_result = initial_result
    blocks = prob_info["blocks"]
    bays = [Bay.from_dict(data, bay_id) for bay_id, data in enumerate(prob_info["bays"])]
    max_checks = 96 if len(blocks) >= 150 else 160
    max_improvements = 50 if len(blocks) >= 150 else 80
    checks = 0
    feasible_checks = 0
    improvements = 0

    while time.monotonic() < deadline - 2.0 and checks < max_checks and improvements < max_improvements:
        assignments = _assignments_from_solution(current)
        tardy_ids = sorted(
            (
                block_id
                for block_id, assignment in assignments.items()
                if int(assignment["exit_time"]) > int(blocks[block_id]["due_date"])
            ),
            key=lambda block_id: (
                -(int(assignments[block_id]["exit_time"]) - int(blocks[block_id]["due_date"])),
                int(blocks[block_id]["due_date"]),
                block_id,
            ),
        )[:60]
        if not tardy_ids:
            break

        accepted_candidate = None
        accepted_result = None
        accepted_move = None
        for block_id in tardy_ids:
            if time.monotonic() >= deadline - 2.0 or checks >= max_checks:
                break
            original = assignments[block_id]
            bay_id = int(original["bay_id"])
            bay_blocks, bay_schedules, _bay_loads = _state_from_assignments(
                prob_info,
                {bid: data for bid, data in assignments.items() if bid != block_id},
                bays,
            )
            assignment = _active_aware_preference_assignment(
                prob_info,
                block_id,
                bay_id,
                bays,
                bay_blocks,
                bay_schedules,
                min(deadline, time.monotonic() + 2.0),
            )
            if assignment is None:
                continue
            assignment = {**assignment, "_seq": int(original.get("_seq", block_id))}
            if (
                int(assignment["entry_time"]) == int(original["entry_time"])
                and int(assignment["exit_time"]) == int(original["exit_time"])
                and int(assignment["x"]) == int(original["x"])
                and int(assignment["y"]) == int(original["y"])
                and int(assignment["orient_idx"]) == int(original["orient_idx"])
            ):
                continue
            candidate_assignments = {bid: dict(data) for bid, data in assignments.items()}
            candidate_assignments[block_id] = assignment
            quick_result = _objective_result_from_assignments(prob_info, candidate_assignments)
            if not _same_bay_relayout_better(current_result, quick_result):
                continue
            candidate = {"operations": _build_operations(candidate_assignments.values())}
            checks += 1
            candidate_result = check_feasibility(prob_info, candidate)
            if candidate_result.get("feasible"):
                feasible_checks += 1
            if not _same_bay_relayout_better(current_result, candidate_result):
                continue
            accepted_candidate = candidate
            accepted_result = candidate_result
            accepted_move = (block_id, bay_id)
            break

        if accepted_candidate is None or accepted_result is None:
            break
        current = accepted_candidate
        current_result = accepted_result
        improvements += 1
        if trace_enabled:
            block_id, bay_id = accepted_move
            _trace_alns_event(
                "same_bay_relayout_accept",
                improvements=improvements,
                block_id=block_id,
                bay_id=bay_id,
                objective=current_result.get("objective"),
                obj1=current_result.get("obj1"),
                obj2=current_result.get("obj2"),
                obj3=current_result.get("obj3"),
            )

    if trace_enabled:
        _trace_alns_event(
            "same_bay_tardiness_relayout",
            elapsed=time.monotonic() - started_at,
            checks=checks,
            feasible_checks=feasible_checks,
            improvements=improvements,
            initial_objective=initial_result.get("objective"),
            final_objective=current_result.get("objective"),
            initial_obj1=initial_result.get("obj1"),
            final_obj1=current_result.get("obj1"),
            initial_obj2=initial_result.get("obj2"),
            final_obj2=current_result.get("obj2"),
            initial_obj3=initial_result.get("obj3"),
            final_obj3=current_result.get("obj3"),
        )
    return current, current_result


def _same_bay_relayout_better(current_result: dict, candidate_result: dict) -> bool:
    if not candidate_result.get("feasible") or not current_result.get("feasible"):
        return False
    current_obj1 = float(current_result.get("obj1") or math.inf)
    candidate_obj1 = float(candidate_result.get("obj1") or math.inf)
    current_obj3 = float(current_result.get("obj3") or math.inf)
    candidate_obj3 = float(candidate_result.get("obj3") or math.inf)
    current_objective = float(current_result.get("objective") or math.inf)
    candidate_objective = float(candidate_result.get("objective") or math.inf)
    return (
        candidate_obj1 < current_obj1 - 1e-6
        and candidate_obj3 <= current_obj3 + 1e-6
        and candidate_objective < current_objective - 1e-6
    )


def _same_bay_cluster_relayout(
    prob_info: dict,
    solution: dict,
    initial_result: dict,
    deadline: float,
    *,
    trace_enabled: bool = False,
) -> tuple[dict, dict]:
    from utils import Bay, check_feasibility

    if not initial_result.get("feasible"):
        return solution, initial_result

    started_at = time.monotonic()
    current = solution
    current_result = initial_result
    blocks = prob_info["blocks"]
    bays = [Bay.from_dict(data, bay_id) for bay_id, data in enumerate(prob_info["bays"])]
    max_checks = 60 if len(blocks) >= 150 else 100
    max_improvements = 18 if len(blocks) >= 150 else 30
    checks = 0
    feasible_checks = 0
    improvements = 0

    while time.monotonic() < deadline - 2.0 and checks < max_checks and improvements < max_improvements:
        assignments = _assignments_from_solution(current)
        clusters = _same_bay_relayout_clusters(prob_info, assignments, cluster_size=3, max_clusters=20)
        if not clusters:
            break

        accepted_candidate = None
        accepted_result = None
        accepted_cluster = None
        for cluster in clusters:
            if time.monotonic() >= deadline - 2.0 or checks >= max_checks:
                break
            base_assignments = {bid: dict(data) for bid, data in assignments.items() if bid not in cluster}
            candidate_assignments = {bid: dict(data) for bid, data in base_assignments.items()}
            bay_blocks, bay_schedules, bay_loads = _state_from_assignments(prob_info, base_assignments, bays)
            failed = False
            for block_id in sorted(
                cluster,
                key=lambda bid: (
                    int(blocks[bid]["due_date"]),
                    int(blocks[bid]["release_time"]),
                    int(blocks[bid]["processing_time"]),
                    bid,
                ),
            ):
                original = assignments[block_id]
                bay_id = int(original["bay_id"])
                assignment = _active_aware_preference_assignment(
                    prob_info,
                    block_id,
                    bay_id,
                    bays,
                    bay_blocks,
                    bay_schedules,
                    min(deadline, time.monotonic() + 2.0),
                )
                if assignment is None:
                    failed = True
                    break
                assignment = {**assignment, "_seq": int(original.get("_seq", block_id))}
                candidate_assignments[block_id] = assignment
                _append_assignment_to_state(assignment, blocks, bay_blocks, bay_schedules, bay_loads)
            if failed:
                continue
            quick_result = _objective_result_from_assignments(prob_info, candidate_assignments)
            if not _same_bay_relayout_better(current_result, quick_result):
                continue
            candidate = {"operations": _build_operations(candidate_assignments.values())}
            checks += 1
            candidate_result = check_feasibility(prob_info, candidate)
            if candidate_result.get("feasible"):
                feasible_checks += 1
            if not _same_bay_relayout_better(current_result, candidate_result):
                continue
            accepted_candidate = candidate
            accepted_result = candidate_result
            accepted_cluster = tuple(cluster)
            break

        if accepted_candidate is None or accepted_result is None:
            break
        current = accepted_candidate
        current_result = accepted_result
        improvements += 1
        if trace_enabled:
            _trace_alns_event(
                "same_bay_cluster_relayout_accept",
                improvements=improvements,
                cluster=list(accepted_cluster),
                objective=current_result.get("objective"),
                obj1=current_result.get("obj1"),
                obj2=current_result.get("obj2"),
                obj3=current_result.get("obj3"),
            )

    if trace_enabled:
        _trace_alns_event(
            "same_bay_cluster_relayout",
            elapsed=time.monotonic() - started_at,
            checks=checks,
            feasible_checks=feasible_checks,
            improvements=improvements,
            initial_objective=initial_result.get("objective"),
            final_objective=current_result.get("objective"),
            initial_obj1=initial_result.get("obj1"),
            final_obj1=current_result.get("obj1"),
            initial_obj2=initial_result.get("obj2"),
            final_obj2=current_result.get("obj2"),
            initial_obj3=initial_result.get("obj3"),
            final_obj3=current_result.get("obj3"),
        )
    return current, current_result


def _same_bay_relayout_clusters(
    prob_info: dict,
    assignments: dict[int, dict],
    *,
    cluster_size: int,
    max_clusters: int,
) -> list[list[int]]:
    blocks = prob_info["blocks"]
    tardy_ids = sorted(
        (
            block_id
            for block_id, assignment in assignments.items()
            if int(assignment["exit_time"]) > int(blocks[block_id]["due_date"])
        ),
        key=lambda bid: (
            -(int(assignments[bid]["exit_time"]) - int(blocks[bid]["due_date"])),
            int(blocks[bid]["due_date"]),
            bid,
        ),
    )[:40]
    clusters: list[list[int]] = []
    seen: set[tuple[int, ...]] = set()
    for target_id in tardy_ids:
        target = assignments[target_id]
        target_bay = int(target["bay_id"])
        target_entry = int(target["entry_time"])
        target_exit = int(target["exit_time"])
        neighbors = []
        for other_id, other in assignments.items():
            if other_id == target_id or int(other["bay_id"]) != target_bay:
                continue
            other_entry = int(other["entry_time"])
            other_exit = int(other["exit_time"])
            overlap = max(0, min(target_exit, other_exit) - max(target_entry, other_entry))
            near_gap = min(abs(other_exit - target_entry), abs(other_entry - target_exit))
            if overlap <= 0 and near_gap > 2:
                continue
            other_tardiness = max(0, other_exit - int(blocks[other_id]["due_date"]))
            neighbors.append(
                (
                    -overlap,
                    near_gap,
                    -other_tardiness,
                    int(blocks[other_id]["due_date"]),
                    other_id,
                )
            )
        if not neighbors:
            continue
        cluster = [target_id]
        for *_score, other_id in sorted(neighbors):
            if other_id not in cluster:
                cluster.append(other_id)
            if len(cluster) >= cluster_size:
                break
        if len(cluster) < 2:
            continue
        key = tuple(sorted(cluster))
        if key in seen:
            continue
        seen.add(key)
        clusters.append(cluster)
        if len(clusters) >= max_clusters:
            break
    return clusters


def _exit_path_blocker_cluster_relayout(
    prob_info: dict,
    solution: dict,
    initial_result: dict,
    deadline: float,
    *,
    trace_enabled: bool = False,
) -> tuple[dict, dict]:
    from utils import Bay, check_feasibility

    if not initial_result.get("feasible"):
        return solution, initial_result

    started_at = time.monotonic()
    current = solution
    current_result = initial_result
    blocks = prob_info["blocks"]
    bays = [Bay.from_dict(data, bay_id) for bay_id, data in enumerate(prob_info["bays"])]
    max_checks = 48 if len(blocks) >= 150 else 96
    max_improvements = 18 if len(blocks) >= 150 else 30
    checks = 0
    feasible_checks = 0
    improvements = 0

    while time.monotonic() < deadline - 2.0 and checks < max_checks and improvements < max_improvements:
        assignments = _assignments_from_solution(current)
        clusters = _exit_path_blocker_clusters(prob_info, assignments, max_clusters=24, max_size=5)
        if not clusters:
            break

        accepted_candidate = None
        accepted_result = None
        accepted_cluster = None
        accepted_order = None
        for cluster in clusters:
            if time.monotonic() >= deadline - 2.0 or checks >= max_checks:
                break
            for order in _exit_path_reinsert_orders(prob_info, assignments, cluster):
                if time.monotonic() >= deadline - 2.0 or checks >= max_checks:
                    break
                base_assignments = {bid: dict(data) for bid, data in assignments.items() if bid not in cluster}
                candidate_assignments = {bid: dict(data) for bid, data in base_assignments.items()}
                bay_blocks, bay_schedules, bay_loads = _state_from_assignments(prob_info, base_assignments, bays)
                failed = False
                for block_id in order:
                    original = assignments[block_id]
                    bay_id = int(original["bay_id"])
                    assignment = _active_aware_preference_assignment(
                        prob_info,
                        block_id,
                        bay_id,
                        bays,
                        bay_blocks,
                        bay_schedules,
                        min(deadline, time.monotonic() + 2.0),
                    )
                    if assignment is None:
                        failed = True
                        break
                    assignment = {**assignment, "_seq": int(original.get("_seq", block_id))}
                    candidate_assignments[block_id] = assignment
                    _append_assignment_to_state(assignment, blocks, bay_blocks, bay_schedules, bay_loads)
                if failed:
                    continue

                quick_result = _objective_result_from_assignments(prob_info, candidate_assignments)
                if not _same_bay_relayout_better(current_result, quick_result):
                    continue
                candidate = {"operations": _build_operations(candidate_assignments.values())}
                checks += 1
                candidate_result = check_feasibility(prob_info, candidate)
                if candidate_result.get("feasible"):
                    feasible_checks += 1
                if not _same_bay_relayout_better(current_result, candidate_result):
                    continue
                accepted_candidate = candidate
                accepted_result = candidate_result
                accepted_cluster = tuple(cluster)
                accepted_order = tuple(order)
                break
            if accepted_candidate is not None:
                break

        if accepted_candidate is None or accepted_result is None:
            break
        current = accepted_candidate
        current_result = accepted_result
        improvements += 1
        if trace_enabled:
            _trace_alns_event(
                "exit_path_blocker_cluster_accept",
                improvements=improvements,
                cluster=list(accepted_cluster),
                order=list(accepted_order),
                objective=current_result.get("objective"),
                obj1=current_result.get("obj1"),
                obj2=current_result.get("obj2"),
                obj3=current_result.get("obj3"),
            )

    if trace_enabled:
        _trace_alns_event(
            "exit_path_blocker_cluster_relayout",
            elapsed=time.monotonic() - started_at,
            checks=checks,
            feasible_checks=feasible_checks,
            improvements=improvements,
            initial_objective=initial_result.get("objective"),
            final_objective=current_result.get("objective"),
            initial_obj1=initial_result.get("obj1"),
            final_obj1=current_result.get("obj1"),
            initial_obj2=initial_result.get("obj2"),
            final_obj2=current_result.get("obj2"),
            initial_obj3=initial_result.get("obj3"),
            final_obj3=current_result.get("obj3"),
        )
    return current, current_result


def _exit_path_blocker_clusters(
    prob_info: dict,
    assignments: dict[int, dict],
    *,
    max_clusters: int,
    max_size: int,
) -> list[list[int]]:
    from utils import Bay, Block, check_entry, check_exit, check_collisions

    blocks = prob_info["blocks"]
    bays = [Bay.from_dict(data, bay_id) for bay_id, data in enumerate(prob_info["bays"])]
    tardy_ids = sorted(
        (
            block_id
            for block_id, assignment in assignments.items()
            if int(assignment["exit_time"]) > int(blocks[block_id]["due_date"])
        ),
        key=lambda bid: (
            -(int(assignments[bid]["exit_time"]) - int(blocks[bid]["due_date"])),
            int(blocks[bid]["due_date"]),
            bid,
        ),
    )[:40]
    clusters: list[list[int]] = []
    seen: set[tuple[int, ...]] = set()

    for target_id in tardy_ids:
        target_assignment = assignments[target_id]
        bay_id = int(target_assignment["bay_id"])
        block_data = blocks[target_id]
        release_time = int(block_data["release_time"])
        processing_time = int(block_data["processing_time"])
        due_time = int(block_data["due_date"])
        target_entry = max(release_time, due_time - processing_time)
        target_exit = target_entry + processing_time
        if target_exit >= int(target_assignment["exit_time"]):
            continue

        target_block = Block(
            target_id,
            block_data,
            x=int(target_assignment["x"]),
            y=int(target_assignment["y"]),
            orient_idx=int(target_assignment["orient_idx"]),
        )
        same_bay = [
            (
                other_id,
                other,
                Block(
                    other_id,
                    blocks[other_id],
                    x=int(other["x"]),
                    y=int(other["y"]),
                    orient_idx=int(other["orient_idx"]),
                ),
            )
            for other_id, other in assignments.items()
            if other_id != target_id and int(other["bay_id"]) == bay_id
        ]
        present_at_entry = [
            other_block
            for _other_id, other, other_block in same_bay
            if int(other["entry_time"]) <= target_entry < int(other["exit_time"])
        ]
        present_at_exit = [
            target_block,
            *[
                other_block
                for _other_id, other, other_block in same_bay
                if int(other["entry_time"]) < target_exit < int(other["exit_time"])
            ],
        ]
        active = [
            other_block
            for _other_id, other, other_block in same_bay
            if target_entry < int(other["exit_time"]) and int(other["entry_time"]) < target_exit
        ]

        blocker_counts: dict[int, float] = {}
        for obs in check_entry(bays[bay_id], present_at_entry, target_block, fast=False):
            blocker_id = int(obs.existing_block.block_id)
            if blocker_id != target_id:
                blocker_counts[blocker_id] = blocker_counts.get(blocker_id, 0.0) + 4.0
        for obs in check_exit(bays[bay_id], present_at_exit, target_block, fast=False):
            blocker_id = int(obs.existing_block.block_id)
            if blocker_id != target_id:
                blocker_counts[blocker_id] = blocker_counts.get(blocker_id, 0.0) + 5.0
        for collision in check_collisions(bays[bay_id], [target_block, *active]):
            if collision.block_a.block_id == target_id:
                blocker_id = int(collision.block_b.block_id)
            elif collision.block_b.block_id == target_id:
                blocker_id = int(collision.block_a.block_id)
            else:
                continue
            blocker_counts[blocker_id] = blocker_counts.get(blocker_id, 0.0) + 2.0

        if not blocker_counts:
            continue
        ranked_blockers = sorted(
            blocker_counts,
            key=lambda bid: (
                -blocker_counts[bid],
                int(blocks[bid]["due_date"]),
                int(assignments[bid]["entry_time"]),
                bid,
            ),
        )
        cluster = [target_id]
        for blocker_id in ranked_blockers:
            if blocker_id not in cluster:
                cluster.append(blocker_id)
            if len(cluster) >= max_size:
                break
        if len(cluster) < 2:
            continue
        key = tuple(sorted(cluster))
        if key in seen:
            continue
        seen.add(key)
        clusters.append(cluster)
        if len(clusters) >= max_clusters:
            break
    return clusters


def _exit_path_reinsert_orders(
    prob_info: dict,
    assignments: dict[int, dict],
    cluster: list[int],
) -> list[list[int]]:
    import itertools

    blocks = prob_info["blocks"]
    target_id = cluster[0]
    blockers = sorted(
        cluster[1:],
        key=lambda bid: (
            int(blocks[bid]["due_date"]),
            int(blocks[bid]["release_time"]),
            int(assignments[bid]["entry_time"]),
            bid,
        ),
    )
    due_order = sorted(
        cluster,
        key=lambda bid: (
            int(blocks[bid]["due_date"]),
            int(blocks[bid]["release_time"]),
            int(assignments[bid]["entry_time"]),
            bid,
        ),
    )
    release_order = sorted(
        cluster,
        key=lambda bid: (
            int(blocks[bid]["release_time"]),
            int(blocks[bid]["due_date"]),
            int(assignments[bid]["entry_time"]),
            bid,
        ),
    )
    candidates = [
        [target_id, *blockers],
        due_order,
        release_order,
        [*blockers, target_id],
    ]
    if len(cluster) <= 4:
        candidates.extend(list(order) for order in itertools.permutations(cluster))

    result = []
    seen: set[tuple[int, ...]] = set()
    for order in candidates:
        clean = []
        for block_id in order:
            if block_id in cluster and block_id not in clean:
                clean.append(block_id)
        for block_id in cluster:
            if block_id not in clean:
                clean.append(block_id)
        key = tuple(clean)
        if key in seen:
            continue
        seen.add(key)
        result.append(clean)
    return result


def _exact_release_assignment(
    prob_info: dict,
    block_id: int,
    bays,
    bay_blocks,
    bay_schedules,
    bay_loads,
    deadline: float,
    prefer_bays: bool = False,
) -> dict | None:
    from utils import Block, check_entry, check_exit

    block_data = prob_info["blocks"][block_id]
    entry_time = int(block_data["release_time"])
    exit_time = entry_time + int(block_data["processing_time"])
    due_time = int(block_data["due_date"])
    preferences = block_data["bay_preferences"]
    s_max = max(preferences)
    best = None

    if prefer_bays:
        bay_order = sorted(
            range(len(bays)),
            key=lambda bay_id: (
                s_max - preferences[bay_id],
                bay_loads[bay_id],
                bay_id,
            ),
        )
    else:
        bay_order = sorted(
            range(len(bays)),
            key=lambda bay_id: (
                bay_loads[bay_id],
                -preferences[bay_id],
                bay_id,
            ),
        )
    for bay_id in bay_order:
        if time.monotonic() >= deadline - 0.2:
            break
        bay = bays[bay_id]
        present_at_entry = [
            other
            for other, (entry, exit_) in zip(bay_blocks[bay_id], bay_schedules[bay_id])
            if entry <= entry_time < exit_
        ]
        present_at_exit = [
            other
            for other, (entry, exit_) in zip(bay_blocks[bay_id], bay_schedules[bay_id])
            if entry < exit_time < exit_
        ]
        for orient_idx in range(len(block_data.get("shape", []))):
            if time.monotonic() >= deadline - 0.2:
                break
            bbox = orientation_bbox(block_data, orient_idx)
            for x, y in _candidate_positions(bay, bay_blocks[bay_id], bbox)[:40]:
                if time.monotonic() >= deadline - 0.2:
                    break
                block = Block(block_id, block_data, x=x, y=y, orient_idx=orient_idx)
                if not bay.contains_block(block):
                    continue
                if check_entry(bay, present_at_entry, block, fast=True):
                    continue
                if check_exit(bay, [block, *present_at_exit], block, fast=True):
                    continue
                if _release_slot_collides(bay, block, bay_blocks[bay_id], bay_schedules[bay_id], entry_time, exit_time):
                    continue
                if _blocks_earlier_due_exits(
                    bay,
                    block,
                    bay_blocks[bay_id],
                    bay_schedules[bay_id],
                    entry_time,
                    exit_time,
                ):
                    continue
                score = (
                    max(0, exit_time - due_time),
                    bay_loads[bay_id],
                    s_max - preferences[bay_id],
                    y + bbox[3],
                    bay_id,
                    orient_idx,
                )
                assignment = {
                    "block_id": block_id,
                    "bay_id": bay_id,
                    "x": int(x),
                    "y": int(y),
                    "orient_idx": int(orient_idx),
                    "entry_time": int(entry_time),
                    "exit_time": int(exit_time),
                }
                if best is None or score < best[0]:
                    best = (score, assignment)

    return None if best is None else best[1]


def _fast_release_assignment(
    prob_info: dict,
    block_id: int,
    bays,
    bay_blocks,
    bay_schedules,
    bay_loads,
    prefer_bays: bool = False,
) -> dict | None:
    from utils import Block, check_entry, check_exit

    block_data = prob_info["blocks"][block_id]
    entry_time = int(block_data["release_time"])
    exit_time = entry_time + int(block_data["processing_time"])
    preferences = block_data["bay_preferences"]
    best = None
    s_max = max(preferences)
    if prefer_bays:
        bay_order = sorted(
            range(len(bays)),
            key=lambda bay_id: (
                s_max - preferences[bay_id],
                bay_loads[bay_id],
                bay_id,
            ),
        )
    else:
        bay_order = sorted(
            range(len(bays)),
            key=lambda bay_id: (
                bay_loads[bay_id],
                -preferences[bay_id],
                bay_id,
            ),
        )

    for bay_id in bay_order:
        bay = bays[bay_id]
        present_at_entry = [
            other
            for other, (entry, exit_) in zip(bay_blocks[bay_id], bay_schedules[bay_id])
            if entry <= entry_time < exit_
        ]
        present_at_exit = [
            other
            for other, (entry, exit_) in zip(bay_blocks[bay_id], bay_schedules[bay_id])
            if entry < exit_time < exit_
        ]
        for orient_idx in range(len(block_data.get("shape", []))):
            bbox = orientation_bbox(block_data, orient_idx)
            width = bbox[2] - bbox[0]
            height = bbox[3] - bbox[1]
            lower_left = lower_left_integer_position(bbox)
            candidates = [
                lower_left,
                (
                    max(0, int((bay.width - width) // 2 - bbox[0])),
                    max(0, int((bay.height - height) // 2 - bbox[1])),
                ),
                (max(0, int(bay.width - width - bbox[0])), lower_left[1]),
                (lower_left[0], max(0, int(bay.height - height - bbox[1]))),
            ]
            seen: set[tuple[int, int]] = set()
            for x, y in candidates:
                x = int(x)
                y = int(y)
                if (x, y) in seen:
                    continue
                seen.add((x, y))
                block = Block(block_id, block_data, x=x, y=y, orient_idx=orient_idx)
                if not bay.contains_block(block):
                    continue
                if check_entry(bay, present_at_entry, block, fast=True):
                    continue
                if check_exit(bay, [block, *present_at_exit], block, fast=True):
                    continue
                if _release_slot_collides(
                    bay, block, bay_blocks[bay_id], bay_schedules[bay_id], entry_time, exit_time
                ):
                    continue
                if _blocks_earlier_due_exits(
                    bay,
                    block,
                    bay_blocks[bay_id],
                    bay_schedules[bay_id],
                    entry_time,
                    exit_time,
                ):
                    continue
                blocker_penalty = _exit_blocking_penalty_units(
                    bay,
                    block,
                    bay_blocks[bay_id],
                    bay_schedules[bay_id],
                    entry_time,
                    exit_time,
                    limit=8,
                )
                assignment = {
                    "block_id": block_id,
                    "bay_id": bay_id,
                    "x": x,
                    "y": y,
                    "orient_idx": int(orient_idx),
                    "entry_time": int(entry_time),
                    "exit_time": int(exit_time),
                }
                if blocker_penalty <= 1e-6:
                    return assignment
                if prefer_bays:
                    score = (
                        blocker_penalty,
                        s_max - preferences[bay_id],
                        bay_loads[bay_id],
                        y + bbox[3],
                        bay_id,
                        orient_idx,
                    )
                else:
                    score = (
                        blocker_penalty,
                        bay_loads[bay_id],
                        -preferences[bay_id],
                        y + bbox[3],
                        bay_id,
                        orient_idx,
                    )
                if best is None or score < best[0]:
                    best = (score, assignment)

    return None if best is None else best[1]


def _release_slot_collides(bay, block, placed_blocks, schedule, entry_time: int, exit_time: int) -> bool:
    for other, (other_entry, other_exit) in zip(placed_blocks, schedule):
        if entry_time < other_exit and other_entry < exit_time:
            if _cached_pair_collision(bay, block, other):
                return True
    return False


def _rank_feasible_seeds(prob_info: dict, seeds: list[dict]) -> list[dict]:
    from utils import check_feasibility

    ranked = []
    seen = set()
    for seed in seeds:
        result = check_feasibility(prob_info, seed)
        if not result.get("feasible"):
            continue
        key = (result.get("objective"), result.get("obj1"), result.get("obj2"), result.get("obj3"))
        if key in seen:
            continue
        seen.add(key)
        ranked.append((float(result["objective"]), float(result.get("obj1") or 0.0), seed))
    ranked.sort(key=lambda item: (item[0], item[1]))
    return [seed for _, _, seed in ranked[:2]]


def _initial_polish_deadline(total_budget: float, search_deadline: float) -> float:
    """Reserve an early compression pass before the main ALNS loop.

    Long runs used to skip this pass and then only had a tiny final reserve for
    compression, which could make 300s worse than 60s on large instances.
    """

    now = time.monotonic()
    if total_budget < 120.0:
        return search_deadline
    polish_budget = min(90.0, max(20.0, total_budget * 0.15))
    return min(search_deadline, now + polish_budget)


def _repair_by_reinsertion(prob_info: dict, solution: dict, deadline: float, max_passes: int = 4) -> dict:
    from utils import check_feasibility

    current = solution
    for _ in range(max_passes):
        result = check_feasibility(prob_info, current)
        if result.get("feasible") or time.monotonic() >= deadline:
            return current
        removed = _violation_block_ids(result.get("violations", []))
        if not removed:
            return current
        assignments = _assignments_from_solution(current)
        current = _repair_removed(prob_info, assignments, removed, deadline)
        if current is None:
            return solution
    return current


def _construct_seed(prob_info: dict, deadline: float, require_access: bool) -> dict:
    blocks = prob_info["blocks"]
    block_ids = sorted(
        range(len(blocks)),
        key=lambda bid: (
            int(blocks[bid]["due_date"]),
            int(blocks[bid]["release_time"]),
            -_fit_difficulty(blocks[bid]),
            bid,
        ),
    )
    return _build_by_insertion(prob_info, block_ids, {}, deadline, require_access=require_access)


def _repair_removed(
    prob_info: dict,
    assignments: dict[int, dict],
    removed: list[int],
    deadline: float,
    use_regret: bool = False,
    allow_timeout_fallback: bool = True,
    use_exit_blocking_penalty: bool = False,
    prioritize_tardy_first: bool = False,
    preserve_removed_order: bool = False,
    use_multi_slot: bool = False,
    use_preference_bias: bool = False,
) -> dict | None:
    remaining = {bid: assignment for bid, assignment in assignments.items() if bid not in removed}
    blocks = prob_info["blocks"]
    if preserve_removed_order:
        order = [bid for bid in removed if bid in assignments]
    elif prioritize_tardy_first:
        order = sorted(
            removed,
            key=lambda bid: (
                -max(0, int(assignments[bid]["exit_time"]) - int(blocks[bid]["due_date"])),
                int(blocks[bid]["due_date"]),
                bid,
            ),
        )
    else:
        order = sorted(
            removed,
            key=lambda bid: (
                int(blocks[bid]["due_date"]),
                -max(0, int(assignments[bid]["exit_time"]) - int(blocks[bid]["due_date"])),
                bid,
            ),
        )
    if use_regret:
        if preserve_removed_order and len(order) > 1:
            first_solution = _build_by_insertion(
                prob_info,
                order[:1],
                remaining,
                deadline,
                require_access=True,
                allow_timeout_fallback=allow_timeout_fallback,
                use_exit_blocking_penalty=use_exit_blocking_penalty,
                use_multi_slot=use_multi_slot,
                use_preference_bias=use_preference_bias,
            )
            if first_solution is None:
                return None
            remaining = _assignments_from_solution(first_solution)
            order = order[1:]
        return _build_by_regret(
            prob_info,
            order,
            remaining,
            deadline,
            allow_timeout_fallback=allow_timeout_fallback,
            use_exit_blocking_penalty=use_exit_blocking_penalty,
            use_multi_slot=use_multi_slot,
            use_preference_bias=use_preference_bias,
        )
    return _build_by_insertion(
        prob_info,
        order,
        remaining,
        deadline,
        require_access=True,
        allow_timeout_fallback=allow_timeout_fallback,
        use_exit_blocking_penalty=use_exit_blocking_penalty,
        use_multi_slot=use_multi_slot,
        use_preference_bias=use_preference_bias,
    )


def _build_by_insertion(
    prob_info: dict,
    block_ids: Iterable[int],
    fixed_assignments: dict[int, dict],
    deadline: float,
    require_access: bool,
    allow_timeout_fallback: bool = True,
    use_exit_blocking_penalty: bool = False,
    use_multi_slot: bool = False,
    use_preference_bias: bool = False,
) -> dict | None:
    from utils import Bay, Block

    bays = [Bay.from_dict(data, bay_id) for bay_id, data in enumerate(prob_info["bays"])]
    blocks = prob_info["blocks"]
    assignments = dict(fixed_assignments)
    bay_blocks, bay_schedules, bay_loads = _state_from_assignments(prob_info, assignments, bays)

    for block_id in block_ids:
        if time.monotonic() >= deadline:
            if not allow_timeout_fallback:
                return None
            assignments[block_id] = _serial_append_assignment(
                prob_info, block_id, bays, bay_schedules, bay_loads
            )
        else:
            assignment = _best_insertion(
                prob_info,
                block_id,
                bays,
                bay_blocks,
                bay_schedules,
                bay_loads,
                deadline,
                require_access=require_access,
                use_exit_blocking_penalty=use_exit_blocking_penalty,
                use_multi_slot=use_multi_slot,
                use_preference_bias=use_preference_bias,
            )
            if assignment is None:
                assignment = _serial_append_assignment(
                    prob_info, block_id, bays, bay_schedules, bay_loads
                )
            assignments[block_id] = assignment

        _append_assignment_to_state(assignments[block_id], blocks, bay_blocks, bay_schedules, bay_loads)

    return {"operations": _build_operations(assignments.values())}


def _build_by_regret(
    prob_info: dict,
    block_ids: Iterable[int],
    fixed_assignments: dict[int, dict],
    deadline: float,
    allow_timeout_fallback: bool = True,
    use_exit_blocking_penalty: bool = False,
    use_multi_slot: bool = False,
    use_preference_bias: bool = False,
) -> dict | None:
    from utils import Bay

    bays = [Bay.from_dict(data, bay_id) for bay_id, data in enumerate(prob_info["bays"])]
    blocks = prob_info["blocks"]
    assignments = dict(fixed_assignments)
    bay_blocks, bay_schedules, bay_loads = _state_from_assignments(prob_info, assignments, bays)
    pending = list(block_ids)

    while pending:
        if time.monotonic() >= deadline:
            if not allow_timeout_fallback:
                return None
            for block_id in pending:
                assignments[block_id] = _serial_append_assignment(
                    prob_info, block_id, bays, bay_schedules, bay_loads
                )
                _append_assignment_to_state(assignments[block_id], blocks, bay_blocks, bay_schedules, bay_loads)
            break

        best_choice = None
        for block_id in pending:
            ranked = _ranked_insertions(
                prob_info,
                block_id,
                bays,
                bay_blocks,
                bay_schedules,
                bay_loads,
                deadline,
                require_access=True,
                limit=3,
                use_exit_blocking_penalty=use_exit_blocking_penalty,
                use_multi_slot=use_multi_slot,
                use_preference_bias=use_preference_bias,
            )
            if ranked:
                best_score, assignment = ranked[0]
                second_score = ranked[1][0] if len(ranked) > 1 else best_score + 1_000_000.0
                third_score = ranked[2][0] if len(ranked) > 2 else second_score + 500_000.0
                regret = (second_score - best_score) + 0.25 * (third_score - best_score)
                priority = (
                    regret,
                    max(0, int(assignment["exit_time"]) - int(blocks[block_id]["due_date"])),
                    -best_score,
                    -int(blocks[block_id]["due_date"]),
                    block_id,
                )
            else:
                assignment = _serial_append_assignment(prob_info, block_id, bays, bay_schedules, bay_loads)
                priority = (
                    float("inf"),
                    max(0, int(assignment["exit_time"]) - int(blocks[block_id]["due_date"])),
                    float("-inf"),
                    -int(blocks[block_id]["due_date"]),
                    block_id,
                )

            if best_choice is None or priority > best_choice[0]:
                best_choice = (priority, block_id, assignment)

        _, chosen_id, chosen_assignment = best_choice
        assignments[chosen_id] = chosen_assignment
        _append_assignment_to_state(chosen_assignment, blocks, bay_blocks, bay_schedules, bay_loads)
        pending.remove(chosen_id)

    return {"operations": _build_operations(assignments.values())}


def _compress_solution(prob_info: dict, solution: dict, deadline: float) -> dict:
    from utils import Bay, Block, check_feasibility

    current_result = check_feasibility(prob_info, solution)
    if not current_result.get("feasible"):
        return solution

    assignments = _assignments_from_solution(solution)
    if not assignments:
        return solution

    blocks = prob_info["blocks"]
    orders = [
        sorted(
            assignments,
            key=lambda bid: (
                int(blocks[bid]["due_date"]),
                int(blocks[bid]["release_time"]),
                int(assignments[bid]["exit_time"]),
                bid,
            ),
        ),
        sorted(
            assignments,
            key=lambda bid: (
                int(assignments[bid]["entry_time"]),
                int(blocks[bid]["due_date"]),
                bid,
            ),
        ),
    ]

    best_solution = solution
    best_objective = float(current_result["objective"])
    for order in orders:
        if time.monotonic() >= deadline - 0.3:
            break

        candidate = _rebuild_fixed_placements(prob_info, assignments, order, deadline)
        if candidate is None:
            continue
        result = check_feasibility(prob_info, candidate)
        if result.get("feasible") and float(result["objective"]) < best_objective:
            best_solution = candidate
            best_objective = float(result["objective"])
            assignments = _assignments_from_solution(best_solution)

    return best_solution


def _objective_safe_obj1_polish(
    prob_info: dict,
    solution: dict,
    initial_result: dict,
    deadline: float,
    total_budget: float | None = None,
) -> tuple[dict, dict]:
    from utils import check_feasibility

    current = solution
    current_result = initial_result
    if not current_result.get("feasible"):
        return solution, current_result
    if float(current_result.get("obj1") or 0.0) <= 1e-6:
        return solution, current_result

    if len(prob_info["blocks"]) < 150:
        max_rounds = 2
    elif total_budget is not None and total_budget >= 240.0:
        max_rounds = 5
    else:
        max_rounds = 3
    for _ in range(max_rounds):
        if time.monotonic() >= deadline - 0.4:
            break

        assignments = _assignments_from_solution(current)
        best_candidate = None
        best_candidate_result = None
        current_obj1 = float(current_result.get("obj1") or 0.0)
        long_obj1_repair = total_budget is not None and total_budget >= 240.0 and current_obj1 > 1e-6
        allow_chain = total_budget is None or total_budget < 240.0 or long_obj1_repair
        deep_exit_repair = total_budget is not None and (
            90.0 <= total_budget < 210.0 or long_obj1_repair
        )
        for removed, use_regret, preserve_order in _obj1_polish_destroy_sets(
            prob_info, assignments, deadline, allow_chain, deep_exit_repair
        ):
            if time.monotonic() >= deadline - 0.4:
                break
            if not removed:
                continue

            candidate = _repair_removed(
                prob_info,
                assignments,
                removed,
                deadline,
                use_regret=use_regret,
                allow_timeout_fallback=False,
                use_exit_blocking_penalty=True,
                prioritize_tardy_first=allow_chain,
                preserve_removed_order=preserve_order,
                use_multi_slot=deep_exit_repair,
            )
            if candidate is None:
                continue

            candidate_result = check_feasibility(prob_info, candidate)
            if _objective_safe_obj1_better(current_result, candidate_result):
                if best_candidate_result is None or _obj1_polish_rank(candidate_result) < _obj1_polish_rank(
                    best_candidate_result
                ):
                    best_candidate = candidate
                    best_candidate_result = candidate_result

        if best_candidate is None or best_candidate_result is None:
            break
        current = best_candidate
        current_result = best_candidate_result

    return current, current_result


def _objective_safe_obj1_better(current_result: dict, candidate_result: dict) -> bool:
    if not candidate_result.get("feasible"):
        return False
    if not current_result.get("feasible"):
        return True
    current_objective = current_result.get("objective")
    candidate_objective = candidate_result.get("objective")
    current_obj1 = current_result.get("obj1")
    candidate_obj1 = candidate_result.get("obj1")
    if None in (current_objective, candidate_objective, current_obj1, candidate_obj1):
        return False
    return (
        float(candidate_objective) <= float(current_objective) + 1e-6
        and float(candidate_obj1) < float(current_obj1) - 1e-6
    )


def _objective_safe_left_shift_polish(
    prob_info: dict,
    solution: dict,
    initial_result: dict,
    deadline: float,
) -> tuple[dict, dict]:
    from utils import check_feasibility

    if not initial_result.get("feasible"):
        return solution, initial_result

    current = solution
    current_result = initial_result
    blocks = prob_info["blocks"]
    n_blocks = len(blocks)
    max_checks = 120 if n_blocks >= 150 else 240
    checks = 0
    time_margin = 2.0 if n_blocks >= 150 else 0.7

    while time.monotonic() < deadline - time_margin and checks < max_checks:
        assignments = _assignments_from_solution(current)
        tardy_ids = sorted(
            (
                bid
                for bid, assignment in assignments.items()
                if int(assignment["exit_time"]) > int(blocks[bid]["due_date"])
            ),
            key=lambda bid: (
                -(int(assignments[bid]["exit_time"]) - int(blocks[bid]["due_date"])),
                int(blocks[bid]["due_date"]),
                bid,
            ),
        )[:40]
        if not tardy_ids:
            break

        best_candidate = None
        best_candidate_result = None
        for block_id in tardy_ids:
            if time.monotonic() >= deadline - time_margin or checks >= max_checks:
                break
            assignment = assignments[block_id]
            max_shift = int(assignment["entry_time"]) - int(blocks[block_id]["release_time"])
            tardiness = int(assignment["exit_time"]) - int(blocks[block_id]["due_date"])
            for shift in _left_shift_amounts(max_shift, tardiness):
                if time.monotonic() >= deadline - time_margin or checks >= max_checks:
                    break
                shifted_assignments = {bid: dict(data) for bid, data in assignments.items()}
                shifted_assignments[block_id]["entry_time"] = int(assignment["entry_time"]) - shift
                shifted_assignments[block_id]["exit_time"] = int(assignment["exit_time"]) - shift
                if not _left_shift_local_precheck(prob_info, shifted_assignments, block_id):
                    continue
                candidate = {"operations": _build_operations(shifted_assignments.values())}
                checks += 1
                candidate_result = check_feasibility(prob_info, candidate)
                if not _objective_safe_obj1_better(current_result, candidate_result):
                    continue
                if best_candidate_result is None or _obj1_polish_rank(candidate_result) < _obj1_polish_rank(
                    best_candidate_result
                ):
                    best_candidate = candidate
                    best_candidate_result = candidate_result

        if best_candidate is None or best_candidate_result is None:
            break
        current = best_candidate
        current_result = best_candidate_result

    return current, current_result


def _left_shift_amounts(max_shift: int, tardiness: int) -> list[int]:
    if max_shift <= 0 or tardiness <= 0:
        return []

    ordered: list[int] = []

    def add(value: int) -> None:
        if 1 <= value <= max_shift and value not in ordered:
            ordered.append(value)

    due_shift = min(max_shift, tardiness)
    add(due_shift)
    for center in (due_shift, max_shift):
        for delta in (-2, -1, 1, 2):
            add(center + delta)
    add(max_shift)
    add(min(max_shift, max(1, tardiness // 2)))
    add(min(max_shift, max(1, (tardiness + 1) // 2)))
    add(1)
    return ordered


def _left_shift_local_precheck(prob_info: dict, assignments: dict[int, dict], block_id: int) -> bool:
    from utils import Bay, Block, check_entry, check_exit

    assignment = assignments[block_id]
    bay_id = int(assignment["bay_id"])
    bay = Bay.from_dict(prob_info["bays"][bay_id], bay_id)
    blocks = prob_info["blocks"]
    target = Block(
        block_id,
        blocks[block_id],
        x=int(assignment["x"]),
        y=int(assignment["y"]),
        orient_idx=int(assignment["orient_idx"]),
    )
    entry_time = int(assignment["entry_time"])
    exit_time = int(assignment["exit_time"])

    same_bay: list[tuple[int, dict, Block]] = []
    for other_id, other in assignments.items():
        if other_id == block_id or int(other["bay_id"]) != bay_id:
            continue
        same_bay.append(
            (
                other_id,
                other,
                Block(
                    other_id,
                    blocks[other_id],
                    x=int(other["x"]),
                    y=int(other["y"]),
                    orient_idx=int(other["orient_idx"]),
                ),
            )
        )

    present_at_entry = [
        other_block
        for _, other, other_block in same_bay
        if int(other["entry_time"]) <= entry_time < int(other["exit_time"])
    ]
    if check_entry(bay, present_at_entry, target, fast=True):
        return False

    present_at_exit = [
        target,
        *[
            other_block
            for _, other, other_block in same_bay
            if int(other["entry_time"]) < exit_time < int(other["exit_time"])
        ],
    ]
    if check_exit(bay, present_at_exit, target, fast=True):
        return False

    for _, other, other_block in same_bay:
        other_entry = int(other["entry_time"])
        other_exit = int(other["exit_time"])
        if entry_time < other_exit and other_entry < exit_time and _cached_pair_collision(bay, target, other_block):
            return False
    return True


def _objective_safe_cluster_left_shift_polish(
    prob_info: dict,
    solution: dict,
    initial_result: dict,
    deadline: float,
) -> tuple[dict, dict]:
    from utils import check_feasibility

    if not initial_result.get("feasible"):
        return solution, initial_result

    current = solution
    current_result = initial_result
    blocks = prob_info["blocks"]
    checks = 0
    max_checks = 36 if len(blocks) >= 150 else 80

    while time.monotonic() < deadline - 3.0 and checks < max_checks:
        assignments = _assignments_from_solution(current)
        clusters = _actual_exit_blocker_removal_sets(
            prob_info,
            assignments,
            count=3,
            deadline=deadline,
            max_sets=3,
        )
        clusters.extend(
            _same_bay_tardy_clusters(
                prob_info,
                assignments,
                max_clusters=3,
                cluster_size=3,
            )
        )
        if not clusters:
            break

        best_candidate = None
        best_candidate_result = None
        seen_clusters: set[tuple[int, ...]] = set()
        for cluster in clusters:
            if time.monotonic() >= deadline - 3.0 or checks >= max_checks:
                break
            cluster = [block_id for block_id in cluster if block_id in assignments]
            key = tuple(cluster)
            if not cluster or key in seen_clusters:
                continue
            seen_clusters.add(key)

            max_shift = min(
                int(assignments[block_id]["entry_time"]) - int(blocks[block_id]["release_time"])
                for block_id in cluster
            )
            target_id = cluster[0]
            target_tardiness = int(assignments[target_id]["exit_time"]) - int(blocks[target_id]["due_date"])
            for shift in _left_shift_amounts(max_shift, target_tardiness):
                if time.monotonic() >= deadline - 3.0 or checks >= max_checks:
                    break
                shifted_assignments = {bid: dict(data) for bid, data in assignments.items()}
                for block_id in cluster:
                    shifted_assignments[block_id]["entry_time"] = int(assignments[block_id]["entry_time"]) - shift
                    shifted_assignments[block_id]["exit_time"] = int(assignments[block_id]["exit_time"]) - shift
                if not all(_left_shift_local_precheck(prob_info, shifted_assignments, bid) for bid in cluster):
                    continue

                candidate = {"operations": _build_operations(shifted_assignments.values())}
                checks += 1
                candidate_result = check_feasibility(prob_info, candidate)
                if not _objective_safe_obj1_better(current_result, candidate_result):
                    continue
                if best_candidate_result is None or _obj1_polish_rank(candidate_result) < _obj1_polish_rank(
                    best_candidate_result
                ):
                    best_candidate = candidate
                    best_candidate_result = candidate_result

        if best_candidate is None or best_candidate_result is None:
            break
        current = best_candidate
        current_result = best_candidate_result

    return current, current_result


def _obj1_low_enough_for_secondary(result: dict) -> bool:
    if not result.get("feasible"):
        return False
    obj1 = result.get("obj1")
    if obj1 is None:
        return False
    return float(obj1) <= 100.0


def _objective_safe_secondary_better(current_result: dict, candidate_result: dict) -> bool:
    if not candidate_result.get("feasible"):
        return False
    if not current_result.get("feasible"):
        return True
    current_objective = current_result.get("objective")
    candidate_objective = candidate_result.get("objective")
    current_obj1 = current_result.get("obj1")
    candidate_obj1 = candidate_result.get("obj1")
    current_obj3 = current_result.get("obj3")
    candidate_obj3 = candidate_result.get("obj3")
    if None in (current_objective, candidate_objective, current_obj1, candidate_obj1, current_obj3, candidate_obj3):
        return False
    return (
        float(candidate_obj1) <= float(current_obj1) + 1e-6
        and float(candidate_obj3) < float(current_obj3) - 1e-6
        and float(candidate_objective) < float(current_objective) - 1e-6
    )


def _objective_safe_preference_polish(
    prob_info: dict,
    solution: dict,
    initial_result: dict,
    deadline: float,
    *,
    trace_enabled: bool = False,
) -> tuple[dict, dict]:
    from utils import check_feasibility

    if not initial_result.get("feasible") or not _obj1_low_enough_for_secondary(initial_result):
        return solution, initial_result

    started_at = time.monotonic()
    current = solution
    current_result = initial_result
    max_checks = 48 if len(prob_info["blocks"]) >= 150 else 96
    checks = 0
    proxy_checks = 0
    proxy_improving_checks = 0
    feasible_checks = 0
    improving_checks = 0
    improvements = 0
    max_proxy_checks = max_checks * 10

    while time.monotonic() < deadline - 2.0 and checks < max_checks and proxy_checks < max_proxy_checks:
        assignments = _assignments_from_solution(current)
        best_candidate = None
        best_candidate_result = None
        best_proxy_candidate = None
        best_proxy_result = None

        for block_id in _preference_polish_block_order(prob_info, assignments)[:28]:
            if time.monotonic() >= deadline - 2.0 or checks >= max_checks or proxy_checks >= max_proxy_checks:
                break
            for candidate_assignment in _preference_relocation_assignments(
                prob_info,
                assignments,
                block_id,
                deadline,
                max_bays=3,
                max_positions=24,
            ):
                if time.monotonic() >= deadline - 2.0 or checks >= max_checks or proxy_checks >= max_proxy_checks:
                    break
                candidate_assignments = {bid: dict(data) for bid, data in assignments.items()}
                candidate_assignments[block_id] = candidate_assignment
                proxy_checks += 1
                proxy_result = _objective_result_from_assignments(prob_info, candidate_assignments)
                if not _objective_safe_secondary_better(current_result, proxy_result):
                    continue
                proxy_improving_checks += 1
                candidate = {"operations": _build_operations(candidate_assignments.values())}
                if best_proxy_result is None or _secondary_polish_rank(proxy_result) < _secondary_polish_rank(
                    best_proxy_result
                ):
                    best_proxy_candidate = candidate
                    best_proxy_result = proxy_result
                if proxy_improving_checks % 5 != 0:
                    continue
                checks += 1
                candidate_result = check_feasibility(prob_info, candidate)
                if candidate_result.get("feasible"):
                    feasible_checks += 1
                if not _objective_safe_secondary_better(current_result, candidate_result):
                    continue
                improving_checks += 1
                if best_candidate_result is None or _secondary_polish_rank(candidate_result) < _secondary_polish_rank(
                    best_candidate_result
                ):
                    best_candidate = candidate
                    best_candidate_result = candidate_result

        if (
            best_candidate is None
            and best_proxy_candidate is not None
            and checks < max_checks
            and time.monotonic() < deadline - 2.0
        ):
            checks += 1
            candidate_result = check_feasibility(prob_info, best_proxy_candidate)
            if candidate_result.get("feasible"):
                feasible_checks += 1
            if _objective_safe_secondary_better(current_result, candidate_result):
                improving_checks += 1
                best_candidate = best_proxy_candidate
                best_candidate_result = candidate_result

        if best_candidate is None or best_candidate_result is None:
            break
        current = best_candidate
        current_result = best_candidate_result
        improvements += 1

    if trace_enabled:
        _trace_alns_event(
            "preference_polish",
            elapsed=time.monotonic() - started_at,
            checks=checks,
            proxy_checks=proxy_checks,
            proxy_improving_checks=proxy_improving_checks,
            feasible_checks=feasible_checks,
            improving_checks=improving_checks,
            improvements=improvements,
            initial_objective=initial_result.get("objective"),
            final_objective=current_result.get("objective"),
            initial_obj1=initial_result.get("obj1"),
            final_obj1=current_result.get("obj1"),
            initial_obj2=initial_result.get("obj2"),
            final_obj2=current_result.get("obj2"),
            initial_obj3=initial_result.get("obj3"),
            final_obj3=current_result.get("obj3"),
        )
    if time.monotonic() < deadline - 2.0:
        current, current_result = _objective_safe_preference_swap_polish(
            prob_info,
            current,
            current_result,
            deadline,
            trace_enabled=trace_enabled,
        )
    return current, current_result


def _objective_safe_preference_swap_polish(
    prob_info: dict,
    solution: dict,
    initial_result: dict,
    deadline: float,
    *,
    trace_enabled: bool = False,
) -> tuple[dict, dict]:
    from utils import check_feasibility

    if not initial_result.get("feasible") or not _obj1_low_enough_for_secondary(initial_result):
        return solution, initial_result

    started_at = time.monotonic()
    current = solution
    current_result = initial_result
    checks = 0
    feasible_checks = 0
    improving_checks = 0
    improvements = 0
    max_checks = 36 if len(prob_info["blocks"]) >= 150 else 80

    while time.monotonic() < deadline - 2.0 and checks < max_checks:
        assignments = _assignments_from_solution(current)
        pairs = _preference_swap_pairs(prob_info, assignments, max_pairs=36)
        if not pairs:
            break

        best_candidate = None
        best_candidate_result = None
        for first_id, second_id, _gain in pairs:
            if time.monotonic() >= deadline - 2.0 or checks >= max_checks:
                break
            first = assignments[first_id]
            second = assignments[second_id]
            first_target_bay = int(second["bay_id"])
            second_target_bay = int(first["bay_id"])
            first_candidates = _preference_assignment_candidates_in_bay(
                prob_info,
                assignments,
                first_id,
                first_target_bay,
                excluded_ids={first_id, second_id},
                deadline=deadline,
                max_positions=12,
                max_results=2,
            )
            if not first_candidates:
                continue
            second_candidates = _preference_assignment_candidates_in_bay(
                prob_info,
                assignments,
                second_id,
                second_target_bay,
                excluded_ids={first_id, second_id},
                deadline=deadline,
                max_positions=12,
                max_results=2,
            )
            if not second_candidates:
                continue

            for first_candidate in first_candidates:
                if time.monotonic() >= deadline - 2.0 or checks >= max_checks:
                    break
                for second_candidate in second_candidates:
                    if time.monotonic() >= deadline - 2.0 or checks >= max_checks:
                        break
                    candidate_assignments = {bid: dict(data) for bid, data in assignments.items()}
                    candidate_assignments[first_id] = first_candidate
                    candidate_assignments[second_id] = second_candidate
                    candidate = {"operations": _build_operations(candidate_assignments.values())}
                    checks += 1
                    candidate_result = check_feasibility(prob_info, candidate)
                    if candidate_result.get("feasible"):
                        feasible_checks += 1
                    if not _objective_safe_secondary_better(current_result, candidate_result):
                        continue
                    improving_checks += 1
                    if best_candidate_result is None or _secondary_polish_rank(candidate_result) < _secondary_polish_rank(
                        best_candidate_result
                    ):
                        best_candidate = candidate
                        best_candidate_result = candidate_result

        if best_candidate is None or best_candidate_result is None:
            break
        current = best_candidate
        current_result = best_candidate_result
        improvements += 1

    if trace_enabled:
        _trace_alns_event(
            "preference_swap_polish",
            elapsed=time.monotonic() - started_at,
            checks=checks,
            feasible_checks=feasible_checks,
            improving_checks=improving_checks,
            improvements=improvements,
            initial_objective=initial_result.get("objective"),
            final_objective=current_result.get("objective"),
            initial_obj1=initial_result.get("obj1"),
            final_obj1=current_result.get("obj1"),
            initial_obj2=initial_result.get("obj2"),
            final_obj2=current_result.get("obj2"),
            initial_obj3=initial_result.get("obj3"),
            final_obj3=current_result.get("obj3"),
        )
    return current, current_result


def _objective_safe_fixed_schedule_preference_swap_polish(
    prob_info: dict,
    solution: dict,
    initial_result: dict,
    deadline: float,
    *,
    trace_enabled: bool = False,
) -> tuple[dict, dict]:
    from utils import check_feasibility

    if not initial_result.get("feasible") or not _obj1_low_enough_for_secondary(initial_result):
        return solution, initial_result

    started_at = time.monotonic()
    current = solution
    current_result = initial_result
    checks = 0
    proxy_checks = 0
    proxy_improving_checks = 0
    feasible_checks = 0
    improving_checks = 0
    improvements = 0
    max_checks = 220 if len(prob_info["blocks"]) >= 150 else 100
    max_proxy_checks = max_checks * 10

    while time.monotonic() < deadline - 2.0 and checks < max_checks and proxy_checks < max_proxy_checks:
        assignments = _assignments_from_solution(current)
        pairs = _preference_swap_pairs(prob_info, assignments, max_pairs=100)
        if not pairs:
            break

        best_candidate = None
        best_candidate_result = None
        best_proxy_candidate = None
        best_proxy_result = None
        for first_id, second_id, _gain in pairs:
            if time.monotonic() >= deadline - 2.0 or checks >= max_checks or proxy_checks >= max_proxy_checks:
                break
            first = assignments[first_id]
            second = assignments[second_id]
            first_target_bay = int(second["bay_id"])
            second_target_bay = int(first["bay_id"])
            first_candidates = _fixed_schedule_grid_relocations_to_bay(
                prob_info,
                assignments,
                first_id,
                first_target_bay,
                excluded_ids={first_id, second_id},
                deadline=deadline,
                max_results=3,
            )
            if not first_candidates:
                continue
            second_candidates = _fixed_schedule_grid_relocations_to_bay(
                prob_info,
                assignments,
                second_id,
                second_target_bay,
                excluded_ids={first_id, second_id},
                deadline=deadline,
                max_results=3,
            )
            if not second_candidates:
                continue

            for first_candidate in first_candidates:
                if time.monotonic() >= deadline - 2.0 or checks >= max_checks or proxy_checks >= max_proxy_checks:
                    break
                for second_candidate in second_candidates:
                    if time.monotonic() >= deadline - 2.0 or checks >= max_checks or proxy_checks >= max_proxy_checks:
                        break
                    candidate_assignments = {bid: dict(data) for bid, data in assignments.items()}
                    candidate_assignments[first_id] = first_candidate
                    candidate_assignments[second_id] = second_candidate
                    proxy_checks += 1
                    proxy_result = _objective_result_from_assignments(prob_info, candidate_assignments)
                    if not _objective_safe_secondary_better(current_result, proxy_result):
                        continue
                    proxy_improving_checks += 1
                    candidate = {"operations": _build_operations(candidate_assignments.values())}
                    if best_proxy_result is None or _secondary_polish_rank(proxy_result) < _secondary_polish_rank(
                        best_proxy_result
                    ):
                        best_proxy_candidate = candidate
                        best_proxy_result = proxy_result
                    if proxy_improving_checks % 5 != 0:
                        continue
                    checks += 1
                    candidate_result = check_feasibility(prob_info, candidate)
                    if candidate_result.get("feasible"):
                        feasible_checks += 1
                    if not _objective_safe_secondary_better(current_result, candidate_result):
                        continue
                    improving_checks += 1
                    if best_candidate_result is None or _secondary_polish_rank(candidate_result) < _secondary_polish_rank(
                        best_candidate_result
                    ):
                        best_candidate = candidate
                        best_candidate_result = candidate_result

        if (
            best_candidate is None
            and best_proxy_candidate is not None
            and checks < max_checks
            and time.monotonic() < deadline - 2.0
        ):
            checks += 1
            candidate_result = check_feasibility(prob_info, best_proxy_candidate)
            if candidate_result.get("feasible"):
                feasible_checks += 1
            if _objective_safe_secondary_better(current_result, candidate_result):
                improving_checks += 1
                best_candidate = best_proxy_candidate
                best_candidate_result = candidate_result

        if best_candidate is None or best_candidate_result is None:
            break
        current = best_candidate
        current_result = best_candidate_result
        improvements += 1

    if trace_enabled:
        _trace_alns_event(
            "fixed_schedule_preference_swap_polish",
            elapsed=time.monotonic() - started_at,
            checks=checks,
            proxy_checks=proxy_checks,
            proxy_improving_checks=proxy_improving_checks,
            feasible_checks=feasible_checks,
            improving_checks=improving_checks,
            improvements=improvements,
            initial_objective=initial_result.get("objective"),
            final_objective=current_result.get("objective"),
            initial_obj1=initial_result.get("obj1"),
            final_obj1=current_result.get("obj1"),
            initial_obj2=initial_result.get("obj2"),
            final_obj2=current_result.get("obj2"),
            initial_obj3=initial_result.get("obj3"),
            final_obj3=current_result.get("obj3"),
        )
    return current, current_result


def _preference_swap_pairs(
    prob_info: dict,
    assignments: dict[int, dict],
    max_pairs: int,
) -> list[tuple[int, int, float]]:
    blocks = prob_info["blocks"]
    block_ids = _preference_polish_block_order(prob_info, assignments)[:48]
    pairs = []
    for idx, first_id in enumerate(block_ids):
        first = assignments[first_id]
        first_bay = int(first["bay_id"])
        first_preferences = blocks[first_id]["bay_preferences"]
        first_regret = max(first_preferences) - first_preferences[first_bay]
        for second_id in block_ids[idx + 1 :]:
            second = assignments[second_id]
            second_bay = int(second["bay_id"])
            if first_bay == second_bay:
                continue
            second_preferences = blocks[second_id]["bay_preferences"]
            second_regret = max(second_preferences) - second_preferences[second_bay]
            swapped_regret = (
                max(first_preferences) - first_preferences[second_bay]
                + max(second_preferences) - second_preferences[first_bay]
            )
            gain = first_regret + second_regret - swapped_regret
            if gain <= 0:
                continue
            pairs.append((first_id, second_id, float(gain)))
    pairs.sort(key=lambda item: (-item[2], item[0], item[1]))
    return pairs[:max_pairs]


def _preference_assignment_candidates_in_bay(
    prob_info: dict,
    assignments: dict[int, dict],
    block_id: int,
    bay_id: int,
    excluded_ids: set[int],
    deadline: float,
    max_positions: int,
    max_results: int,
) -> list[dict]:
    from utils import Bay, Block

    blocks = prob_info["blocks"]
    block_data = blocks[block_id]
    bay = Bay.from_dict(prob_info["bays"][bay_id], bay_id)
    bay_blocks = []
    bay_schedule = []
    for other_id, other in assignments.items():
        if other_id in excluded_ids or int(other["bay_id"]) != bay_id:
            continue
        bay_blocks.append(
            Block(
                other_id,
                blocks[other_id],
                x=int(other["x"]),
                y=int(other["y"]),
                orient_idx=int(other["orient_idx"]),
            )
        )
        bay_schedule.append((int(other["entry_time"]), int(other["exit_time"])))

    candidates = []
    for orient_idx in range(len(block_data.get("shape", []))):
        if time.monotonic() >= deadline - 2.0:
            break
        bbox = orientation_bbox(block_data, orient_idx)
        for x, y in _candidate_positions(bay, bay_blocks, bbox)[:max_positions]:
            if time.monotonic() >= deadline - 2.0:
                break
            block = Block(block_id, block_data, x=int(x), y=int(y), orient_idx=int(orient_idx))
            if not bay.contains_block(block):
                continue
            slots = _safe_slots(
                bay,
                block,
                bay_blocks,
                bay_schedule,
                int(block_data["release_time"]),
                int(block_data["processing_time"]),
                int(block_data["due_date"]),
                require_access=True,
                limit=3,
            )
            for entry_time, exit_time in slots:
                if exit_time > int(block_data["due_date"]):
                    continue
                candidate = {
                    **assignments[block_id],
                    "bay_id": int(bay_id),
                    "x": int(x),
                    "y": int(y),
                    "orient_idx": int(orient_idx),
                    "entry_time": int(entry_time),
                    "exit_time": int(exit_time),
                }
                candidates.append(
                    (
                        int(exit_time),
                        y + bbox[3],
                        int(entry_time),
                        candidate,
                    )
                )
    candidates.sort(key=lambda item: item[:3])
    return [candidate for *_score, candidate in candidates[:max_results]]


def _preference_polish_block_order(prob_info: dict, assignments: dict[int, dict]) -> list[int]:
    blocks = prob_info["blocks"]
    ranked = []
    for block_id, assignment in assignments.items():
        block_data = blocks[block_id]
        exit_time = int(assignment["exit_time"])
        due_time = int(block_data["due_date"])
        if exit_time > due_time:
            continue
        preferences = block_data["bay_preferences"]
        current_bay = int(assignment["bay_id"])
        current_pref = preferences[current_bay]
        best_pref = max(preferences)
        regret = best_pref - current_pref
        if regret <= 0:
            continue
        slack = due_time - exit_time
        release_slack = due_time - int(block_data["release_time"]) - int(block_data["processing_time"])
        ranked.append((-regret, -slack, -release_slack, due_time, block_id))
    return [block_id for *_score, block_id in sorted(ranked)]


def _preference_relocation_assignments(
    prob_info: dict,
    assignments: dict[int, dict],
    block_id: int,
    deadline: float,
    max_bays: int,
    max_positions: int,
) -> list[dict]:
    from utils import Bay, Block

    blocks = prob_info["blocks"]
    block_data = blocks[block_id]
    original = assignments[block_id]
    current_bay = int(original["bay_id"])
    preferences = block_data["bay_preferences"]
    current_pref = preferences[current_bay]
    better_bays = [
        bay_id
        for bay_id in sorted(range(len(prob_info["bays"])), key=lambda idx: (-preferences[idx], idx))
        if preferences[bay_id] > current_pref
    ][:max_bays]
    if not better_bays:
        return []

    bays = [Bay.from_dict(data, bay_id) for bay_id, data in enumerate(prob_info["bays"])]
    release_time = int(block_data["release_time"])
    processing_time = int(block_data["processing_time"])
    due_time = int(block_data["due_date"])
    result: list[dict] = []

    for bay_id in better_bays:
        if time.monotonic() >= deadline - 2.0:
            break
        bay = bays[bay_id]
        target_bay_blocks = []
        target_bay_schedule = []
        for other_id, other in assignments.items():
            if other_id == block_id or int(other["bay_id"]) != bay_id:
                continue
            target_bay_blocks.append(
                Block(
                    other_id,
                    blocks[other_id],
                    x=int(other["x"]),
                    y=int(other["y"]),
                    orient_idx=int(other["orient_idx"]),
                )
            )
            target_bay_schedule.append((int(other["entry_time"]), int(other["exit_time"])))

        for orient_idx in range(len(block_data.get("shape", []))):
            if time.monotonic() >= deadline - 2.0:
                break
            bbox = orientation_bbox(block_data, orient_idx)
            for x, y in _candidate_positions(bay, target_bay_blocks, bbox)[:max_positions]:
                if time.monotonic() >= deadline - 2.0:
                    break
                block = Block(block_id, block_data, x=int(x), y=int(y), orient_idx=int(orient_idx))
                if not bay.contains_block(block):
                    continue
                slots = _safe_slots(
                    bay,
                    block,
                    target_bay_blocks,
                    target_bay_schedule,
                    release_time,
                    processing_time,
                    due_time,
                    require_access=True,
                    limit=3,
                )
                for entry_time, exit_time in slots:
                    if exit_time > due_time:
                        continue
                    result.append(
                        {
                            **original,
                            "bay_id": int(bay_id),
                            "x": int(x),
                            "y": int(y),
                            "orient_idx": int(orient_idx),
                            "entry_time": int(entry_time),
                            "exit_time": int(exit_time),
                        }
                    )
                    if len(result) >= 64:
                        return result
    return result


def _secondary_polish_rank(result: dict) -> tuple[float, float, float]:
    return (
        float(result.get("obj3") or math.inf),
        float(result.get("objective") or math.inf),
        float(result.get("obj2") or math.inf),
    )


def _same_bay_tardy_clusters(
    prob_info: dict,
    assignments: dict[int, dict],
    max_clusters: int,
    cluster_size: int,
) -> list[list[int]]:
    blocks = prob_info["blocks"]
    tardy = sorted(
        (
            bid
            for bid, assignment in assignments.items()
            if int(assignment["exit_time"]) > int(blocks[bid]["due_date"])
        ),
        key=lambda bid: (
            -(int(assignments[bid]["exit_time"]) - int(blocks[bid]["due_date"])),
            int(blocks[bid]["due_date"]),
            bid,
        ),
    )
    clusters: list[list[int]] = []
    for target_id in tardy[:max_clusters]:
        target = assignments[target_id]
        bay_id = int(target["bay_id"])
        target_entry = int(target["entry_time"])
        target_exit = int(target["exit_time"])
        ranked = []
        for other_id, other in assignments.items():
            if other_id == target_id or int(other["bay_id"]) != bay_id:
                continue
            other_entry = int(other["entry_time"])
            other_exit = int(other["exit_time"])
            overlap = min(target_exit, other_exit) - max(target_entry, other_entry)
            if overlap <= 0:
                continue
            ranked.append(
                (
                    -max(0, int(other["exit_time"]) - int(blocks[other_id]["due_date"])),
                    int(blocks[other_id]["due_date"]),
                    -overlap,
                    other_id,
                )
            )
        cluster = [target_id, *[other_id for *_score, other_id in sorted(ranked)[: cluster_size - 1]]]
        if len(cluster) > 1:
            clusters.append(cluster)
    return clusters


def _obj1_polish_rank(result: dict) -> tuple[float, float, float]:
    objective = float(result.get("objective") or math.inf)
    obj1 = float(result.get("obj1") or math.inf)
    if not math.isfinite(objective) or objective <= 0:
        share = math.inf
    else:
        share = obj1 / objective
    return (obj1, share, objective)


def _obj1_polish_destroy_sets(
    prob_info: dict,
    assignments: dict[int, dict],
    deadline: float,
    allow_chain: bool,
    deep_exit_repair: bool,
) -> list[tuple[list[int], bool, bool]]:
    if not assignments:
        return []

    n_blocks = len(prob_info["blocks"])
    max_count = 2 if n_blocks < 150 else (8 if deep_exit_repair else 4)
    candidate_sets: list[tuple[list[int], bool, bool]] = []

    if allow_chain and n_blocks >= 150 and time.monotonic() < deadline - 2.0:
        for removed in _due_window_relocation_blocker_removal_sets(
            prob_info,
            assignments,
            max_count=max_count,
            deadline=deadline,
            max_sets=4 if deep_exit_repair else 2,
        ):
            candidate_sets.append((removed, False, True))
            if len(removed) >= 4:
                candidate_sets.append((removed, True, True))
        for removed in _due_window_blocker_removal_sets(
            prob_info,
            assignments,
            max_count=max_count,
            deadline=deadline,
            max_sets=4 if deep_exit_repair else 2,
        ):
            candidate_sets.append((removed, False, True))
            if len(removed) >= 4:
                candidate_sets.append((removed, True, True))

    if allow_chain and deep_exit_repair and n_blocks >= 150 and time.monotonic() < deadline - 2.0:
        candidate_sets.append(
            (
                _actual_exit_blocker_chain_removal(prob_info, assignments, min(5, max_count), deadline, use_second_hop=True),
                False,
                True,
            )
        )
        if time.monotonic() < deadline - 2.0:
            candidate_sets.append(
                (
                    _actual_exit_blocker_chain_removal(prob_info, assignments, min(5, max_count), deadline, use_second_hop=True),
                    True,
                    True,
                )
            )

    for count in range(1, max_count + 1):
        if time.monotonic() >= deadline - 0.4:
            break
        actual_blocker_sets = 1 if allow_chain else 2
        candidate_sets.append((_worst_tardiness_removal(prob_info, assignments, count), False, False))
        candidate_sets.append((_critical_due_cluster_removal(prob_info, assignments, count), False, False))
        if count >= 2 and time.monotonic() < deadline - 1.0:
            for removed in _actual_exit_blocker_removal_sets(
                prob_info, assignments, count, deadline, max_sets=actual_blocker_sets
            ):
                candidate_sets.append((removed, False, deep_exit_repair))
        if count >= 2:
            candidate_sets.append((_access_blocker_removal(prob_info, assignments, count), False, False))
            if time.monotonic() < deadline - 1.0:
                candidate_sets.append((early_exit_protection_removal(prob_info, assignments, count), False, False))
            candidate_sets.append((_critical_due_cluster_removal(prob_info, assignments, count), True, False))
            if count >= 3:
                if time.monotonic() < deadline - 1.5:
                    for removed in _actual_exit_blocker_removal_sets(
                        prob_info, assignments, count, deadline, max_sets=actual_blocker_sets
                    ):
                        candidate_sets.append((removed, True, deep_exit_repair))
                candidate_sets.append((_access_blocker_removal(prob_info, assignments, count), True, False))
                if time.monotonic() < deadline - 1.5:
                    candidate_sets.append((early_exit_protection_removal(prob_info, assignments, count), True, False))

    if allow_chain and not deep_exit_repair and n_blocks >= 150 and time.monotonic() < deadline - 2.0:
        candidate_sets.append(
            (
                _actual_exit_blocker_chain_removal(
                    prob_info, assignments, 4, deadline, use_second_hop=deep_exit_repair
                ),
                False,
                deep_exit_repair,
            )
        )
        if time.monotonic() < deadline - 2.0:
            candidate_sets.append(
                (
                    _actual_exit_blocker_chain_removal(
                        prob_info, assignments, 4, deadline, use_second_hop=deep_exit_repair
                    ),
                    True,
                    deep_exit_repair,
                )
            )

    unique: list[tuple[list[int], bool, bool]] = []
    seen: set[tuple[tuple[int, ...], bool, bool]] = set()
    for removed, use_regret, preserve_order in candidate_sets:
        cleaned = [block_id for block_id in removed if block_id in assignments]
        key = (tuple(cleaned), use_regret, preserve_order)
        if not cleaned or key in seen:
            continue
        seen.add(key)
        unique.append((cleaned, use_regret, preserve_order))
    return unique


def _due_window_blocker_removal_sets(
    prob_info: dict,
    assignments: dict[int, dict],
    *,
    max_count: int,
    deadline: float,
    max_sets: int,
) -> list[list[int]]:
    """Return clusters that block a tardy block's due-date exit window.

    The older exit-blocker destroy set focuses on the target's current delayed
    exit.  When obj1 is already small, the useful question is sharper: which
    same-bay blocks prevent this target from occupying [due-p, due] and exiting
    exactly at its due date?
    """

    from utils import Bay, Block, check_collisions, check_entry, check_exit

    if max_count <= 1 or not assignments:
        return []

    blocks = prob_info["blocks"]
    target_ids = sorted(
        (
            bid
            for bid, assignment in assignments.items()
            if int(assignment["exit_time"]) > int(blocks[bid]["due_date"])
        ),
        key=lambda bid: (
            -(int(assignments[bid]["exit_time"]) - int(blocks[bid]["due_date"])),
            int(blocks[bid]["due_date"]),
            bid,
        ),
    )[:8]

    results: list[list[int]] = []
    seen_sets: set[tuple[int, ...]] = set()
    for target_id in target_ids:
        if time.monotonic() >= deadline - 1.0 or len(results) >= max_sets:
            break

        target_assignment = assignments[target_id]
        target_data = blocks[target_id]
        target_due = int(target_data["due_date"])
        processing_time = int(target_data["processing_time"])
        desired_entry = target_due - processing_time
        desired_exit = target_due
        if desired_entry < int(target_data["release_time"]):
            continue

        bay_id = int(target_assignment["bay_id"])
        bay = Bay.from_dict(prob_info["bays"][bay_id], bay_id)
        desired_target = Block(
            target_id,
            target_data,
            x=int(target_assignment["x"]),
            y=int(target_assignment["y"]),
            orient_idx=int(target_assignment["orient_idx"]),
        )
        if not bay.contains_block(desired_target):
            continue

        same_bay: list[tuple[int, dict, Block]] = []
        for other_id, other in assignments.items():
            if other_id == target_id or int(other["bay_id"]) != bay_id:
                continue
            same_bay.append(
                (
                    other_id,
                    other,
                    Block(
                        other_id,
                        blocks[other_id],
                        x=int(other["x"]),
                        y=int(other["y"]),
                        orient_idx=int(other["orient_idx"]),
                    ),
                )
            )

        blocker_scores: dict[int, float] = {}
        present_at_entry = [
            other_block
            for _other_id, other, other_block in same_bay
            if int(other["entry_time"]) <= desired_entry < int(other["exit_time"])
        ]
        for obstruction in check_entry(bay, present_at_entry, desired_target, fast=False):
            blocker_id = int(obstruction.existing_block.block_id)
            if blocker_id != target_id and blocker_id in assignments:
                blocker_scores[blocker_id] = blocker_scores.get(blocker_id, 0.0) + 8.0

        present_at_exit = [
            desired_target,
            *[
                other_block
                for _other_id, other, other_block in same_bay
                if int(other["entry_time"]) < desired_exit < int(other["exit_time"])
            ],
        ]
        for obstruction in check_exit(bay, present_at_exit, desired_target, fast=False):
            blocker_id = int(obstruction.existing_block.block_id)
            if blocker_id != target_id and blocker_id in assignments:
                blocker_scores[blocker_id] = blocker_scores.get(blocker_id, 0.0) + 10.0

        for other_id, other, other_block in same_bay:
            other_entry = int(other["entry_time"])
            other_exit = int(other["exit_time"])
            overlap = min(desired_exit, other_exit) - max(desired_entry, other_entry)
            if overlap <= 0:
                continue
            if check_collisions(bay, [desired_target, other_block]):
                blocker_scores[other_id] = blocker_scores.get(other_id, 0.0) + 12.0 + overlap
            elif other_entry <= desired_entry < other_exit or other_entry < desired_exit < other_exit:
                blocker_scores[other_id] = blocker_scores.get(other_id, 0.0) + 1.0

        if not blocker_scores:
            continue

        ranked_blockers = sorted(
            blocker_scores,
            key=lambda bid: (
                -blocker_scores[bid],
                int(blocks[bid]["due_date"]) < target_due,
                -max(0, int(assignments[bid]["exit_time"]) - int(blocks[bid]["due_date"])),
                abs(int(blocks[bid]["due_date"]) - target_due),
                bid,
            ),
        )
        for count in range(2, min(max_count, len(ranked_blockers) + 1) + 1):
            cluster = [target_id, *ranked_blockers[: count - 1]]
            key = tuple(cluster)
            if key in seen_sets:
                continue
            seen_sets.add(key)
            results.append(cluster)
            if len(results) >= max_sets:
                break

    return results


def _due_window_relocation_blocker_removal_sets(
    prob_info: dict,
    assignments: dict[int, dict],
    *,
    max_count: int,
    deadline: float,
    max_sets: int,
) -> list[list[int]]:
    """Find blocker clusters for placing tardy blocks in any due-window bay.

    This is a low-obj1 tail operator.  It does not assume the tardy block's
    current bay/position is the right one; instead it searches compact candidate
    placements across bays at [due-p, due] and returns the small blocker sets
    that would need to be reinserted.
    """

    from utils import Bay, Block, check_entry, check_exit

    if max_count <= 1 or not assignments:
        return []

    blocks = prob_info["blocks"]
    bays = [Bay.from_dict(data, bay_id) for bay_id, data in enumerate(prob_info["bays"])]
    bay_blocks: list[list[Block]] = [[] for _ in bays]
    for block_id, assignment in assignments.items():
        bay_id = int(assignment["bay_id"])
        if 0 <= bay_id < len(bays):
            bay_blocks[bay_id].append(
                Block(
                    block_id,
                    blocks[block_id],
                    x=int(assignment["x"]),
                    y=int(assignment["y"]),
                    orient_idx=int(assignment["orient_idx"]),
                )
            )

    target_ids = sorted(
        (
            bid
            for bid, assignment in assignments.items()
            if int(assignment["exit_time"]) > int(blocks[bid]["due_date"])
        ),
        key=lambda bid: (
            -(int(assignments[bid]["exit_time"]) - int(blocks[bid]["due_date"])),
            int(blocks[bid]["due_date"]),
            bid,
        ),
    )[:4]

    candidates: list[tuple[tuple[float, int, int], list[int]]] = []
    seen_sets: set[tuple[int, ...]] = set()
    for target_id in target_ids:
        if time.monotonic() >= deadline - 1.0:
            break

        target_data = blocks[target_id]
        target_due = int(target_data["due_date"])
        processing_time = int(target_data["processing_time"])
        desired_entry = target_due - processing_time
        desired_exit = target_due
        if desired_entry < int(target_data["release_time"]):
            continue

        preferences = target_data["bay_preferences"]
        best_preference = max(preferences)
        bay_order = sorted(range(len(bays)), key=lambda bay_id: (best_preference - preferences[bay_id], bay_id))
        for bay_id in bay_order:
            if time.monotonic() >= deadline - 1.0:
                break

            bay = bays[bay_id]
            same_bay_rows = [
                (other_id, other)
                for other_id, other in assignments.items()
                if other_id != target_id and int(other["bay_id"]) == bay_id
            ]
            same_bay_blocks = {
                other_id: Block(
                    other_id,
                    blocks[other_id],
                    x=int(other["x"]),
                    y=int(other["y"]),
                    orient_idx=int(other["orient_idx"]),
                )
                for other_id, other in same_bay_rows
            }

            for orient_idx in range(len(target_data.get("shape", []))):
                if time.monotonic() >= deadline - 1.0:
                    break
                bbox = orientation_bbox(target_data, orient_idx)
                for x, y in _candidate_positions(bay, bay_blocks[bay_id], bbox)[:32]:
                    if time.monotonic() >= deadline - 1.0:
                        break
                    target_block = Block(target_id, target_data, x=int(x), y=int(y), orient_idx=int(orient_idx))
                    if not bay.contains_block(target_block):
                        continue

                    blocker_scores: dict[int, float] = {}
                    present_at_entry = []
                    present_at_exit = [target_block]
                    for other_id, other in same_bay_rows:
                        other_entry = int(other["entry_time"])
                        other_exit = int(other["exit_time"])
                        other_block = same_bay_blocks[other_id]
                        if other_entry <= desired_entry < other_exit:
                            present_at_entry.append(other_block)
                        if other_entry < desired_exit < other_exit:
                            present_at_exit.append(other_block)
                        overlap = min(desired_exit, other_exit) - max(desired_entry, other_entry)
                        if overlap > 0 and _cached_pair_collision(bay, target_block, other_block):
                            blocker_scores[other_id] = blocker_scores.get(other_id, 0.0) + 12.0 + overlap

                    for obstruction in check_entry(bay, present_at_entry, target_block, fast=False):
                        blocker_id = int(obstruction.existing_block.block_id)
                        if blocker_id != target_id and blocker_id in assignments:
                            blocker_scores[blocker_id] = blocker_scores.get(blocker_id, 0.0) + 8.0
                    for obstruction in check_exit(bay, present_at_exit, target_block, fast=False):
                        blocker_id = int(obstruction.existing_block.block_id)
                        if blocker_id != target_id and blocker_id in assignments:
                            blocker_scores[blocker_id] = blocker_scores.get(blocker_id, 0.0) + 10.0

                    if not blocker_scores:
                        cluster = [target_id]
                    elif len(blocker_scores) <= max_count - 1:
                        ranked_blockers = sorted(
                            blocker_scores,
                            key=lambda bid: (
                                -blocker_scores[bid],
                                int(blocks[bid]["due_date"]) < target_due,
                                -max(0, int(assignments[bid]["exit_time"]) - int(blocks[bid]["due_date"])),
                                abs(int(blocks[bid]["due_date"]) - target_due),
                                bid,
                            ),
                        )
                        cluster = [target_id, *ranked_blockers]
                    else:
                        continue

                    key = tuple(cluster)
                    if key in seen_sets:
                        continue
                    seen_sets.add(key)
                    preference_regret = best_preference - preferences[bay_id]
                    total_blocker_score = sum(blocker_scores.values())
                    rank = (
                        len(cluster) * 100.0 - min(50.0, total_blocker_score),
                        preference_regret,
                        bay_id,
                    )
                    candidates.append((rank, cluster))

    candidates.sort(key=lambda item: item[0])
    return [cluster for _rank, cluster in candidates[:max_sets]]


def _final_due_window_relocation_polish(
    prob_info: dict,
    solution: dict,
    initial_result: dict,
    deadline: float,
    *,
    trace_enabled: bool = False,
) -> tuple[dict, dict]:
    from utils import check_feasibility

    if not initial_result.get("feasible"):
        return solution, initial_result
    initial_obj1 = float(initial_result.get("obj1") or 0.0)
    if initial_obj1 <= 1e-6 or initial_obj1 > 25.0:
        return solution, initial_result

    started_at = time.monotonic()
    current = solution
    current_result = initial_result
    attempts = 0
    improvements = 0

    while time.monotonic() < deadline - 2.0:
        assignments = _assignments_from_solution(current)
        chain_candidate, chain_result = _due_window_chain_repair(
            prob_info,
            assignments,
            current_result,
            deadline,
            trace_enabled=trace_enabled,
        )
        if chain_candidate is not None and chain_result is not None and _objective_safe_obj1_better(
            current_result, chain_result
        ):
            current = chain_candidate
            current_result = chain_result
            improvements += 1
            if float(current_result.get("obj1") or 0.0) <= 1e-6:
                break
            continue

        options = _due_window_relocation_options(
            prob_info,
            assignments,
            max_blockers=10,
            deadline=deadline,
            max_options=16,
        )
        if not options:
            break

        best_candidate = None
        best_candidate_result = None
        for _rank, target_assignment, blockers in options:
            if time.monotonic() >= deadline - 2.0:
                break
            target_id = int(target_assignment["block_id"])
            removed = [bid for bid in blockers if bid in assignments and bid != target_id]
            fixed = {
                bid: dict(data)
                for bid, data in assignments.items()
                if bid != target_id and bid not in removed
            }
            fixed[target_id] = dict(target_assignment)
            attempts += 1

            candidate = _build_by_regret(
                prob_info,
                sorted(
                    removed,
                    key=lambda bid: (
                        int(prob_info["blocks"][bid]["due_date"]),
                        -max(0, int(assignments[bid]["exit_time"]) - int(prob_info["blocks"][bid]["due_date"])),
                        bid,
                    ),
                ),
                fixed,
                min(deadline, time.monotonic() + 20.0),
                allow_timeout_fallback=False,
                use_exit_blocking_penalty=True,
                use_multi_slot=True,
                use_preference_bias=False,
            )
            if candidate is None:
                continue
            candidate_result = check_feasibility(prob_info, candidate)
            if not _objective_safe_obj1_better(current_result, candidate_result):
                continue
            if best_candidate_result is None or _obj1_polish_rank(candidate_result) < _obj1_polish_rank(
                best_candidate_result
            ):
                best_candidate = candidate
                best_candidate_result = candidate_result

        if best_candidate is None or best_candidate_result is None:
            break
        current = best_candidate
        current_result = best_candidate_result
        improvements += 1
        if float(current_result.get("obj1") or 0.0) <= 1e-6:
            break

    if trace_enabled:
        _trace_alns_event(
            "final_due_window_relocation_polish",
            elapsed=time.monotonic() - started_at,
            attempts=attempts,
            improvements=improvements,
            initial_objective=initial_result.get("objective"),
            final_objective=current_result.get("objective"),
            initial_obj1=initial_result.get("obj1"),
            final_obj1=current_result.get("obj1"),
            initial_obj2=initial_result.get("obj2"),
            final_obj2=current_result.get("obj2"),
            initial_obj3=initial_result.get("obj3"),
            final_obj3=current_result.get("obj3"),
        )
    return current, current_result


def _due_window_chain_repair(
    prob_info: dict,
    assignments: dict[int, dict],
    current_result: dict,
    deadline: float,
    *,
    trace_enabled: bool = False,
) -> tuple[dict | None, dict | None]:
    from utils import check_feasibility

    blocks = prob_info["blocks"]
    tardy_ids = sorted(
        (
            bid
            for bid, assignment in assignments.items()
            if int(assignment["exit_time"]) > int(blocks[bid]["due_date"])
        ),
        key=lambda bid: (
            -(int(assignments[bid]["exit_time"]) - int(blocks[bid]["due_date"])),
            int(blocks[bid]["due_date"]),
            bid,
        ),
    )[:3]
    if not tardy_ids:
        return None, None

    started_at = time.monotonic()
    nodes = 0
    official_checks = 0
    best_candidate = None
    best_result = None

    def evaluate_candidate(candidate: dict | None) -> None:
        nonlocal official_checks, best_candidate, best_result
        if candidate is None:
            return
        official_checks += 1
        candidate_result = check_feasibility(prob_info, candidate)
        if not _objective_safe_obj1_better(current_result, candidate_result):
            return
        if best_result is None or _obj1_polish_rank(candidate_result) < _obj1_polish_rank(best_result):
            best_candidate = candidate
            best_result = candidate_result

    def finish_with_regret(fixed: dict[int, dict], pending: list[int]) -> None:
        if time.monotonic() >= deadline - 2.0:
            return
        if len(pending) > 16:
            return
        cleaned_pending = [bid for bid in pending if bid in assignments and bid not in fixed]
        if not cleaned_pending:
            evaluate_candidate({"operations": _build_operations(fixed.values())})
            return
        candidate = _build_by_regret(
            prob_info,
            sorted(
                cleaned_pending,
                key=lambda bid: (
                    int(blocks[bid]["due_date"]),
                    -max(0, int(assignments[bid]["exit_time"]) - int(blocks[bid]["due_date"])),
                    bid,
                ),
            ),
            fixed,
            min(deadline, time.monotonic() + 12.0),
            allow_timeout_fallback=False,
            use_exit_blocking_penalty=True,
            use_multi_slot=True,
            use_preference_bias=False,
        )
        evaluate_candidate(candidate)

    def dfs(
        fixed: dict[int, dict],
        pending: list[int],
        locked: set[int],
        depth: int,
    ) -> None:
        nonlocal nodes, official_checks, best_candidate, best_result

        if time.monotonic() >= deadline - 2.0:
            return
        if nodes >= 260 or len(pending) > 14:
            return
        if depth > 7:
            finish_with_regret(fixed, pending)
            return
        nodes += 1

        if not pending:
            evaluate_candidate({"operations": _build_operations(fixed.values())})
            return

        block_id = min(
            pending,
            key=lambda bid: (
                int(blocks[bid]["due_date"]),
                -max(0, int(assignments[bid]["exit_time"]) - int(blocks[bid]["due_date"])),
                bid,
            ),
        )
        remaining_pending = [bid for bid in pending if bid != block_id]
        base_fixed = {bid: dict(data) for bid, data in fixed.items() if bid != block_id}
        options = _due_window_options_for_block(
            prob_info,
            base_fixed,
            block_id,
            assignments[block_id],
            deadline,
            max_blockers=max(3, 10 - depth),
            max_options=120 if depth == 0 else 90,
            allow_early_entries=depth > 0,
        )
        usable_options = []
        for option in options:
            _rank, _target_assignment, blockers = option
            blocker_set = {bid for bid in blockers if bid in base_fixed and bid != block_id}
            if blocker_set & locked:
                continue
            usable_options.append(option)
            if len(usable_options) >= (14 if depth == 0 else 10):
                break
        if not usable_options and depth >= 2:
            finish_with_regret(fixed, pending)
            return
        for _rank, target_assignment, blockers in usable_options:
            if time.monotonic() >= deadline - 2.0:
                break
            blocker_set = {bid for bid in blockers if bid in base_fixed and bid != block_id}
            next_pending = list(remaining_pending)
            for blocker_id in sorted(
                blocker_set,
                key=lambda bid: (
                    int(blocks[bid]["due_date"]),
                    -max(0, int(assignments[bid]["exit_time"]) - int(blocks[bid]["due_date"])),
                    bid,
                ),
            ):
                if blocker_id not in next_pending:
                    next_pending.append(blocker_id)
            if len(next_pending) > 12:
                continue
            next_fixed = {bid: data for bid, data in base_fixed.items() if bid not in blocker_set}
            next_fixed[block_id] = dict(target_assignment)
            dfs(next_fixed, next_pending, locked | {block_id}, depth + 1)
            if best_result is not None and float(best_result.get("obj1") or math.inf) <= 1e-6:
                return

    for target_id in tardy_ids:
        if time.monotonic() >= deadline - 2.0:
            break
        dfs({bid: dict(data) for bid, data in assignments.items()}, [target_id], set(), 0)
        if best_result is not None and float(best_result.get("obj1") or math.inf) <= 1e-6:
            break

    if trace_enabled:
        _trace_alns_event(
            "due_window_chain_repair",
            elapsed=time.monotonic() - started_at,
            nodes=nodes,
            official_checks=official_checks,
            initial_objective=current_result.get("objective"),
            best_objective=best_result.get("objective") if best_result else None,
            initial_obj1=current_result.get("obj1"),
            best_obj1=best_result.get("obj1") if best_result else None,
        )
    return best_candidate, best_result


def _due_window_options_for_block(
    prob_info: dict,
    fixed_assignments: dict[int, dict],
    block_id: int,
    original_assignment: dict,
    deadline: float,
    *,
    max_blockers: int,
    max_options: int,
    allow_early_entries: bool = False,
) -> list[tuple[tuple[float, int, int], dict, list[int]]]:
    from utils import Bay, Block, check_entry, check_exit

    blocks = prob_info["blocks"]
    block_data = blocks[block_id]
    due_time = int(block_data["due_date"])
    processing_time = int(block_data["processing_time"])
    latest_entry = due_time - processing_time
    release_time = int(block_data["release_time"])
    if latest_entry < release_time:
        return []
    if allow_early_entries:
        earliest_entry = max(release_time, latest_entry - 8)
        entry_candidates = list(range(latest_entry, earliest_entry - 1, -1))
        if release_time < earliest_entry:
            entry_candidates.append(release_time)
    else:
        entry_candidates = [latest_entry]

    bays = [Bay.from_dict(data, bay_id) for bay_id, data in enumerate(prob_info["bays"])]
    bay_blocks: list[list[Block]] = [[] for _ in bays]
    for other_id, assignment in fixed_assignments.items():
        bay_id = int(assignment["bay_id"])
        if 0 <= bay_id < len(bays):
            bay_blocks[bay_id].append(
                Block(
                    other_id,
                    blocks[other_id],
                    x=int(assignment["x"]),
                    y=int(assignment["y"]),
                    orient_idx=int(assignment["orient_idx"]),
                )
            )

    preferences = block_data["bay_preferences"]
    best_preference = max(preferences)
    current_bay = int(original_assignment["bay_id"])
    bay_order = sorted(
        range(len(bays)),
        key=lambda bay_id: (
            best_preference - preferences[bay_id],
            bay_id != current_bay,
            bay_id,
        ),
    )

    options: list[tuple[tuple[float, int, int], dict, list[int]]] = []
    seen: set[tuple[int, int, int, int, tuple[int, ...]]] = set()
    for bay_id in bay_order:
        if time.monotonic() >= deadline - 1.0:
            break
        bay = bays[bay_id]
        same_bay_rows = [
            (other_id, other)
            for other_id, other in fixed_assignments.items()
            if int(other["bay_id"]) == bay_id
        ]
        same_bay_blocks = {
            other_id: Block(
                other_id,
                blocks[other_id],
                x=int(other["x"]),
                y=int(other["y"]),
                orient_idx=int(other["orient_idx"]),
            )
            for other_id, other in same_bay_rows
        }
        for orient_idx in range(len(block_data.get("shape", []))):
            if time.monotonic() >= deadline - 1.0:
                break
            bbox = orientation_bbox(block_data, orient_idx)
            for x, y in _candidate_positions(bay, bay_blocks[bay_id], bbox)[:48]:
                if time.monotonic() >= deadline - 1.0:
                    break
                target_block = Block(block_id, block_data, x=int(x), y=int(y), orient_idx=int(orient_idx))
                if not bay.contains_block(target_block):
                    continue

                for desired_entry in entry_candidates:
                    desired_exit = desired_entry + processing_time
                    blocker_scores: dict[int, float] = {}
                    present_at_entry = []
                    present_at_exit = [target_block]
                    for other_id, other in same_bay_rows:
                        other_entry = int(other["entry_time"])
                        other_exit = int(other["exit_time"])
                        other_block = same_bay_blocks[other_id]
                        if other_entry <= desired_entry < other_exit:
                            present_at_entry.append(other_block)
                        if other_entry < desired_exit < other_exit:
                            present_at_exit.append(other_block)
                        overlap = min(desired_exit, other_exit) - max(desired_entry, other_entry)
                        if overlap > 0 and _cached_pair_collision(bay, target_block, other_block):
                            blocker_scores[other_id] = blocker_scores.get(other_id, 0.0) + 12.0 + overlap

                    for obstruction in check_entry(bay, present_at_entry, target_block, fast=False):
                        blocker_id = int(obstruction.existing_block.block_id)
                        if blocker_id != block_id and blocker_id in fixed_assignments:
                            blocker_scores[blocker_id] = blocker_scores.get(blocker_id, 0.0) + 8.0
                    for obstruction in check_exit(bay, present_at_exit, target_block, fast=False):
                        blocker_id = int(obstruction.existing_block.block_id)
                        if blocker_id != block_id and blocker_id in fixed_assignments:
                            blocker_scores[blocker_id] = blocker_scores.get(blocker_id, 0.0) + 10.0

                    if len(blocker_scores) > max_blockers:
                        continue
                    ranked_blockers = sorted(
                        blocker_scores,
                        key=lambda bid: (
                            -blocker_scores[bid],
                            int(blocks[bid]["due_date"]) < due_time,
                            -max(0, int(fixed_assignments[bid]["exit_time"]) - int(blocks[bid]["due_date"])),
                            abs(int(blocks[bid]["due_date"]) - due_time),
                            bid,
                        ),
                    )
                    key = (bay_id, int(x), int(y), int(orient_idx), int(desired_entry), tuple(ranked_blockers))
                    if key in seen:
                        continue
                    seen.add(key)
                    target_assignment = {
                        "block_id": block_id,
                        "bay_id": int(bay_id),
                        "x": int(x),
                        "y": int(y),
                        "orient_idx": int(orient_idx),
                        "entry_time": int(desired_entry),
                        "exit_time": int(desired_exit),
                    }
                    rank = (
                        len(ranked_blockers) * 100.0 - min(50.0, sum(blocker_scores.values())),
                        best_preference - preferences[bay_id],
                        latest_entry - desired_entry,
                        bay_id,
                    )
                    options.append((rank, target_assignment, ranked_blockers))

    options.sort(key=lambda item: item[0])
    return options[:max_options]


def _due_window_relocation_options(
    prob_info: dict,
    assignments: dict[int, dict],
    *,
    max_blockers: int,
    deadline: float,
    max_options: int,
) -> list[tuple[tuple[float, int, int], dict, list[int]]]:
    from utils import Bay, Block, check_entry, check_exit

    if max_blockers < 0 or not assignments:
        return []

    blocks = prob_info["blocks"]
    bays = [Bay.from_dict(data, bay_id) for bay_id, data in enumerate(prob_info["bays"])]
    bay_blocks: list[list[Block]] = [[] for _ in bays]
    for block_id, assignment in assignments.items():
        bay_id = int(assignment["bay_id"])
        if 0 <= bay_id < len(bays):
            bay_blocks[bay_id].append(
                Block(
                    block_id,
                    blocks[block_id],
                    x=int(assignment["x"]),
                    y=int(assignment["y"]),
                    orient_idx=int(assignment["orient_idx"]),
                )
            )

    target_ids = sorted(
        (
            bid
            for bid, assignment in assignments.items()
            if int(assignment["exit_time"]) > int(blocks[bid]["due_date"])
        ),
        key=lambda bid: (
            -(int(assignments[bid]["exit_time"]) - int(blocks[bid]["due_date"])),
            int(blocks[bid]["due_date"]),
            bid,
        ),
    )[:4]

    options: list[tuple[tuple[float, int, int], dict, list[int]]] = []
    seen: set[tuple[int, int, int, int, int, tuple[int, ...]]] = set()
    for target_id in target_ids:
        if time.monotonic() >= deadline - 1.0:
            break
        target_data = blocks[target_id]
        target_due = int(target_data["due_date"])
        processing_time = int(target_data["processing_time"])
        desired_entry = target_due - processing_time
        desired_exit = target_due
        if desired_entry < int(target_data["release_time"]):
            continue

        preferences = target_data["bay_preferences"]
        best_preference = max(preferences)
        bay_order = sorted(range(len(bays)), key=lambda bay_id: (best_preference - preferences[bay_id], bay_id))
        for bay_id in bay_order:
            if time.monotonic() >= deadline - 1.0:
                break
            bay = bays[bay_id]
            same_bay_rows = [
                (other_id, other)
                for other_id, other in assignments.items()
                if other_id != target_id and int(other["bay_id"]) == bay_id
            ]
            same_bay_blocks = {
                other_id: Block(
                    other_id,
                    blocks[other_id],
                    x=int(other["x"]),
                    y=int(other["y"]),
                    orient_idx=int(other["orient_idx"]),
                )
                for other_id, other in same_bay_rows
            }
            for orient_idx in range(len(target_data.get("shape", []))):
                if time.monotonic() >= deadline - 1.0:
                    break
                bbox = orientation_bbox(target_data, orient_idx)
                for x, y in _candidate_positions(bay, bay_blocks[bay_id], bbox)[:48]:
                    if time.monotonic() >= deadline - 1.0:
                        break
                    target_block = Block(target_id, target_data, x=int(x), y=int(y), orient_idx=int(orient_idx))
                    if not bay.contains_block(target_block):
                        continue

                    blocker_scores: dict[int, float] = {}
                    present_at_entry = []
                    present_at_exit = [target_block]
                    for other_id, other in same_bay_rows:
                        other_entry = int(other["entry_time"])
                        other_exit = int(other["exit_time"])
                        other_block = same_bay_blocks[other_id]
                        if other_entry <= desired_entry < other_exit:
                            present_at_entry.append(other_block)
                        if other_entry < desired_exit < other_exit:
                            present_at_exit.append(other_block)
                        overlap = min(desired_exit, other_exit) - max(desired_entry, other_entry)
                        if overlap > 0 and _cached_pair_collision(bay, target_block, other_block):
                            blocker_scores[other_id] = blocker_scores.get(other_id, 0.0) + 12.0 + overlap

                    for obstruction in check_entry(bay, present_at_entry, target_block, fast=False):
                        blocker_id = int(obstruction.existing_block.block_id)
                        if blocker_id != target_id and blocker_id in assignments:
                            blocker_scores[blocker_id] = blocker_scores.get(blocker_id, 0.0) + 8.0
                    for obstruction in check_exit(bay, present_at_exit, target_block, fast=False):
                        blocker_id = int(obstruction.existing_block.block_id)
                        if blocker_id != target_id and blocker_id in assignments:
                            blocker_scores[blocker_id] = blocker_scores.get(blocker_id, 0.0) + 10.0

                    if len(blocker_scores) > max_blockers:
                        continue
                    ranked_blockers = sorted(
                        blocker_scores,
                        key=lambda bid: (
                            -blocker_scores[bid],
                            int(blocks[bid]["due_date"]) < target_due,
                            -max(0, int(assignments[bid]["exit_time"]) - int(blocks[bid]["due_date"])),
                            abs(int(blocks[bid]["due_date"]) - target_due),
                            bid,
                        ),
                    )
                    target_assignment = {
                        "block_id": target_id,
                        "bay_id": bay_id,
                        "x": int(x),
                        "y": int(y),
                        "orient_idx": int(orient_idx),
                        "entry_time": int(desired_entry),
                        "exit_time": int(desired_exit),
                    }
                    key = (target_id, bay_id, int(x), int(y), int(orient_idx), tuple(ranked_blockers))
                    if key in seen:
                        continue
                    seen.add(key)
                    preference_regret = best_preference - preferences[bay_id]
                    total_blocker_score = sum(blocker_scores.values())
                    rank = (
                        len(ranked_blockers) * 100.0 - min(50.0, total_blocker_score),
                        preference_regret,
                        bay_id,
                    )
                    options.append((rank, target_assignment, ranked_blockers))

    options.sort(key=lambda item: item[0])
    return options[:max_options]


def _actual_exit_blocker_chain_removal(
    prob_info: dict,
    assignments: dict[int, dict],
    count: int,
    deadline: float,
    use_second_hop: bool = True,
) -> list[int]:
    selected = _actual_exit_blocker_removal(prob_info, assignments, count, deadline)
    if len(selected) >= count or len(selected) < 2:
        return selected

    if use_second_hop:
        _append_second_hop_exit_blockers(prob_info, assignments, selected, count, deadline)
        if len(selected) >= count:
            return selected

    blocks = prob_info["blocks"]
    target_id = selected[0]
    target = assignments[target_id]
    bay_id = int(target["bay_id"])
    target_due = int(blocks[target_id]["due_date"])
    target_window = (int(target["entry_time"]), int(target["exit_time"]))
    seen = set(selected)

    ranked = []
    for other_id, other in assignments.items():
        if other_id in seen or int(other["bay_id"]) != bay_id:
            continue
        other_entry = int(other["entry_time"])
        other_exit = int(other["exit_time"])
        overlap = min(target_window[1], other_exit) - max(target_window[0], other_entry)
        if overlap <= 0:
            continue
        other_due = int(blocks[other_id]["due_date"])
        other_tardy = max(0, other_exit - other_due)
        ranked.append(
            (
                other_due < target_due,
                -other_tardy,
                -overlap,
                abs(other_due - target_due),
                other_exit,
                other_id,
            )
        )

    for *_, other_id in sorted(ranked):
        if time.monotonic() >= deadline - 1.0:
            break
        selected.append(other_id)
        if len(selected) >= count:
            break
    return selected


def _append_second_hop_exit_blockers(
    prob_info: dict,
    assignments: dict[int, dict],
    selected: list[int],
    count: int,
    deadline: float,
) -> None:
    from utils import Bay, Block, check_exit

    blocks = prob_info["blocks"]
    target_due = int(blocks[selected[0]]["due_date"])
    seen = set(selected)
    ranked: list[tuple[bool, int, int, int]] = []

    for direct_blocker_id in list(selected[1:]):
        if time.monotonic() >= deadline - 1.0:
            break
        blocker_assignment = assignments.get(direct_blocker_id)
        if blocker_assignment is None:
            continue

        blocker_data = blocks[direct_blocker_id]
        blocker_entry = int(blocker_assignment["entry_time"])
        blocker_exit = int(blocker_assignment["exit_time"])
        desired_exit = max(blocker_entry + int(blocker_data["processing_time"]), int(blocker_data["due_date"]))
        if not (blocker_entry < desired_exit < blocker_exit):
            continue

        bay_id = int(blocker_assignment["bay_id"])
        bay = Bay.from_dict(prob_info["bays"][bay_id], bay_id)
        blocker_block = Block(
            direct_blocker_id,
            blocker_data,
            x=int(blocker_assignment["x"]),
            y=int(blocker_assignment["y"]),
            orient_idx=int(blocker_assignment["orient_idx"]),
        )
        present = [blocker_block]
        for other_id, other in assignments.items():
            if other_id == direct_blocker_id or int(other["bay_id"]) != bay_id:
                continue
            if int(other["entry_time"]) < desired_exit < int(other["exit_time"]):
                present.append(
                    Block(
                        other_id,
                        blocks[other_id],
                        x=int(other["x"]),
                        y=int(other["y"]),
                        orient_idx=int(other["orient_idx"]),
                    )
                )

        for obstruction in check_exit(bay, present, blocker_block, fast=False):
            other_id = int(obstruction.existing_block.block_id)
            if other_id in seen or other_id not in assignments:
                continue
            other_due = int(blocks[other_id]["due_date"])
            if other_due < target_due:
                continue
            ranked.append((other_due < int(blocker_data["due_date"]), other_due, desired_exit, other_id))

    for _, _, _, other_id in sorted(ranked):
        if other_id in seen:
            continue
        selected.append(other_id)
        seen.add(other_id)
        if len(selected) >= count:
            break


def _actual_exit_blocker_removal(
    prob_info: dict,
    assignments: dict[int, dict],
    count: int,
    deadline: float,
) -> list[int]:
    sets = _actual_exit_blocker_removal_sets(prob_info, assignments, count, deadline, max_sets=1)
    return sets[0] if sets else []


def _actual_exit_blocker_removal_sets(
    prob_info: dict,
    assignments: dict[int, dict],
    count: int,
    deadline: float,
    max_sets: int,
) -> list[list[int]]:
    from utils import Bay, Block, check_exit

    if count <= 1:
        return []

    blocks = prob_info["blocks"]
    results: list[list[int]] = []
    seen_sets: set[tuple[int, ...]] = set()
    target_ids = sorted(
        assignments,
        key=lambda bid: (
            -max(0, int(assignments[bid]["exit_time"]) - int(blocks[bid]["due_date"])),
            int(blocks[bid]["due_date"]),
            bid,
        ),
    )[:12]

    for target_id in target_ids:
        if time.monotonic() >= deadline - 0.8:
            break
        if len(results) >= max_sets:
            break

        target = assignments[target_id]
        target_data = blocks[target_id]
        target_due = int(target_data["due_date"])
        target_exit = int(target["exit_time"])
        target_entry = int(target["entry_time"])
        target_tardiness = max(0, target_exit - target_due)
        if target_tardiness <= 0:
            continue

        desired_exit = max(target_entry + int(target_data["processing_time"]), target_due)
        if not (target_entry < desired_exit < target_exit):
            continue

        bay_id = int(target["bay_id"])
        bay = Bay.from_dict(prob_info["bays"][bay_id], bay_id)
        target_block = Block(
            target_id,
            target_data,
            x=int(target["x"]),
            y=int(target["y"]),
            orient_idx=int(target["orient_idx"]),
        )

        present = [target_block]
        for other_id, other in assignments.items():
            if other_id == target_id or int(other["bay_id"]) != bay_id:
                continue
            if int(other["entry_time"]) < desired_exit < int(other["exit_time"]):
                present.append(
                    Block(
                        other_id,
                        blocks[other_id],
                        x=int(other["x"]),
                        y=int(other["y"]),
                        orient_idx=int(other["orient_idx"]),
                    )
                )

        blockers = []
        seen = {target_id}
        for obstruction in check_exit(bay, present, target_block, fast=False):
            blocker_id = int(obstruction.existing_block.block_id)
            if blocker_id in seen:
                continue
            seen.add(blocker_id)
            blockers.append(blocker_id)

        if blockers:
            ranked_blockers = sorted(
                blockers,
                key=lambda bid: (
                    int(blocks[bid]["due_date"]) < target_due,
                    -max(0, int(assignments[bid]["exit_time"]) - int(blocks[bid]["due_date"])),
                    int(blocks[bid]["due_date"]),
                    bid,
                ),
            )
            selected = [target_id, *ranked_blockers[: count - 1]]
            key = tuple(selected)
            if key not in seen_sets:
                seen_sets.add(key)
                results.append(selected)

    return results


def _critical_due_cluster_removal(
    prob_info: dict,
    assignments: dict[int, dict],
    count: int,
) -> list[int]:
    blocks = prob_info["blocks"]
    tardy = _worst_tardiness_removal(prob_info, assignments, 1)
    if count <= 0 or not tardy:
        return []

    target_id = tardy[0]
    target = assignments[target_id]
    target_data = blocks[target_id]
    target_due = int(target_data["due_date"])
    target_entry = int(target["entry_time"])
    target_exit = int(target["exit_time"])
    target_release = int(target_data["release_time"])
    selected = [target_id]

    ranked = []
    for other_id, other in assignments.items():
        if other_id == target_id or int(other["bay_id"]) != int(target["bay_id"]):
            continue

        other_entry = int(other["entry_time"])
        other_exit = int(other["exit_time"])
        current_overlap = min(target_exit, other_exit) - max(target_entry, other_entry)
        due_window_overlap = min(target_due, other_exit) - max(target_release, other_entry)
        spans_due = other_entry < target_due < other_exit
        spans_release = other_entry < target_release < other_exit
        if current_overlap <= 0 and due_window_overlap <= 0 and not spans_due and not spans_release:
            continue

        other_due = int(blocks[other_id]["due_date"])
        ranked.append(
            (
                other_due < target_due,
                not spans_due,
                not spans_release,
                -max(current_overlap, due_window_overlap, 0),
                abs(other_due - target_due),
                other_exit,
                other_id,
            )
        )

    for *_, other_id in sorted(ranked):
        selected.append(other_id)
        if len(selected) >= count:
            break
    return selected


def _rebuild_fixed_placements(
    prob_info: dict,
    assignments: dict[int, dict],
    order: list[int],
    deadline: float,
) -> dict | None:
    from utils import Bay, Block

    bays = [Bay.from_dict(data, bay_id) for bay_id, data in enumerate(prob_info["bays"])]
    blocks = prob_info["blocks"]
    rebuilt: dict[int, dict] = {}
    bay_blocks = [[] for _ in bays]
    bay_schedules = [[] for _ in bays]
    bay_loads = [0.0 for _ in bays]

    for block_id in order:
        if time.monotonic() >= deadline - 0.3:
            return None

        original = assignments[block_id]
        block_data = blocks[block_id]
        bay_id = int(original["bay_id"])
        block = Block(
            block_id,
            block_data,
            x=int(original["x"]),
            y=int(original["y"]),
            orient_idx=int(original["orient_idx"]),
        )
        if not bays[bay_id].contains_block(block):
            return None

        slot = _earliest_safe_slot(
            bays[bay_id],
            block,
            bay_blocks[bay_id],
            bay_schedules[bay_id],
            int(block_data["release_time"]),
            int(block_data["processing_time"]),
            int(block_data["due_date"]),
            require_access=True,
        )
        if slot is None:
            entry_time = _empty_bay_entry(
                bay_schedules[bay_id],
                int(block_data["release_time"]),
                int(block_data["processing_time"]),
            )
            exit_time = entry_time + int(block_data["processing_time"])
        else:
            entry_time, exit_time = slot

        rebuilt[block_id] = {
            **original,
            "entry_time": int(entry_time),
            "exit_time": int(exit_time),
        }
        _append_assignment_to_state(rebuilt[block_id], blocks, bay_blocks, bay_schedules, bay_loads)

    return {"operations": _build_operations(rebuilt.values())}


def _best_insertion(
    prob_info: dict,
    block_id: int,
    bays,
    bay_blocks,
    bay_schedules,
    bay_loads,
    deadline: float,
    require_access: bool,
    use_exit_blocking_penalty: bool = False,
    use_multi_slot: bool = False,
    use_preference_bias: bool = False,
) -> dict | None:
    ranked = _ranked_insertions(
        prob_info,
        block_id,
        bays,
        bay_blocks,
        bay_schedules,
        bay_loads,
        deadline,
        require_access=require_access,
        limit=1,
        use_exit_blocking_penalty=use_exit_blocking_penalty,
        use_multi_slot=use_multi_slot,
        use_preference_bias=use_preference_bias,
    )
    return ranked[0][1] if ranked else None


def _ranked_insertions(
    prob_info: dict,
    block_id: int,
    bays,
    bay_blocks,
    bay_schedules,
    bay_loads,
    deadline: float,
    require_access: bool,
    limit: int,
    use_exit_blocking_penalty: bool = False,
    use_multi_slot: bool = False,
    use_preference_bias: bool = False,
) -> list[tuple[float, dict]]:
    from utils import Block

    blocks = prob_info["blocks"]
    block_data = blocks[block_id]
    weights = prob_info.get("weights", {})
    w1 = float(weights.get("w1", 1.0))
    w2 = float(weights.get("w2", 1.0))
    w3 = float(weights.get("w3", 1.0))
    preferences = block_data["bay_preferences"]
    s_max = max(preferences)
    bay_weights = _bay_weights(bays)

    ranked: list[tuple[float, dict]] = []

    if use_preference_bias:
        bay_order = sorted(
            range(len(bays)),
            key=lambda bay_id: (
                s_max - preferences[bay_id],
                bay_loads[bay_id],
                bay_id,
            ),
        )
    else:
        bay_order = sorted(
            range(len(bays)),
            key=lambda bay_id: (
                bay_loads[bay_id],
                -(preferences[bay_id]),
                bay_id,
            ),
        )
    for bay_id in bay_order:
        if time.monotonic() >= deadline - 0.05:
            break
        bay = bays[bay_id]
        for orient_idx in range(len(block_data.get("shape", []))):
            if time.monotonic() >= deadline - 0.05:
                break
            bbox = orientation_bbox(block_data, orient_idx)
            candidates = _candidate_positions(bay, bay_blocks[bay_id], bbox)
            for x, y in candidates:
                if time.monotonic() >= deadline - 0.05:
                    break
                block = Block(block_id, block_data, x=x, y=y, orient_idx=orient_idx)
                if not bay.contains_block(block):
                    continue
                slots = _safe_slots(
                    bay,
                    block,
                    bay_blocks[bay_id],
                    bay_schedules[bay_id],
                    int(block_data["release_time"]),
                    int(block_data["processing_time"]),
                    int(block_data["due_date"]),
                    require_access=require_access,
                    limit=4 if use_multi_slot and use_exit_blocking_penalty and require_access else 1,
                )
                if not slots:
                    continue
                for entry_time, exit_time in slots:
                    base_score = _insertion_score(
                        block_data,
                        bay_id,
                        entry_time,
                        exit_time,
                        bay_loads,
                        bay_weights,
                        s_max,
                        w1,
                        w2,
                        w3,
                        top_y=y + bbox[3],
                    )
                    score = base_score
                    if use_exit_blocking_penalty and require_access:
                        if len(ranked) >= limit and base_score > ranked[-1][0] + w1 * 20.0:
                            continue
                        blocker_penalty = _exit_blocking_penalty_units(
                            bay,
                            block,
                            bay_blocks[bay_id],
                            bay_schedules[bay_id],
                            entry_time,
                            exit_time,
                            limit=6,
                        )
                        score = base_score + w1 * blocker_penalty
                    if len(ranked) < limit or score < ranked[-1][0]:
                        assignment = {
                            "block_id": block_id,
                            "bay_id": bay_id,
                            "x": int(x),
                            "y": int(y),
                            "orient_idx": int(orient_idx),
                            "entry_time": int(entry_time),
                            "exit_time": int(exit_time),
                        }
                        ranked.append((score, assignment))
                        ranked.sort(key=lambda item: item[0])
                        if len(ranked) > limit:
                            ranked.pop()

    return ranked


def _append_assignment_to_state(assignment: dict, blocks: list[dict], bay_blocks, bay_schedules, bay_loads) -> None:
    from utils import Block

    block_id = assignment["block_id"]
    bay_id = assignment["bay_id"]
    block = Block(
        block_id=block_id,
        block_data=blocks[block_id],
        x=assignment["x"],
        y=assignment["y"],
        orient_idx=assignment["orient_idx"],
    )
    bay_blocks[bay_id].append(block)
    bay_schedules[bay_id].append((assignment["entry_time"], assignment["exit_time"]))
    bay_loads[bay_id] += blocks[block_id]["workload"]


def _earliest_safe_slot(
    bay,
    block,
    placed_blocks,
    schedule,
    release_time: int,
    processing_time: int,
    due_time: int,
    require_access: bool,
) -> tuple[int, int] | None:
    slots = _safe_slots(
        bay,
        block,
        placed_blocks,
        schedule,
        release_time,
        processing_time,
        due_time,
        require_access,
        limit=1,
    )
    return slots[0] if slots else None


def _safe_slots(
    bay,
    block,
    placed_blocks,
    schedule,
    release_time: int,
    processing_time: int,
    due_time: int,
    require_access: bool,
    limit: int,
) -> list[tuple[int, int]]:
    from utils import check_collisions, check_entry, check_exit

    existing_entry_times = {entry for entry, _ in schedule}
    candidate_entries = {int(release_time)}
    for placed_block, (entry, exit_time) in zip(placed_blocks, schedule):
        if exit_time >= release_time:
            candidate_entries.add(int(exit_time))
        if limit > 1:
            desired_exit = max(
                int(entry) + int(placed_block.block_data.get("processing_time", processing_time)),
                int(placed_block.block_data.get("due_date", due_time)),
            )
            if desired_exit >= release_time:
                candidate_entries.add(int(desired_exit))
    if limit > 1:
        due_entry = int(due_time) - int(processing_time)
        if due_entry >= release_time:
            candidate_entries.add(due_entry)

    slots: list[tuple[int, int]] = []
    for entry_time in sorted(candidate_entries):
        entry_time = max(release_time, int(entry_time))
        if entry_time in existing_entry_times:
            continue
        exit_time = entry_time + processing_time

        if require_access:
            present_at_entry = [
                other for other, (entry, exit) in zip(placed_blocks, schedule)
                if entry <= entry_time < exit
            ]
            if check_entry(bay, present_at_entry, block, fast=True):
                continue

            present_at_exit = [
                block,
                *[
                    other for other, (entry, exit) in zip(placed_blocks, schedule)
                    if entry < exit_time < exit
                ],
            ]
            if check_exit(bay, present_at_exit, block, fast=True):
                continue
            if _blocks_earlier_due_exits(
                bay,
                block,
                placed_blocks,
                schedule,
                entry_time,
                exit_time,
            ):
                continue

        collision = False
        for other, (other_entry, other_exit) in zip(placed_blocks, schedule):
            if entry_time < other_exit and other_entry < exit_time:
                if _cached_pair_collision(bay, block, other):
                    collision = True
                    break
        if collision:
            continue

        slots.append((entry_time, exit_time))
        if len(slots) >= limit:
            break

    return slots


def _blocks_earlier_due_exits(
    bay,
    new_block,
    placed_blocks,
    schedule,
    entry_time: int,
    exit_time: int,
) -> bool:
    from utils import check_exit

    new_due = int(new_block.block_data["due_date"])
    for target, (_, target_exit) in zip(placed_blocks, schedule):
        target_due = int(target.block_data["due_date"])
        if target_due > new_due:
            continue
        if not (entry_time < target_exit < exit_time):
            continue

        present = [
            new_block,
            target,
            *[
                other for other, (other_entry, other_exit) in zip(placed_blocks, schedule)
                if other.block_id != target.block_id and other_entry < target_exit < other_exit
            ],
        ]
        if check_exit(bay, present, target, fast=True):
            return True
    return False


def _exit_blocking_penalty_units(
    bay,
    new_block,
    placed_blocks,
    schedule,
    entry_time: int,
    exit_time: int,
    limit: int,
) -> float:
    """Return estimated tardiness units if new_block blocks critical exits.

    This is intentionally a soft pairwise signal, not a feasibility rule.
    Official `check_feasibility` still decides the final candidate.
    """

    from utils import check_exit

    new_due = int(new_block.block_data["due_date"])
    targets = []
    for target, (target_entry, target_exit) in zip(placed_blocks, schedule):
        if int(target.block_id) == int(new_block.block_id):
            continue

        target_due = int(target.block_data["due_date"])
        target_processing = int(target.block_data["processing_time"])
        desired_exit = max(int(target_entry) + target_processing, target_due)
        if not (int(target_entry) < desired_exit < int(target_exit)):
            continue
        if not (entry_time < desired_exit < exit_time):
            continue

        target_tardiness = max(0, int(target_exit) - target_due)
        if target_due > new_due and target_tardiness <= 0:
            continue

        target_slack = (
            target_due
            - int(target.block_data["release_time"])
            - target_processing
        )
        targets.append(
            (
                target_due,
                -target_tardiness,
                target_slack,
                target,
                desired_exit,
                target_tardiness,
            )
        )

    penalty = 0.0
    for _, _, target_slack, target, desired_exit, target_tardiness in sorted(targets)[:limit]:
        obstructions = check_exit(bay, [target, new_block], target, fast=False)
        if not any(int(obs.existing_block.block_id) == int(new_block.block_id) for obs in obstructions):
            continue

        delay_units = max(1, min(8, target_tardiness or int(target.block_data["processing_time"])))
        urgency = 1.0 + 0.25 * max(0, 3 - target_slack)
        penalty += min(20.0, delay_units * urgency)
    return penalty


def _cached_pair_collision(bay, block_a, block_b) -> bool:
    from utils import check_collisions

    key_a = _block_geometry_key(block_a)
    key_b = _block_geometry_key(block_b)
    if key_b < key_a:
        key_a, key_b = key_b, key_a
    key = (int(bay.id), key_a, key_b)
    cached = _COLLISION_CACHE.get(key)
    if cached is not None:
        return cached

    result = bool(check_collisions(bay, [block_a, block_b]))
    if len(_COLLISION_CACHE) >= _MAX_COLLISION_CACHE_SIZE:
        _COLLISION_CACHE.clear()
    _COLLISION_CACHE[key] = result
    return result


def _block_geometry_key(block) -> tuple[int, int, int, int]:
    return (
        int(block.block_id),
        int(block.x),
        int(block.y),
        int(block.orient_idx),
    )


def _candidate_positions(bay, placed_blocks, bbox) -> list[tuple[int, int]]:
    min_x, min_y, max_x, max_y = bbox
    xs = {lower_left_integer_position(bbox)[0]}
    ys = {lower_left_integer_position(bbox)[1]}
    xs.add(max(0, int(math.floor((bay.width - (max_x - min_x)) / 2 - min_x))))
    ys.add(max(0, int(math.floor((bay.height - (max_y - min_y)) / 2 - min_y))))
    for block in placed_blocks:
        rect = block.bounding_rect()
        xs.add(math.ceil(rect[2] - min_x))
        ys.add(math.ceil(rect[3] - min_y))

    result = []
    for x in sorted(xs):
        for y in sorted(ys):
            if x + max_x <= bay.width + 1e-6 and y + max_y <= bay.height + 1e-6:
                result.append((int(x), int(y)))
    return result[:_CANDIDATE_POSITION_LIMIT]


def _serial_append_assignment(prob_info: dict, block_id: int, bays, bay_schedules, bay_loads) -> dict:
    from utils import Block

    block_data = prob_info["blocks"][block_id]
    preferences = block_data["bay_preferences"]
    best_key = None
    best_value = None
    for bay_id, bay in enumerate(bays):
        for orient_idx in range(len(block_data.get("shape", []))):
            bbox = orientation_bbox(block_data, orient_idx)
            x, y = lower_left_integer_position(bbox)
            block = Block(block_id, block_data, x=x, y=y, orient_idx=orient_idx)
            if not bay.contains_block(block):
                continue
            entry_time = _empty_bay_entry(
                bay_schedules[bay_id],
                int(block_data["release_time"]),
                int(block_data["processing_time"]),
            )
            exit_time = entry_time + int(block_data["processing_time"])
            key = (
                max(0, exit_time - int(block_data["due_date"])),
                bay_loads[bay_id],
                -preferences[bay_id],
                bay_id,
            )
            if best_key is None or key < best_key:
                best_key = key
                best_value = (bay_id, x, y, orient_idx, entry_time, exit_time)

    if best_value is None:
        bay_id = min(range(len(bays)), key=lambda idx: (bay_loads[idx], idx))
        entry_time = _empty_bay_entry(
            bay_schedules[bay_id],
            int(block_data["release_time"]),
            int(block_data["processing_time"]),
        )
        best_value = (bay_id, 0, 0, 0, entry_time, entry_time + int(block_data["processing_time"]))

    bay_id, x, y, orient_idx, entry_time, exit_time = best_value
    return {
        "block_id": block_id,
        "bay_id": int(bay_id),
        "x": int(x),
        "y": int(y),
        "orient_idx": int(orient_idx),
        "entry_time": int(entry_time),
        "exit_time": int(exit_time),
    }


def _state_from_assignments(prob_info: dict, assignments: dict[int, dict], bays):
    from utils import Block

    blocks = prob_info["blocks"]
    bay_blocks = [[] for _ in bays]
    bay_schedules = [[] for _ in bays]
    bay_loads = [0.0 for _ in bays]
    for assignment in sorted(assignments.values(), key=lambda item: (item["entry_time"], item["block_id"])):
        block_id = assignment["block_id"]
        bay_id = assignment["bay_id"]
        bay_blocks[bay_id].append(
            Block(
                block_id,
                blocks[block_id],
                x=assignment["x"],
                y=assignment["y"],
                orient_idx=assignment["orient_idx"],
            )
        )
        bay_schedules[bay_id].append((assignment["entry_time"], assignment["exit_time"]))
        bay_loads[bay_id] += blocks[block_id]["workload"]
    return bay_blocks, bay_schedules, bay_loads


def _assignments_from_solution(solution: dict) -> dict[int, dict]:
    assignments: dict[int, dict] = {}
    entry_seq = 0
    for time_key, operations in sorted(solution.get("operations", {}).items(), key=lambda item: int(item[0])):
        time_idx = int(time_key)
        for op in operations:
            block_id = int(op["block_id"])
            assignments.setdefault(block_id, {"block_id": block_id})
            if op["type"] == "ENTRY":
                assignments[block_id].update(
                    {
                        "bay_id": int(op["bay_id"]),
                        "x": int(op.get("x", 0)),
                        "y": int(op.get("y", 0)),
                        "orient_idx": int(op.get("orient_idx", 0)),
                        "entry_time": time_idx,
                        "_seq": entry_seq,
                    }
                )
                entry_seq += 1
            elif op["type"] == "EXIT":
                assignments[block_id].update({"bay_id": int(op["bay_id"]), "exit_time": time_idx})
    return {bid: data for bid, data in assignments.items() if "entry_time" in data and "exit_time" in data}


def _build_operations(assignments: Iterable[dict]) -> dict:
    buckets: dict[int, list[tuple[int, int, int, dict]]] = {}
    for assignment in assignments:
        placement = Placement(
            block_id=int(assignment["block_id"]),
            bay_id=int(assignment["bay_id"]),
            x=int(assignment["x"]),
            y=int(assignment["y"]),
            orient_idx=int(assignment["orient_idx"]),
            entry_time=int(assignment["entry_time"]),
            exit_time=int(assignment["exit_time"]),
        )
        buckets.setdefault(placement.exit_time, []).append(
            (0, 0, placement.block_id, {"type": "EXIT", "block_id": placement.block_id, "bay_id": placement.bay_id})
        )
        buckets.setdefault(placement.entry_time, []).append(
            (
                1,
                int(assignment.get("_seq", placement.block_id)),
                placement.block_id,
                {
                    "type": "ENTRY",
                    "block_id": placement.block_id,
                    "bay_id": placement.bay_id,
                    "x": placement.x,
                    "y": placement.y,
                    "orient_idx": placement.orient_idx,
                },
            )
        )
    return {str(time_idx): [item[3] for item in sorted(items)] for time_idx, items in sorted(buckets.items())}


def _insertion_score(
    block_data: dict,
    bay_id: int,
    entry_time: int,
    exit_time: int,
    bay_loads: list[float],
    bay_weights: list[float],
    s_max: float,
    w1: float,
    w2: float,
    w3: float,
    top_y: float,
) -> float:
    tardiness = max(0.0, exit_time - block_data["due_date"])
    pref_penalty = s_max - block_data["bay_preferences"][bay_id]
    new_load = bay_loads[bay_id] + block_data["workload"]
    imbalance = max(
        (
            abs(bay_weights[bay_id] * new_load - bay_weights[other] * bay_loads[other])
            for other in range(len(bay_loads))
            if other != bay_id
        ),
        default=0.0,
    )
    return w1 * tardiness + w2 * imbalance + w3 * pref_penalty + 1e-3 * exit_time + 1e-4 * top_y


def _bay_weights(bays) -> list[float]:
    areas = [bay.width * bay.height for bay in bays]
    avg_area = sum(areas) / len(areas)
    return [avg_area / area for area in areas]


def _empty_bay_entry(schedule: list[tuple[int, int]], release_time: int, processing_time: int) -> int:
    entry_time = int(release_time)
    changed = True
    while changed:
        changed = False
        exit_time = entry_time + processing_time
        for other_entry, other_exit in schedule:
            if entry_time < other_exit and other_entry < exit_time:
                entry_time = max(entry_time, other_exit)
                changed = True
    return entry_time


def _worst_tardiness_removal(prob_info: dict, assignments: dict[int, dict], count: int) -> list[int]:
    blocks = prob_info["blocks"]
    ranked = sorted(
        assignments,
        key=lambda bid: (
            max(0, int(assignments[bid]["exit_time"]) - int(blocks[bid]["due_date"])),
            int(assignments[bid]["exit_time"]),
        ),
        reverse=True,
    )
    return ranked[:count]


def _worst_preference_removal(prob_info: dict, assignments: dict[int, dict], count: int) -> list[int]:
    blocks = prob_info["blocks"]
    ranked = []
    for block_id, assignment in assignments.items():
        block_data = blocks[block_id]
        preferences = block_data["bay_preferences"]
        bay_id = int(assignment["bay_id"])
        regret = max(preferences) - preferences[bay_id]
        if regret <= 0:
            continue
        exit_time = int(assignment["exit_time"])
        due_time = int(block_data["due_date"])
        tardiness = max(0, exit_time - due_time)
        slack = max(0, due_time - exit_time)
        ranked.append((regret, -tardiness, slack, -due_time, block_id))
    ranked.sort(reverse=True)
    selected = [block_id for *_score, block_id in ranked[:count]]
    if len(selected) < count:
        selected.extend(
            block_id
            for block_id in _worst_tardiness_removal(prob_info, assignments, count)
            if block_id not in selected
        )
    return selected[:count]


def _random_removal(assignments: dict[int, dict], count: int, rng: random.Random) -> list[int]:
    block_ids = list(assignments)
    rng.shuffle(block_ids)
    return block_ids[:count]


def _initial_operator_weights(
    use_early_exit_destroy: bool,
    total_budget: float,
    n_blocks: int,
) -> dict[str, float]:
    weights = {
        "worst2": 5.0,
        "access2": 4.0,
        "random2": 1.0,
    }
    if use_early_exit_destroy:
        weights["early2"] = 3.0
    if total_budget >= 120.0 and n_blocks >= 150:
        weights["worst3"] = 2.0
        weights["access3"] = 1.5
        weights["early3"] = 1.2
    if total_budget >= 240.0:
        weights["preference2"] = 1.0
        if n_blocks >= 150:
            weights["worst4"] = 1.8
            weights["access4"] = 1.4
            weights["early4"] = 1.2
            weights["preference3"] = 0.6
    if total_budget >= 500.0 and n_blocks >= 150:
        weights["worst5"] = 1.4
        weights["access5"] = 1.0
        weights["early5"] = 0.9
        weights["preference4"] = 0.4
    return weights


def _choose_operator(weights: dict[str, float], rng: random.Random) -> str:
    total = sum(max(0.01, weight) for weight in weights.values())
    threshold = rng.random() * total
    cumulative = 0.0
    for name, weight in weights.items():
        cumulative += max(0.01, weight)
        if cumulative >= threshold:
            return name
    return next(iter(weights))


def _apply_destroy_operator(
    operator: str,
    prob_info: dict,
    assignments: dict[int, dict],
    rng: random.Random,
) -> list[int]:
    count_match = re.search(r"(\d+)$", operator)
    count = int(count_match.group(1)) if count_match else 2
    if operator.startswith("random"):
        return _random_removal(assignments, count, rng)
    if operator.startswith("access"):
        return _access_blocker_removal(prob_info, assignments, count)
    if operator.startswith("early"):
        return early_exit_protection_removal(prob_info, assignments, count)
    if operator.startswith("preference"):
        return _worst_preference_removal(prob_info, assignments, count)
    return _worst_tardiness_removal(prob_info, assignments, count)


def _update_operator_weights(
    weights: dict[str, float],
    scores: dict[str, float],
    counts: dict[str, int],
) -> None:
    for name in list(weights):
        if counts[name]:
            average_score = scores[name] / counts[name]
            weights[name] = 0.8 * weights[name] + 0.2 * max(0.1, average_score)
        scores[name] = 0.0
        counts[name] = 0


def _access_blocker_removal(prob_info: dict, assignments: dict[int, dict], count: int) -> list[int]:
    blocks = prob_info["blocks"]
    tardy_targets = _worst_tardiness_removal(prob_info, assignments, max(1, min(4, count)))
    selected: list[int] = []
    seen: set[int] = set()

    for target_id in tardy_targets:
        if target_id not in assignments:
            continue
        target = assignments[target_id]
        if target_id not in seen:
            selected.append(target_id)
            seen.add(target_id)
        if len(selected) >= count:
            break

        target_entry = int(target["entry_time"])
        target_exit = int(target["exit_time"])
        ranked_blockers = []
        for other_id, other in assignments.items():
            if other_id in seen or other_id == target_id:
                continue
            if other["bay_id"] != target["bay_id"]:
                continue
            other_entry = int(other["entry_time"])
            other_exit = int(other["exit_time"])
            overlap = min(target_exit, other_exit) - max(target_entry, other_entry)
            spans_entry = other_entry < target_entry < other_exit
            spans_exit = other_entry < target_exit < other_exit
            if overlap <= 0 and not spans_entry and not spans_exit:
                continue
            ranked_blockers.append(
                (
                    not spans_entry,
                    not spans_exit,
                    -max(0, overlap),
                    int(blocks[other_id]["due_date"]),
                    other_id,
                )
            )

        for *_score, other_id in sorted(ranked_blockers):
            selected.append(other_id)
            seen.add(other_id)
            if len(selected) >= count:
                break
        if len(selected) >= count:
            break

    if len(selected) < count:
        for block_id in _worst_tardiness_removal(prob_info, assignments, count):
            if block_id not in seen:
                selected.append(block_id)
                seen.add(block_id)
            if len(selected) >= count:
                break
    return selected


def _violation_block_ids(violations: list[str]) -> list[int]:
    result = []
    seen = set()
    for violation in violations:
        for match in _BLOCK_RE.finditer(violation):
            block_id = int(match.group(1))
            if block_id not in seen:
                seen.add(block_id)
                result.append(block_id)
    return result


def _better(current: SearchResult, candidate: SearchResult) -> SearchResult:
    if not candidate.feasible:
        return current
    if not current.feasible:
        return candidate
    if candidate.objective is None:
        return current
    if current.objective is None or candidate.objective < current.objective:
        return candidate
    return current


def _fit_difficulty(block_data: dict) -> float:
    areas = []
    for orient_idx in range(len(block_data.get("shape", []))):
        min_x, min_y, max_x, max_y = orientation_bbox(block_data, orient_idx)
        areas.append((max_x - min_x) * (max_y - min_y))
    return min(areas) if areas else 1.0


def _stable_seed(prob_info: dict) -> int:
    name = str(prob_info.get("name", "ogc"))
    return sum((idx + 1) * ord(ch) for idx, ch in enumerate(name)) % (2**31)
