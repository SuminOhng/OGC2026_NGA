"""Fast feasibility stabilization for near-feasible greedy solutions."""

from __future__ import annotations

import re
import time


_BLOCK_RE = re.compile(r"block\s+(\d+)")


def stabilize_solution(
    prob_info: dict,
    solution: dict,
    max_passes: int = 6,
    deadline: float | None = None,
) -> dict:
    """Repair a near-feasible solution by moving violating blocks to empty windows.

    This is intentionally conservative. It preserves each block's bay,
    orientation, and position, and only delays violating blocks until their bay
    is empty for the full processing interval. That sacrifices some tardiness
    but quickly converts many Stage 2/3/4 failures into feasible solutions.
    """

    from utils import check_feasibility

    assignments = _assignments_from_solution(solution)
    n_bays = len(prob_info["bays"])
    blocks_data = prob_info["blocks"]

    for _ in range(max_passes):
        if deadline is not None and time.monotonic() >= deadline - 0.05:
            return {"operations": _build_operations(assignments.values())}

        candidate = {"operations": _build_operations(assignments.values())}
        result = check_feasibility(prob_info, candidate)
        if result.get("feasible"):
            return candidate

        to_repair = _violation_block_ids(result.get("violations", []))
        if not to_repair:
            return candidate

        bay_schedule = _build_bay_schedule(assignments.values(), n_bays)
        for block_id in sorted(
            to_repair,
            key=lambda bid: (
                blocks_data[bid]["due_date"],
                blocks_data[bid]["processing_time"],
                bid,
            ),
        ):
            if deadline is not None and time.monotonic() >= deadline - 0.05:
                return {"operations": _build_operations(assignments.values())}
            if block_id not in assignments:
                continue
            current = assignments[block_id]
            bay_id = current["bay_id"]
            old_slot = (current["entry_time"], current["exit_time"])
            if old_slot in bay_schedule[bay_id]:
                bay_schedule[bay_id].remove(old_slot)

            processing_time = int(blocks_data[block_id]["processing_time"])
            entry_time = _empty_bay_entry(
                bay_schedule[bay_id],
                int(blocks_data[block_id]["release_time"]),
                processing_time,
            )
            exit_time = entry_time + processing_time
            assignments[block_id] = {
                **current,
                "entry_time": int(entry_time),
                "exit_time": int(exit_time),
            }
            bay_schedule[bay_id].append((entry_time, exit_time))

    return {"operations": _build_operations(assignments.values())}


def _assignments_from_solution(solution: dict) -> dict[int, dict]:
    assignments: dict[int, dict] = {}
    entry_seq = 0
    for time_key, operations in sorted(solution.get("operations", {}).items(), key=lambda item: int(item[0])):
        time_idx = int(time_key)
        for op in operations:
            block_id = int(op["block_id"])
            if op["type"] == "ENTRY":
                assignments.setdefault(block_id, {})
                assignments[block_id].update(
                    {
                        "block_id": block_id,
                        "bay_id": int(op["bay_id"]),
                        "x": int(round(op.get("x", 0))),
                        "y": int(round(op.get("y", 0))),
                        "orient_idx": int(op.get("orient_idx", 0)),
                        "entry_time": time_idx,
                        "_seq": entry_seq,
                    }
                )
                entry_seq += 1
            elif op["type"] == "EXIT":
                assignments.setdefault(block_id, {"block_id": block_id})
                assignments[block_id].update(
                    {
                        "bay_id": int(op["bay_id"]),
                        "exit_time": time_idx,
                    }
                )
    return assignments


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


def _build_bay_schedule(assignments, n_bays: int) -> list[list[tuple[int, int]]]:
    schedules = [[] for _ in range(n_bays)]
    for assignment in assignments:
        if "bay_id" not in assignment:
            continue
        if "entry_time" not in assignment or "exit_time" not in assignment:
            continue
        schedules[assignment["bay_id"]].append(
            (int(assignment["entry_time"]), int(assignment["exit_time"]))
        )
    return schedules


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


def _build_operations(assignments) -> dict:
    buckets: dict[int, list[tuple[int, int, int, dict]]] = {}
    for assignment in assignments:
        required = {"block_id", "bay_id", "x", "y", "orient_idx", "entry_time", "exit_time"}
        if not required.issubset(assignment):
            continue
        block_id = int(assignment["block_id"])
        bay_id = int(assignment["bay_id"])
        entry_time = int(assignment["entry_time"])
        exit_time = int(assignment["exit_time"])
        buckets.setdefault(exit_time, []).append(
            (0, 0, block_id, {"type": "EXIT", "block_id": block_id, "bay_id": bay_id})
        )
        buckets.setdefault(entry_time, []).append(
            (
                1,
                int(assignment.get("_seq", block_id)),
                block_id,
                {
                    "type": "ENTRY",
                    "block_id": block_id,
                    "bay_id": bay_id,
                    "x": int(assignment["x"]),
                    "y": int(assignment["y"]),
                    "orient_idx": int(assignment["orient_idx"]),
                },
            )
        )

    operations = {}
    for time_idx in sorted(buckets):
        operations[str(time_idx)] = [item[3] for item in sorted(buckets[time_idx])]
    return operations
