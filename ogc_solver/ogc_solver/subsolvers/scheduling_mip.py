"""Fixed-placement scheduling MIP subsolver.

This subsolver only changes entry/exit times. Bay assignment, x/y position, and
orientation are fixed. It is intentionally conservative: if a per-bay MIP fails,
the caller receives ``None`` and can keep the original solution.
"""

from __future__ import annotations

import time

from ..state import Placement


def reschedule_fixed_placements(
    prob_info: dict,
    solution: dict,
    deadline: float,
    *,
    integer_start: bool = False,
) -> dict | None:
    """Return a solution with MIP-optimized per-bay schedules, or None on failure."""

    if time.monotonic() >= deadline - 0.5:
        return None
    try:
        import gurobipy as gp
    except Exception as exc:
        print(f"[SchedulingMIP] Gurobi import failed: {exc}", flush=True)
        return None

    assignments = _assignments_from_solution(solution)
    if len(assignments) != len(prob_info.get("blocks", [])):
        return None

    by_bay: list[list[int]] = [[] for _ in prob_info["bays"]]
    for block_id, row in assignments.items():
        bay_id = int(row["bay_id"])
        if bay_id < 0 or bay_id >= len(by_bay):
            return None
        by_bay[bay_id].append(int(block_id))

    updated = {block_id: dict(row) for block_id, row in assignments.items()}
    remaining = deadline - time.monotonic()
    if remaining < 2.0:
        print("[SchedulingMIP] skipped: not enough seed budget", flush=True)
        return None
    for bay_id, block_ids in enumerate(by_bay):
        if not block_ids:
            continue
        bay_deadline = min(deadline, time.monotonic() + max(0.5, (deadline - time.monotonic()) / max(1, len(by_bay))))
        schedule = _solve_one_bay_schedule(
            prob_info,
            assignments,
            bay_id,
            block_ids,
            gp,
            bay_deadline,
            integer_start=integer_start,
        )
        if schedule is None:
            return None
        for block_id, (entry_time, exit_time) in schedule.items():
            updated[block_id]["entry_time"] = int(entry_time)
            updated[block_id]["exit_time"] = int(exit_time)

    return {"operations": _build_operations(updated.values())}


def _solve_one_bay_schedule(
    prob_info: dict,
    assignments: dict[int, dict],
    bay_id: int,
    block_ids: list[int],
    gp,
    deadline: float,
    *,
    integer_start: bool,
) -> dict[int, tuple[int, int]] | None:
    if time.monotonic() >= deadline - 0.2:
        return None

    blocks = prob_info["blocks"]
    ordered = sorted(
        block_ids,
        key=lambda block_id: (
            int(blocks[block_id]["due_date"]),
            int(blocks[block_id]["release_time"]),
            int(blocks[block_id]["processing_time"]),
            block_id,
        ),
    )
    try:
        model = gp.Model(f"ogc_schedule_bay_{bay_id}")
        model.Params.OutputFlag = 1
        model.Params.TimeLimit = max(1.0, deadline - time.monotonic())
        model.Params.MIPGap = 0.15

        horizon = _bay_horizon(blocks, block_ids, assignments)
        start_vtype = gp.GRB.INTEGER if integer_start else gp.GRB.CONTINUOUS
        start = {
            block_id: model.addVar(
                lb=float(blocks[block_id]["release_time"]),
                ub=float(max(int(blocks[block_id]["release_time"]), horizon - int(blocks[block_id]["processing_time"]))),
                vtype=start_vtype,
                name=f"s_{block_id}",
            )
            for block_id in ordered
        }
        tardiness = {block_id: model.addVar(lb=0.0, name=f"T_{block_id}") for block_id in ordered}
        for block_id in ordered:
            proc = int(blocks[block_id]["processing_time"])
            due = int(blocks[block_id]["due_date"])
            model.addConstr(tardiness[block_id] >= start[block_id] + proc - due)

        conflict_pairs = 0
        big_m = float(horizon + max(int(blocks[block_id]["processing_time"]) for block_id in ordered) + 1)
        for left_pos, left_id in enumerate(ordered):
            left_proc = int(blocks[left_id]["processing_time"])
            for right_id in ordered[left_pos + 1 :]:
                right_proc = int(blocks[right_id]["processing_time"])
                if not _same_bay_pair_needs_time_separation(prob_info, assignments, left_id, right_id):
                    continue
                conflict_pairs += 1
                left_before = model.addVar(vtype=gp.GRB.BINARY, name=f"before_{left_id}_{right_id}")
                model.addConstr(start[right_id] >= start[left_id] + left_proc - big_m * (1 - left_before))
                model.addConstr(start[left_id] >= start[right_id] + right_proc - big_m * left_before)

        objective = gp.quicksum(tardiness[block_id] for block_id in ordered) + 1e-4 * gp.quicksum(
            start[block_id] for block_id in ordered
        )
        model.setObjective(objective, gp.GRB.MINIMIZE)
        model.optimize()
        if model.SolCount <= 0:
            return None
        print(
            f"[SchedulingMIP] bay {bay_id}: blocks={len(ordered)} conflict_pairs={conflict_pairs} "
            f"integer_start={integer_start}",
            flush=True,
        )
        return {
            block_id: (
                _integerized_start(float(start[block_id].X), integer_start=integer_start),
                _integerized_start(float(start[block_id].X), integer_start=integer_start)
                + int(blocks[block_id]["processing_time"]),
            )
            for block_id in ordered
        }
    except Exception as exc:
        print(f"[SchedulingMIP] bay {bay_id} failed: {exc}", flush=True)
        return None


def _bay_horizon(blocks: list[dict], block_ids: list[int], assignments: dict[int, dict]) -> int:
    total_processing = sum(int(blocks[block_id]["processing_time"]) for block_id in block_ids)
    latest_due = max(int(blocks[block_id]["due_date"]) for block_id in block_ids)
    latest_old_exit = max(int(assignments[block_id]["exit_time"]) for block_id in block_ids)
    latest_release = max(int(blocks[block_id]["release_time"]) for block_id in block_ids)
    return max(latest_due, latest_old_exit, latest_release + total_processing) + max(20, total_processing // 4)


def _same_bay_pair_needs_time_separation(
    prob_info: dict,
    assignments: dict[int, dict],
    left_id: int,
    right_id: int,
) -> bool:
    from utils import Bay, Block, check_collisions, check_entry, check_exit

    left = assignments[left_id]
    right = assignments[right_id]
    if int(left["bay_id"]) != int(right["bay_id"]):
        return False
    bay_id = int(left["bay_id"])
    bay = Bay.from_dict(prob_info["bays"][bay_id], bay_id)
    left_block = Block(left_id, prob_info["blocks"][left_id], x=int(left["x"]), y=int(left["y"]), orient_idx=int(left["orient_idx"]))
    right_block = Block(right_id, prob_info["blocks"][right_id], x=int(right["x"]), y=int(right["y"]), orient_idx=int(right["orient_idx"]))
    if check_collisions(bay, [left_block, right_block]):
        return True
    if check_entry(bay, [right_block], left_block, fast=True):
        return True
    if check_entry(bay, [left_block], right_block, fast=True):
        return True
    if check_exit(bay, [left_block, right_block], left_block, fast=True):
        return True
    if check_exit(bay, [right_block, left_block], right_block, fast=True):
        return True
    return False


def same_bay_pair_needs_time_separation(
    prob_info: dict,
    assignments: dict[int, dict],
    left_id: int,
    right_id: int,
) -> bool:
    """Public wrapper used by the bay-assignment feedback loop."""

    return _same_bay_pair_needs_time_separation(prob_info, assignments, left_id, right_id)


def _integerized_start(value: float, *, integer_start: bool) -> int:
    if integer_start:
        return int(round(value))
    return int(__import__("math").ceil(value - 1e-6))


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


def _build_operations(assignments) -> dict:
    events: dict[int, list[dict]] = {}
    for row in assignments:
        placement = Placement(
            block_id=int(row["block_id"]),
            bay_id=int(row["bay_id"]),
            x=int(row["x"]),
            y=int(row["y"]),
            orient_idx=int(row["orient_idx"]),
            entry_time=int(row["entry_time"]),
            exit_time=int(row["exit_time"]),
        )
        events.setdefault(placement.exit_time, []).append(
            {"type": "EXIT", "block_id": placement.block_id, "bay_id": placement.bay_id}
        )
        events.setdefault(placement.entry_time, []).append(
            {
                "type": "ENTRY",
                "block_id": placement.block_id,
                "bay_id": placement.bay_id,
                "x": placement.x,
                "y": placement.y,
                "orient_idx": placement.orient_idx,
            }
        )
    return {
        str(time_idx): sorted(events[time_idx], key=lambda op: (0 if op["type"] == "EXIT" else 1, op["block_id"]))
        for time_idx in sorted(events)
    }
