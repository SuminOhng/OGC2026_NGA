"""Optional fixed-placement scheduling polish using OR-Tools CP-SAT."""

from __future__ import annotations

import time
from typing import Any

from .scheduling_mip import same_bay_pair_needs_time_separation


def cp_sat_available() -> bool:
    try:
        from ortools.sat.python import cp_model  # noqa: F401
    except Exception:
        return False
    return True


def reschedule_fixed_placements_cp_sat(
    prob_info: dict,
    solution: dict,
    deadline: float,
    *,
    min_seconds: float = 6.0,
) -> dict | None:
    """Return a fixed-placement reschedule candidate, or None on failure."""

    remaining = deadline - time.monotonic()
    if remaining < min_seconds:
        return None
    try:
        from ortools.sat.python import cp_model
    except Exception:
        return None

    assignments = _assignments_from_solution(solution)
    blocks = prob_info.get("blocks", [])
    if len(assignments) != len(blocks):
        return None

    model = cp_model.CpModel()
    horizon = _safe_horizon(blocks, assignments)
    starts: dict[int, Any] = {}
    ends: dict[int, Any] = {}
    tardiness_vars: dict[int, Any] = {}
    for block_id, block in enumerate(blocks):
        release = int(block["release_time"])
        processing = int(block["processing_time"])
        due = int(block["due_date"])
        start = model.NewIntVar(release, horizon, f"s_{block_id}")
        end = model.NewIntVar(release + processing, horizon + processing, f"e_{block_id}")
        tardiness = model.NewIntVar(0, horizon + processing, f"t_{block_id}")
        model.Add(end == start + processing)
        model.Add(tardiness >= end - due)
        starts[block_id] = start
        ends[block_id] = end
        tardiness_vars[block_id] = tardiness

    block_ids = sorted(assignments)
    for left_pos, left_id in enumerate(block_ids):
        for right_id in block_ids[left_pos + 1 :]:
            if time.monotonic() >= deadline - 1.0:
                return None
            if int(assignments[left_id]["bay_id"]) != int(assignments[right_id]["bay_id"]):
                continue
            if not same_bay_pair_needs_time_separation(prob_info, assignments, left_id, right_id):
                continue
            left_before = model.NewBoolVar(f"before_{left_id}_{right_id}")
            model.Add(ends[left_id] <= starts[right_id]).OnlyEnforceIf(left_before)
            model.Add(ends[right_id] <= starts[left_id]).OnlyEnforceIf(left_before.Not())

    model.Minimize(sum(tardiness_vars.values()) * 1000 + sum(starts.values()))
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = max(0.5, deadline - time.monotonic() - 0.5)
    solver.parameters.num_search_workers = 8
    status = solver.Solve(model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return None

    updated = {block_id: dict(row) for block_id, row in assignments.items()}
    for block_id in updated:
        updated[block_id]["entry_time"] = int(solver.Value(starts[block_id]))
        updated[block_id]["exit_time"] = int(solver.Value(ends[block_id]))
    return {"operations": _build_operations(updated.values())}


def _safe_horizon(blocks: list[dict], assignments: dict[int, dict]) -> int:
    latest_old_exit = max(int(row["exit_time"]) for row in assignments.values())
    latest_due = max(int(block["due_date"]) for block in blocks)
    latest_release = max(int(block["release_time"]) for block in blocks)
    total_processing = sum(int(block["processing_time"]) for block in blocks)
    return max(latest_old_exit, latest_due, latest_release + total_processing) + max(20, total_processing // 4)


def _assignments_from_solution(solution: dict) -> dict[int, dict]:
    assignments: dict[int, dict] = {}
    for time_key, operations in sorted(solution.get("operations", {}).items(), key=lambda item: int(item[0])):
        time_idx = int(time_key)
        for seq, op in enumerate(operations):
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
                        "_seq": seq,
                    }
                )
            elif op["type"] == "EXIT":
                assignments[block_id].update({"bay_id": int(op["bay_id"]), "exit_time": time_idx})
    return {bid: data for bid, data in assignments.items() if "entry_time" in data and "exit_time" in data}


def _build_operations(assignments) -> dict:
    events: dict[int, list[dict]] = {}
    for row in assignments:
        block_id = int(row["block_id"])
        bay_id = int(row["bay_id"])
        events.setdefault(int(row["exit_time"]), []).append(
            {"type": "EXIT", "block_id": block_id, "bay_id": bay_id}
        )
        events.setdefault(int(row["entry_time"]), []).append(
            {
                "type": "ENTRY",
                "block_id": block_id,
                "bay_id": bay_id,
                "x": int(row["x"]),
                "y": int(row["y"]),
                "orient_idx": int(row["orient_idx"]),
            }
        )
    return {
        str(time_idx): sorted(events[time_idx], key=lambda op: (0 if op["type"] == "EXIT" else 1, op["block_id"]))
        for time_idx in sorted(events)
    }
