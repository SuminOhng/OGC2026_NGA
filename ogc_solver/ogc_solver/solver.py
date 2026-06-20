"""Top-level solver orchestration."""

from __future__ import annotations

import copy
import os
import time

from .cpp_accelerator import (
    _build_operations,
    _proxy_solution_rank,
    _solution_assignments,
    polish_cpp_candidates_with_seeds,
    solve_cpp_accelerator_candidates,
)


def solve(prob_info: dict, timelimit: float = 60) -> dict:
    """Return a solution dictionary for one problem instance.

    The default submission path is C++ only. Python serializes the problem,
    runs the native solver, validates native candidates with the official
    checker when available, and returns the best C++ candidate. If C++ produces
    no usable candidate, the returned empty solution makes that failure visible.
    """

    safe_timelimit = max(0.0, float(timelimit))
    guard_seconds = min(1.0, max(0.35, safe_timelimit * 0.02))
    if (
        len(prob_info.get("blocks", [])) < 150
        and safe_timelimit >= 45.0
        and float(prob_info.get("weights", {}).get("w1", 1.0)) >= 1000.0
    ):
        guard_seconds = max(guard_seconds, 5.0)
    if _late_tail_final_polish_profile(prob_info, safe_timelimit):
        guard_seconds = max(guard_seconds, 25.0)
    if _wide_tail_short_polish_profile(prob_info, safe_timelimit):
        guard_seconds = max(guard_seconds, 7.0)
    if len(prob_info.get("blocks", [])) >= 250 and 45.0 <= safe_timelimit < 90.0:
        guard_seconds = max(guard_seconds, 5.0)
    if (
        len(prob_info.get("blocks", [])) >= 250
        and safe_timelimit >= 240.0
        and float(prob_info.get("weights", {}).get("w1", 1.0)) < 1000.0
    ):
        guard_seconds = max(guard_seconds, 12.0)
    started_at = time.monotonic()
    deadline = started_at + max(0.0, safe_timelimit - guard_seconds)
    final_deadline = started_at + max(0.0, safe_timelimit - min(0.75, max(0.25, safe_timelimit * 0.0025)))
    reference_prob_info = copy.deepcopy(prob_info)
    if len(reference_prob_info.get("blocks", [])) >= 150 and safe_timelimit >= 240.0:
        reference_prob_info["_use_fast_release_seed"] = True
    cpp_candidates = _try_cpp_accelerator(reference_prob_info, safe_timelimit, deadline)
    protected_incumbent = _best_checked_cpp_candidate(reference_prob_info, cpp_candidates)
    if protected_incumbent is not None:
        checked_seed_pool = _top_checked_cpp_candidates(reference_prob_info, cpp_candidates, limit=6)
        protected_incumbent = _try_cpp_seed_polish_feedback(
            reference_prob_info,
            protected_incumbent,
            safe_timelimit,
            deadline,
            checked_seed_pool,
            final_deadline=final_deadline,
        )
        return _finalize_checked_solution(reference_prob_info, protected_incumbent, safe_timelimit, started_at)

    return _cpp_failure_solution()


def _try_cpp_accelerator(prob_info: dict, safe_timelimit: float, deadline: float) -> list[dict]:
    if os.environ.get("OGC_DISABLE_CPP_ACCEL", "").strip():
        return []
    if safe_timelimit < 10.0:
        return []
    remaining = deadline - time.monotonic()
    if remaining < 1.0:
        return []
    n_blocks = len(prob_info.get("blocks", []))
    if (
        n_blocks >= 250
        and len(prob_info.get("bays", [])) <= 4
        and float(prob_info.get("weights", {}).get("w1", 1.0)) < 1000.0
        and safe_timelimit >= 240.0
    ):
        budget = min(36.0, max(34.0, remaining * 0.12))
    elif n_blocks >= 150 and safe_timelimit >= 240.0:
        budget = min(30.0, max(28.0, remaining * 0.10))
    elif n_blocks >= 250 and safe_timelimit >= 45.0:
        feedback_reserve = min(44.0, max(38.0, remaining * 0.70))
        budget = min(14.0, max(5.0, remaining - feedback_reserve))
    elif n_blocks >= 150 and safe_timelimit >= 45.0:
        feedback_reserve = min(30.0, max(24.0, remaining * 0.55))
        budget = min(24.0, max(5.0, remaining - feedback_reserve))
    elif (
        n_blocks < 150
        and safe_timelimit >= 240.0
        and len(prob_info.get("bays", [])) == 2
        and float(prob_info.get("weights", {}).get("w1", 1.0)) >= 1000.0
        and float(prob_info.get("weights", {}).get("w3", 1.0)) <= 220.0
    ):
        short_budget = min(20.0, max(10.0, remaining * 0.07))
        long_budget = min(115.0, max(95.0, remaining * 0.39))
        candidates = solve_cpp_accelerator_candidates(copy.deepcopy(prob_info), timeout_seconds=short_budget)
        candidates.extend(
            solve_cpp_accelerator_candidates(copy.deepcopy(prob_info), timeout_seconds=long_budget)
        )
        return candidates
    elif (
        n_blocks < 150
        and safe_timelimit >= 240.0
        and float(prob_info.get("weights", {}).get("w1", 1.0)) >= 1000.0
        and 250.0 <= float(prob_info.get("weights", {}).get("w3", 1.0)) <= 350.0
    ):
        short_budget = min(10.0, max(5.0, remaining * 0.04))
        long_budget = min(70.0, max(55.0, remaining * 0.24))
        candidates = solve_cpp_accelerator_candidates(copy.deepcopy(prob_info), timeout_seconds=short_budget)
        candidates.extend(
            solve_cpp_accelerator_candidates(copy.deepcopy(prob_info), timeout_seconds=long_budget)
        )
        return candidates
    else:
        budget = min(10.0, max(0.25, remaining * 0.30))
    return solve_cpp_accelerator_candidates(copy.deepcopy(prob_info), timeout_seconds=budget)


def _finalize_checked_solution(
    prob_info: dict,
    solution: dict,
    safe_timelimit: float,
    started_at: float,
) -> dict:
    """Return the C++ candidate unchanged; no Python fallback is allowed here."""

    return solution


def _cpp_failure_solution() -> dict:
    return {"operations": {}}


def _try_cpp_seed_polish_feedback(
    prob_info: dict,
    incumbent: dict,
    safe_timelimit: float,
    deadline: float,
    seed_pool: list[dict] | None = None,
    final_deadline: float | None = None,
) -> dict:
    if os.environ.get("OGC_DISABLE_CPP_SEED_FEEDBACK", "").strip():
        return incumbent
    n_blocks = len(prob_info.get("blocks", []))
    allow_small_high_w1_tail = (
        safe_timelimit >= 45.0
        and n_blocks >= 100
        and float(prob_info.get("weights", {}).get("w1", 1.0)) >= 1000.0
    )
    if n_blocks < 150 and not allow_small_high_w1_tail:
        return incumbent
    current = incumbent
    if _checked_obj1(prob_info, current) <= 0.0:
        return current
    pool_limit = 24 if safe_timelimit >= 240.0 else 6
    pool = _top_checked_cpp_candidates(prob_info, [*(seed_pool or []), current], limit=pool_limit)
    if not pool:
        pool = [current]
    if n_blocks < 150 and allow_small_high_w1_tail:
        if safe_timelimit >= 240.0:
            max_rounds = 5
            max_round_seconds = 120.0
        elif safe_timelimit >= 55.0:
            max_rounds = 4
            max_round_seconds = 10.0
        else:
            max_rounds = 2
            max_round_seconds = 8.0
        beam_width = 8 if safe_timelimit < 240.0 else 1
    elif safe_timelimit >= 240.0:
        max_rounds = 24
        max_round_seconds = 24.0
        beam_width = 1
    elif safe_timelimit >= 90.0:
        max_rounds = 4
        max_round_seconds = 14.0
        beam_width = 2
    elif safe_timelimit >= 45.0:
        max_rounds = 2 if n_blocks >= 250 else 1
        max_round_seconds = 10.0
        beam_width = 2
    else:
        max_rounds = 2
        max_round_seconds = 8.0
        beam_width = 1
    stale_rounds = 0
    focused_late_shift_attempts: set[int] = set()
    for round_index in range(max_rounds):
        remaining = deadline - time.monotonic()
        if remaining < 4.0:
            break
        current_obj1 = _checked_obj1(prob_info, current)
        focused_bucket = int(round(current_obj1))
        if (
            os.environ.get("OGC_ENABLE_CPP_FOCUSED_MID_SHIFT", "").strip() == "1"
            and
            _focused_late_shift_enabled(prob_info, safe_timelimit, current_obj1, require_env=True)
            and focused_bucket not in focused_late_shift_attempts
            and remaining >= 2.5
        ):
            focused_late_shift_attempts.add(focused_bucket)
            improved = _try_cpp_focused_late_shift(prob_info, current, deadline)
            if improved is not current:
                current = improved
                stale_rounds = 0
                if _checked_obj1(prob_info, current) <= 0.0:
                    break
                continue
        target_budget = _cpp_feedback_round_budget(current_obj1, max_round_seconds)
        if (
            safe_timelimit >= 240.0
            and current_obj1 <= 300.0
            and float(prob_info.get("weights", {}).get("w1", 1.0)) >= 1000.0
            and n_blocks <= 140
        ):
            target_budget = min(max_round_seconds, 120.0)
        if _wide_tail_short_polish_profile(prob_info, safe_timelimit) and 0.0 < current_obj1 <= 180.0:
            target_budget = min(target_budget, 12.0)
        if current_obj1 > 1000.0:
            budget = min(target_budget, max(1.0, remaining - 1.0))
        elif (
            safe_timelimit >= 240.0
            and current_obj1 <= 50.0
            and n_blocks <= 140
            and float(prob_info.get("weights", {}).get("w1", 1.0)) >= 1000.0
        ):
            budget = min(target_budget, max(1.0, remaining - 1.0))
        else:
            planned_rounds = 14 if safe_timelimit >= 240.0 else max_rounds
            rounds_left = max(1, min(max_rounds - round_index, planned_rounds - min(round_index, planned_rounds - 1)))
            future_round_budget = min(12.0, max_round_seconds)
            min_future_budget = future_round_budget * max(0, rounds_left - 1)
            if remaining - 1.0 > target_budget + min_future_budget:
                budget = target_budget
            else:
                budget = min(target_budget, max(1.0, (remaining - 1.0) / rounds_left))
        medium_high_w1_tail = (
            safe_timelimit >= 45.0
            and n_blocks <= 140
            and current_obj1 > 0.0
            and current_obj1 <= 250.0
            and float(prob_info.get("weights", {}).get("w1", 1.0)) >= 1000.0
        )
        active_beam_width = 4 if current_obj1 > 1000.0 else beam_width
        seeds_to_polish = [copy.deepcopy(seed) for seed in pool[:active_beam_width]]
        candidates = polish_cpp_candidates_with_seeds(
            copy.deepcopy(prob_info),
            seeds_to_polish,
            timeout_seconds=budget,
        )
        if not candidates:
            break
        if current_obj1 > 1000.0 or medium_high_w1_tail:
            combined_candidates = []
            combine_sources = 120 if medium_high_w1_tail else 80
            combine_accepts = 32 if medium_high_w1_tail else 24
            combine_scan = None
            for seed in seeds_to_polish:
                combined_candidates.extend(
                    _combine_checked_single_move_candidates(
                        prob_info,
                        seed,
                        candidates,
                        max_sources=combine_sources,
                        max_accepts=combine_accepts,
                        max_candidates_to_scan=combine_scan,
                    )
                )
            candidates.extend(combined_candidates)
        pool = _top_checked_cpp_candidates(prob_info, [*pool, *candidates], limit=pool_limit)
        next_solution = _best_checked_cpp_candidate(prob_info, [current, *pool])
        if next_solution is None:
            break
        improved = _better_checked_solution(prob_info, current, next_solution)
        if improved is current:
            stale_rounds += 1
            stale_limit = 3
            if stale_rounds >= stale_limit:
                break
            continue
        current = improved
        stale_rounds = 0
        if _checked_obj1(prob_info, current) <= 0.0:
            break
    current_obj1 = _checked_obj1(prob_info, current)
    if _late_tail_final_polish_profile(prob_info, safe_timelimit) and 0.0 < current_obj1 <= 60.0:
        polish_deadline = final_deadline if final_deadline is not None else deadline
        for _ in range(5):
            previous_obj1 = current_obj1
            remaining = polish_deadline - time.monotonic()
            if remaining < 6.0:
                break
            old_blocker_push = os.environ.get("OGC_CPP_ENABLE_BLOCKER_PUSH")
            os.environ["OGC_CPP_ENABLE_BLOCKER_PUSH"] = "1"
            try:
                candidates = polish_cpp_candidates_with_seeds(
                    copy.deepcopy(prob_info),
                    [copy.deepcopy(current)],
                    timeout_seconds=min(12.0, max(1.0, remaining - 0.5)),
                )
            finally:
                if old_blocker_push is None:
                    os.environ.pop("OGC_CPP_ENABLE_BLOCKER_PUSH", None)
                else:
                    os.environ["OGC_CPP_ENABLE_BLOCKER_PUSH"] = old_blocker_push
            current = _better_checked_solution(
                prob_info,
                current,
                _best_checked_cpp_candidate(prob_info, candidates),
            )
            current_obj1 = _checked_obj1(prob_info, current)
            if current_obj1 >= previous_obj1 - 1e-6 or current_obj1 <= 0.0:
                break
    if _wide_tail_short_polish_profile(prob_info, safe_timelimit) and 0.0 < current_obj1 <= 180.0:
        polish_deadline = final_deadline if final_deadline is not None else deadline
        old_blocker_push = os.environ.get("OGC_CPP_ENABLE_BLOCKER_PUSH")
        os.environ["OGC_CPP_ENABLE_BLOCKER_PUSH"] = "1"
        try:
            for _ in range(4):
                previous_obj1 = current_obj1
                remaining = polish_deadline - time.monotonic()
                if remaining < 3.0:
                    break
                candidates = polish_cpp_candidates_with_seeds(
                    copy.deepcopy(prob_info),
                    [copy.deepcopy(current)],
                    timeout_seconds=min(8.0, max(1.0, remaining - 0.5)),
                )
                if not candidates:
                    break
                current = _better_checked_solution(
                    prob_info,
                    current,
                    _best_checked_cpp_candidate(prob_info, candidates),
                )
                current_obj1 = _checked_obj1(prob_info, current)
                if current_obj1 >= previous_obj1 - 1e-6 or current_obj1 <= 0.0:
                    break
        finally:
            if old_blocker_push is None:
                os.environ.pop("OGC_CPP_ENABLE_BLOCKER_PUSH", None)
            else:
                os.environ["OGC_CPP_ENABLE_BLOCKER_PUSH"] = old_blocker_push
    if _two_bay_tail_final_polish_profile(prob_info, safe_timelimit) and 0.0 < current_obj1 <= 750.0:
        polish_deadline = final_deadline if final_deadline is not None else deadline
        for _ in range(8):
            previous_obj1 = current_obj1
            remaining = polish_deadline - time.monotonic()
            if remaining < 5.0:
                break
            candidates = polish_cpp_candidates_with_seeds(
                copy.deepcopy(prob_info),
                [copy.deepcopy(current)],
                timeout_seconds=min(12.0, max(1.0, remaining - 0.5)),
            )
            current = _better_checked_solution(
                prob_info,
                current,
                _best_checked_cpp_candidate(prob_info, candidates),
            )
            current_obj1 = _checked_obj1(prob_info, current)
            if current_obj1 >= previous_obj1 - 1e-6 or current_obj1 <= 0.0:
                break
    if _focused_late_shift_enabled(prob_info, safe_timelimit, current_obj1):
        focus_deadline = final_deadline if final_deadline is not None else deadline
        for _ in range(4):
            current_obj1 = _checked_obj1(prob_info, current)
            if not _focused_late_shift_enabled(prob_info, safe_timelimit, current_obj1):
                break
            improved = _try_cpp_focused_late_shift(prob_info, current, focus_deadline)
            if improved is current:
                break
            current = improved
    return current


def _late_tail_final_polish_profile(prob_info: dict, safe_timelimit: float) -> bool:
    return (
        safe_timelimit >= 240.0
        and len(prob_info.get("blocks", [])) <= 140
        and len(prob_info.get("bays", [])) >= 3
        and float(prob_info.get("weights", {}).get("w1", 1.0)) >= 1000.0
        and 250.0 <= float(prob_info.get("weights", {}).get("w3", 1.0)) <= 350.0
    )


def _wide_tail_short_polish_profile(prob_info: dict, safe_timelimit: float) -> bool:
    return (
        safe_timelimit >= 240.0
        and len(prob_info.get("blocks", [])) <= 140
        and len(prob_info.get("bays", [])) >= 3
        and float(prob_info.get("weights", {}).get("w1", 1.0)) >= 1000.0
        and float(prob_info.get("weights", {}).get("w3", 1.0)) <= 200.0
    )


def _two_bay_tail_final_polish_profile(prob_info: dict, safe_timelimit: float) -> bool:
    return (
        safe_timelimit >= 240.0
        and len(prob_info.get("blocks", [])) <= 140
        and len(prob_info.get("bays", [])) == 2
        and float(prob_info.get("weights", {}).get("w1", 1.0)) >= 1000.0
        and float(prob_info.get("weights", {}).get("w3", 1.0)) <= 220.0
    )


def _focused_late_shift_enabled(
    prob_info: dict,
    safe_timelimit: float,
    current_obj1: float,
    *,
    require_env: bool = False,
) -> bool:
    if os.environ.get("OGC_DISABLE_CPP_FOCUSED_LATE_SHIFT", "").strip():
        return False
    if os.environ.get("OGC_ENABLE_CPP_FOCUSED_LATE_SHIFT", "").strip() != "1":
        return False
    return (
        safe_timelimit >= 240.0
        and current_obj1 > 1000.0
        and current_obj1 <= 6610.0
        and len(prob_info.get("blocks", [])) >= 250
        and len(prob_info.get("bays", [])) <= 4
        and float(prob_info.get("weights", {}).get("w1", 1.0)) < 1000.0
    )


def _try_cpp_focused_late_shift(prob_info: dict, incumbent: dict, deadline: float) -> dict:
    remaining = deadline - time.monotonic()
    if remaining < 2.0:
        return incumbent
    old_value = os.environ.get("OGC_CPP_FOCUSED_LATE_SHIFT")
    os.environ["OGC_CPP_FOCUSED_LATE_SHIFT"] = "1"
    try:
        candidates = polish_cpp_candidates_with_seeds(
            copy.deepcopy(prob_info),
            [copy.deepcopy(incumbent)],
            timeout_seconds=min(4.0, max(1.0, remaining - 0.2)),
        )
    finally:
        if old_value is None:
            os.environ.pop("OGC_CPP_FOCUSED_LATE_SHIFT", None)
        else:
            os.environ["OGC_CPP_FOCUSED_LATE_SHIFT"] = old_value
    if not candidates:
        return incumbent
    return _better_checked_solution(
        prob_info,
        incumbent,
        _best_checked_focused_single_shift_candidate(prob_info, incumbent, candidates),
    )


def _best_checked_focused_single_shift_candidate(
    prob_info: dict,
    incumbent: dict,
    candidates: list[dict],
    *,
    max_checks: int = 360,
) -> dict | None:
    n_blocks = len(prob_info.get("blocks", []))
    blocks = prob_info.get("blocks", [])
    base_rows = _solution_assignments(incumbent, n_blocks)
    if base_rows is None:
        return None
    base_by_id = {int(row["block_id"]): row for row in base_rows}
    ranked: list[tuple[tuple[int, int, float, int], dict]] = []
    for candidate in candidates:
        rows = _solution_assignments(candidate, n_blocks)
        if rows is None:
            continue
        changed: list[tuple[int, dict, dict]] = []
        obj1 = 0.0
        for row in rows:
            block_id = int(row["block_id"])
            if block_id < 0 or block_id >= len(blocks):
                obj1 = float("inf")
                break
            block = blocks[block_id]
            obj1 += max(0, int(row["exit_time"]) - int(block["due_date"]))
            before = base_by_id.get(block_id)
            if before is None:
                changed.append((block_id, {}, row))
                continue
            if any(
                int(row[key]) != int(before[key])
                for key in ("bay_id", "x", "y", "orient_idx", "entry_time", "exit_time")
            ):
                changed.append((block_id, before, row))
        if len(changed) != 1 or not obj1 < float("inf"):
            continue
        block_id, before, after = changed[0]
        if not before:
            continue
        if any(int(before[key]) != int(after[key]) for key in ("bay_id", "x", "y", "orient_idx")):
            continue
        if int(after["entry_time"]) >= int(before["entry_time"]):
            continue
        shift = int(before["entry_time"]) - int(after["entry_time"])
        if 8 <= shift <= 20:
            shift_bucket = 0
        elif 3 <= shift < 8:
            shift_bucket = 1
        elif 21 <= shift <= 45:
            shift_bucket = 2
        else:
            shift_bucket = 3
        ranked.append(((shift_bucket, abs(shift - 12), obj1, -shift), candidate))
    if not ranked:
        return None
    ranked.sort(key=lambda item: item[0])

    try:
        from utils import check_feasibility

        best_solution = None
        best_rank = None
        for _proxy_rank, candidate in ranked[:max_checks]:
            result = check_feasibility(prob_info, candidate)
            if not result.get("feasible"):
                continue
            objective = result.get("objective")
            obj1 = result.get("obj1")
            if objective is None or obj1 is None:
                continue
            rank = (float(obj1), float(objective))
            if best_rank is None or rank < best_rank:
                best_solution = candidate
                best_rank = rank
        return best_solution
    except Exception:
        return None


def _cpp_feedback_round_budget(current_obj1: float, max_round_seconds: float) -> float:
    if current_obj1 <= 5.0:
        return min(max_round_seconds, 12.0)
    if current_obj1 <= 8.0:
        return min(max_round_seconds, 14.0)
    if current_obj1 <= 9.0:
        return min(max_round_seconds, 23.0)
    return min(max_round_seconds, 20.0)


def _combine_checked_single_move_candidates(
    prob_info: dict,
    base_solution: dict,
    candidates: list[dict],
    *,
    max_sources: int,
    max_accepts: int,
    max_candidates_to_scan: int | None = None,
) -> list[dict]:
    """Greedily compose official-feasible one-block moves proposed by C++."""

    n_blocks = len(prob_info.get("blocks", []))
    base_rows = _solution_assignments(base_solution, n_blocks)
    if base_rows is None:
        return []
    base_by_id = {int(row["block_id"]): row for row in base_rows}
    try:
        from utils import check_feasibility

        base_result = check_feasibility(prob_info, base_solution)
    except Exception:
        return []
    if not base_result.get("feasible"):
        return []
    current_rank = (float(base_result.get("obj1", float("inf"))), float(base_result.get("objective", float("inf"))))
    ranked_moves: list[tuple[tuple[float, float], int, dict]] = []
    for candidate_index, candidate in enumerate(candidates):
        if max_candidates_to_scan is not None and candidate_index >= max_candidates_to_scan:
            break
        rows = _solution_assignments(candidate, n_blocks)
        if rows is None:
            continue
        changed: list[int] = []
        replacement: dict | None = None
        for row in rows:
            block_id = int(row["block_id"])
            before = base_by_id.get(block_id)
            if before is None:
                changed.append(block_id)
                replacement = row
                continue
            if any(
                int(row[key]) != int(before[key])
                for key in ("bay_id", "x", "y", "orient_idx", "entry_time", "exit_time")
            ):
                changed.append(block_id)
                replacement = row
        if len(changed) != 1 or replacement is None:
            continue
        try:
            result = check_feasibility(prob_info, candidate)
        except Exception:
            continue
        if not result.get("feasible") or result.get("obj1") is None or result.get("objective") is None:
            continue
        ranked_moves.append(((float(result["obj1"]), float(result["objective"])), changed[0], replacement))
    if not ranked_moves:
        return []

    ranked_moves.sort(key=lambda item: item[0])
    current_rows = [dict(row) for row in base_rows]
    used_blocks: set[int] = set()
    accepted: list[dict] = []
    seen_replacements: set[tuple[int, int, int, int, int, int, int]] = set()
    for _rank, block_id, replacement in ranked_moves[:max_sources]:
        replacement_key = (
            int(block_id),
            int(replacement["bay_id"]),
            int(replacement["x"]),
            int(replacement["y"]),
            int(replacement["orient_idx"]),
            int(replacement["entry_time"]),
            int(replacement["exit_time"]),
        )
        if block_id in used_blocks or replacement_key in seen_replacements:
            continue
        seen_replacements.add(replacement_key)
        trial_rows = [dict(row) for row in current_rows]
        for row in trial_rows:
            if int(row["block_id"]) == block_id:
                row.update(replacement)
                break
        trial = {"operations": _build_operations(trial_rows)}
        try:
            trial_result = check_feasibility(prob_info, trial)
        except Exception:
            continue
        if not trial_result.get("feasible"):
            continue
        trial_rank = (float(trial_result.get("obj1", float("inf"))), float(trial_result.get("objective", float("inf"))))
        if trial_rank >= current_rank:
            continue
        current_rows = trial_rows
        current_rank = trial_rank
        used_blocks.add(block_id)
        accepted.append(trial)
        if len(accepted) >= max_accepts or current_rank[0] <= 0.0:
            break
    return accepted

def _best_checked_cpp_candidate(prob_info: dict, candidates: list[dict]) -> dict | None:
    if not candidates:
        return None
    best_solution = None
    best_rank = None
    candidates_to_check = _limited_cpp_check_candidates(prob_info, candidates)
    try:
        from utils import check_feasibility

        for candidate in candidates_to_check:
            result = check_feasibility(prob_info, candidate)
            if not result.get("feasible"):
                continue
            objective = result.get("objective")
            obj1 = result.get("obj1")
            if objective is None or obj1 is None:
                continue
            rank = (float(obj1), float(objective))
            if best_rank is None or rank < best_rank:
                best_solution = candidate
                best_rank = rank
    except Exception:
        return None
    return best_solution


def _top_checked_cpp_candidates(prob_info: dict, candidates: list[dict], limit: int) -> list[dict]:
    if not candidates or limit <= 0:
        return []
    candidates_to_check = _limited_cpp_check_candidates(prob_info, candidates)
    ranked: list[tuple[tuple[float, float], dict]] = []
    try:
        from utils import check_feasibility

        for candidate in candidates_to_check:
            result = check_feasibility(prob_info, candidate)
            if not result.get("feasible"):
                continue
            objective = result.get("objective")
            obj1 = result.get("obj1")
            if objective is None or obj1 is None:
                continue
            ranked.append(((float(obj1), float(objective)), candidate))
    except Exception:
        return []
    ranked.sort(key=lambda item: item[0])
    selected: list[dict] = []
    seen_keys: set[str] = set()
    seen_obj1: set[int] = set()
    for _rank, candidate in ranked:
        obj1_bucket = int(round(_rank[0]))
        if obj1_bucket in seen_obj1:
            continue
        key = repr(candidate.get("operations", {}))
        if key in seen_keys:
            continue
        selected.append(candidate)
        seen_keys.add(key)
        seen_obj1.add(obj1_bucket)
        if len(selected) >= limit:
            break
    for _rank, candidate in ranked:
        key = repr(candidate.get("operations", {}))
        if key in seen_keys:
            continue
        selected.append(candidate)
        seen_keys.add(key)
        if len(selected) >= limit:
            break
    return selected


def _limited_cpp_check_candidates(prob_info: dict, candidates: list[dict]) -> list[dict]:
    """Keep official checker cost bounded on large short-budget C++ pools."""

    n_blocks = len(prob_info.get("blocks", []))
    if n_blocks < 200 or len(candidates) <= 160:
        return candidates

    ranked = sorted(candidates, key=lambda candidate: _proxy_solution_rank(prob_info, candidate))
    selected: list[dict] = []
    seen_keys: set[str] = set()

    def add_candidate(candidate: dict) -> None:
        key = repr(candidate.get("operations", {}))
        if key in seen_keys:
            return
        selected.append(candidate)
        seen_keys.add(key)

    for candidate in ranked[:192]:
        add_candidate(candidate)

    if len(ranked) > 192:
        stride = max(1, (len(ranked) - 192) // 32)
        for index in range(192, len(ranked), stride):
            add_candidate(ranked[index])
            if len(selected) >= 224:
                break

    return selected


def _checked_obj1(prob_info: dict, solution: dict) -> float:
    try:
        from utils import check_feasibility

        result = check_feasibility(prob_info, solution)
    except Exception:
        return float("inf")
    if not result.get("feasible") or result.get("obj1") is None:
        return float("inf")
    return float(result["obj1"])


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
    incumbent_obj1 = incumbent_result.get("obj1")
    candidate_obj1 = candidate_result.get("obj1")
    if candidate_objective is None or candidate_obj1 is None:
        return incumbent
    if incumbent_objective is None or incumbent_obj1 is None:
        return candidate
    candidate_rank = (float(candidate_obj1), float(candidate_objective))
    incumbent_rank = (float(incumbent_obj1), float(incumbent_objective))
    if candidate_rank < incumbent_rank:
        return candidate
    return incumbent
