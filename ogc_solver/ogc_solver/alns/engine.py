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

from ..planner import build_initial_solution
from ..state import Placement, lower_left_integer_position, orientation_bbox
from ..stabilize import stabilize_solution
from ..subsolvers import early_exit_protection_removal, should_run_schedule_polish

_BLOCK_RE = re.compile(r"block\s+(\d+)")
_COLLISION_CACHE: dict[tuple, bool] = {}
_MAX_COLLISION_CACHE_SIZE = 200_000
_CANDIDATE_POSITION_LIMIT = 80


@dataclass(frozen=True)
class SearchResult:
    solution: dict
    feasible: bool
    objective: float | None


def solve_alns(prob_info: dict, deadline: float, fallback_solution: dict | None = None) -> dict:
    """Run a small access-aware ALNS loop and return the best feasible solution."""

    from utils import check_feasibility

    _COLLISION_CACHE.clear()
    started_at = time.monotonic()
    total_budget = max(0.0, deadline - started_at)
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
    if total_budget >= 120.0:
        seed_budget = min(60.0, total_budget)
        seed_phase_deadline = min(search_deadline, started_at + seed_budget)
        seed_build_deadline = started_at + seed_budget * 0.85
    else:
        seed_phase_deadline = deadline
        seed_build_deadline = search_deadline
    rng = random.Random(_stable_seed(prob_info))

    for seed_solution in _initial_seeds(prob_info, seed_build_deadline, seed_phase_deadline, total_budget):
        seed_result = check_feasibility(prob_info, seed_solution)
        if seed_result.get("feasible"):
            best = _better(best, SearchResult(seed_solution, True, seed_result["objective"]))

    use_polishing = should_run_schedule_polish(total_budget)
    initial_polish_deadline = _initial_polish_deadline(total_budget, search_deadline)
    if use_polishing and best.feasible and time.monotonic() < initial_polish_deadline - 0.5:
        compressed = _compress_solution(prob_info, best.solution, initial_polish_deadline)
        compressed_result = check_feasibility(prob_info, compressed)
        if compressed_result.get("feasible"):
            best = _better(best, SearchResult(compressed, True, compressed_result["objective"]))

    current = best.solution
    current_obj = float(best.objective or math.inf)
    iteration = 0
    temperature = max(1.0, current_obj * 0.01 if math.isfinite(current_obj) else 1.0)
    use_early_exit_destroy = len(prob_info["blocks"]) >= 150
    use_adaptive_ops = total_budget >= 120.0 and len(prob_info["blocks"]) < 150
    operator_weights = _initial_operator_weights(use_early_exit_destroy, total_budget, len(prob_info["blocks"]))
    operator_scores = {name: 0.0 for name in operator_weights}
    operator_counts = {name: 0 for name in operator_weights}
    while time.monotonic() < search_deadline - 0.5:
        iteration += 1
        assignments = _assignments_from_solution(current)
        if not assignments:
            break

        if use_adaptive_ops:
            operator = _choose_operator(operator_weights, rng)
            removed = _apply_destroy_operator(operator, prob_info, assignments, rng)
        else:
            operator = "fixed"
            remove_count = 2
            if iteration % 7 == 0:
                removed = _random_removal(assignments, remove_count, rng)
            elif use_early_exit_destroy and iteration % 9 == 1:
                removed = early_exit_protection_removal(prob_info, assignments, remove_count)
            elif iteration % 5 == 1:
                removed = _access_blocker_removal(prob_info, assignments, remove_count)
            else:
                removed = _worst_tardiness_removal(prob_info, assignments, remove_count)

        candidate = _repair_removed(
            prob_info,
            assignments,
            removed,
            search_deadline,
            use_regret=(iteration % 6 == 2),
        )
        if candidate is None:
            if use_adaptive_ops:
                operator_counts[operator] += 1
            continue

        result = check_feasibility(prob_info, candidate)
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

    if use_polishing and best.feasible and time.monotonic() < deadline - 0.5:
        compressed = _compress_solution(prob_info, best.solution, deadline)
        compressed_result = check_feasibility(prob_info, compressed)
        if compressed_result.get("feasible"):
            best = _better(best, SearchResult(compressed, True, compressed_result["objective"]))

    if best.feasible and time.monotonic() < deadline - 0.5:
        best_result = check_feasibility(prob_info, best.solution)
        polish_deadline = deadline - 0.35
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

    final_result = check_feasibility(prob_info, best.solution)
    if final_result.get("feasible"):
        return best.solution
    return fallback_solution


def _set_candidate_position_limit(total_budget: float, n_blocks: int) -> None:
    global _CANDIDATE_POSITION_LIMIT
    if n_blocks >= 150 and total_budget < 90.0:
        _CANDIDATE_POSITION_LIMIT = 20
    else:
        _CANDIDATE_POSITION_LIMIT = 80


def _initial_seeds(
    prob_info: dict,
    build_deadline: float,
    repair_deadline: float,
    total_budget: float,
) -> list[dict]:
    seeds = []
    if time.monotonic() < build_deadline:
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
            assignment = _fast_release_assignment(prob_info, block_id, bays, bay_blocks, bay_schedules, bay_loads)
        if assignment is None:
            assignment = _exact_release_assignment(
                prob_info,
                block_id,
                bays,
                bay_blocks,
                bay_schedules,
                bay_loads,
                deadline,
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


def _exact_release_assignment(
    prob_info: dict,
    block_id: int,
    bays,
    bay_blocks,
    bay_schedules,
    bay_loads,
    deadline: float,
) -> dict | None:
    from utils import Block, check_entry, check_exit

    block_data = prob_info["blocks"][block_id]
    entry_time = int(block_data["release_time"])
    exit_time = entry_time + int(block_data["processing_time"])
    due_time = int(block_data["due_date"])
    preferences = block_data["bay_preferences"]
    s_max = max(preferences)
    best = None

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
) -> dict | None:
    from utils import Block, check_entry, check_exit

    block_data = prob_info["blocks"][block_id]
    entry_time = int(block_data["release_time"])
    exit_time = entry_time + int(block_data["processing_time"])
    preferences = block_data["bay_preferences"]
    best = None
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
        allow_chain = total_budget is None or total_budget < 240.0
        deep_exit_repair = total_budget is not None and 90.0 <= total_budget < 210.0
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
    max_count = 2 if n_blocks < 150 else 3
    candidate_sets: list[tuple[list[int], bool, bool]] = []

    if allow_chain and deep_exit_repair and n_blocks >= 150 and time.monotonic() < deadline - 2.0:
        candidate_sets.append(
            (
                _actual_exit_blocker_chain_removal(prob_info, assignments, 4, deadline, use_second_hop=True),
                False,
                True,
            )
        )
        if time.monotonic() < deadline - 2.0:
            candidate_sets.append(
                (
                    _actual_exit_blocker_chain_removal(prob_info, assignments, 4, deadline, use_second_hop=True),
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
    count = 3 if operator.endswith("3") else 2
    if operator.startswith("random"):
        return _random_removal(assignments, count, rng)
    if operator.startswith("access"):
        return _access_blocker_removal(prob_info, assignments, count)
    if operator.startswith("early"):
        return early_exit_protection_removal(prob_info, assignments, count)
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
