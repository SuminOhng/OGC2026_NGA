"""Optimize entry/exit times while keeping bay, position, and orientation fixed.

This is an analysis tool.  It answers: "Given this placement, how much
tardiness remains if only the schedule is optimized?"  It is not a global lower
bound for the OGC problem, because placements are fixed to a given solution.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SUBMISSION_ROOT = ROOT / "ogc_solver"
BASELINE_ROOT = ROOT / "baseline" / "baseline"

sys.path.insert(0, str(BASELINE_ROOT))
sys.path.insert(0, str(SUBMISSION_ROOT))

from utils import check_feasibility  # noqa: E402
from ogc_solver.subsolvers.scheduling_mip import same_bay_pair_needs_time_separation  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("instance", type=Path)
    parser.add_argument("--solution", type=Path, default=None)
    parser.add_argument("--timelimit", type=float, default=5.0, help="Solver timelimit when no solution is provided")
    parser.add_argument("--cp-time-limit", type=float, default=30.0)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    with args.instance.open(encoding="utf-8") as handle:
        prob_info = json.load(handle)

    if args.solution is None:
        from myalgorithm import algorithm  # noqa: E402

        started_at = time.time()
        solution = algorithm(prob_info, args.timelimit)
        solve_elapsed = time.time() - started_at
    else:
        with args.solution.open(encoding="utf-8") as handle:
            solution = json.load(handle)
        solve_elapsed = None

    original_result = check_feasibility(prob_info, solution)
    assignments = parse_solution(solution)
    probe = optimize_fixed_schedule(
        prob_info,
        assignments,
        cp_time_limit=args.cp_time_limit,
        workers=args.workers,
    )
    probe["instance"] = prob_info.get("name", args.instance.stem)
    probe["solver_elapsed"] = None if solve_elapsed is None else round(solve_elapsed, 3)
    probe["original"] = {
        "feasible": original_result.get("feasible"),
        "stage": original_result.get("stage"),
        "objective": original_result.get("objective"),
        "obj1": original_result.get("obj1"),
        "obj2": original_result.get("obj2"),
        "obj3": original_result.get("obj3"),
    }

    candidate_solution = probe.pop("_candidate_solution", None)
    if candidate_solution is not None:
        candidate_result = check_feasibility(prob_info, candidate_solution)
        probe["candidate"] = {
            "feasible": candidate_result.get("feasible"),
            "stage": candidate_result.get("stage"),
            "objective": candidate_result.get("objective"),
            "obj1": candidate_result.get("obj1"),
            "obj2": candidate_result.get("obj2"),
            "obj3": candidate_result.get("obj3"),
        }
        if args.output is not None:
            solution_path = args.output.with_suffix(".solution.json")
            with solution_path.open("w", encoding="utf-8") as handle:
                json.dump(candidate_solution, handle)
            probe["candidate_solution_path"] = str(solution_path)

    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("w", encoding="utf-8") as handle:
            json.dump(probe, handle, indent=2)
    print(json.dumps(probe, indent=2))


def parse_solution(solution: dict[str, Any]) -> dict[int, dict[str, Any]]:
    assignments: dict[int, dict[str, Any]] = {}
    for time_key, ops_at_time in solution.get("operations", {}).items():
        time_value = int(time_key)
        for seq, op in enumerate(ops_at_time):
            block_id = int(op["block_id"])
            if op["type"] == "ENTRY":
                assignments[block_id] = {
                    "block_id": block_id,
                    "bay_id": int(op["bay_id"]),
                    "x": int(op["x"]),
                    "y": int(op["y"]),
                    "orient_idx": int(op["orient_idx"]),
                    "entry_time": time_value,
                    "exit_time": None,
                    "_entry_seq": seq,
                }
            elif op["type"] == "EXIT":
                assignments.setdefault(block_id, {"block_id": block_id})
                assignments[block_id]["exit_time"] = time_value
                assignments[block_id]["_exit_seq"] = seq
    return {
        block_id: row
        for block_id, row in assignments.items()
        if row.get("exit_time") is not None and {"bay_id", "x", "y", "orient_idx", "entry_time"}.issubset(row)
    }


def optimize_fixed_schedule(
    prob_info: dict[str, Any],
    assignments: dict[int, dict[str, Any]],
    *,
    cp_time_limit: float,
    workers: int,
) -> dict[str, Any]:
    try:
        from ortools.sat.python import cp_model
    except Exception as exc:  # pragma: no cover - environment-specific
        return {"available": False, "error": f"ortools import failed: {exc}"}

    blocks = prob_info.get("blocks", [])
    if len(assignments) != len(blocks):
        return {"available": False, "error": "incomplete assignments"}

    model = cp_model.CpModel()
    horizon = _safe_horizon(blocks, assignments)
    starts = {}
    ends = {}
    tardiness_vars = {}
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

    conflict_pairs = 0
    block_ids = sorted(assignments)
    for left_pos, left_id in enumerate(block_ids):
        for right_id in block_ids[left_pos + 1 :]:
            if int(assignments[left_id]["bay_id"]) != int(assignments[right_id]["bay_id"]):
                continue
            if not same_bay_pair_needs_time_separation(prob_info, assignments, left_id, right_id):
                continue
            conflict_pairs += 1
            left_before = model.NewBoolVar(f"before_{left_id}_{right_id}")
            model.Add(ends[left_id] <= starts[right_id]).OnlyEnforceIf(left_before)
            model.Add(ends[right_id] <= starts[left_id]).OnlyEnforceIf(left_before.Not())

    model.Minimize(sum(tardiness_vars.values()) * 1000 + sum(starts.values()))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = max(0.1, float(cp_time_limit))
    solver.parameters.num_search_workers = max(1, int(workers))
    status = solver.Solve(model)
    status_name = solver.StatusName(status)

    result = {
        "available": True,
        "status": status_name,
        "cp_time_limit": float(cp_time_limit),
        "workers": int(workers),
        "horizon": horizon,
        "conflict_pairs": conflict_pairs,
        "best_obj1_bound": float(solver.BestObjectiveBound()) / 1000.0,
        "branches": solver.NumBranches(),
        "conflicts": solver.NumConflicts(),
        "wall_time": solver.WallTime(),
    }
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return result

    optimized = {}
    for block_id, row in assignments.items():
        optimized[block_id] = dict(row)
        optimized[block_id]["entry_time"] = int(solver.Value(starts[block_id]))
        optimized[block_id]["exit_time"] = int(solver.Value(ends[block_id]))
    result["best_obj1"] = sum(
        max(0, optimized[block_id]["exit_time"] - int(blocks[block_id]["due_date"]))
        for block_id in optimized
    )
    result["_candidate_solution"] = build_solution(optimized)
    return result


def _safe_horizon(blocks: list[dict[str, Any]], assignments: dict[int, dict[str, Any]]) -> int:
    latest_old_exit = max(int(row["exit_time"]) for row in assignments.values())
    latest_due = max(int(block["due_date"]) for block in blocks)
    latest_release = max(int(block["release_time"]) for block in blocks)
    total_processing = sum(int(block["processing_time"]) for block in blocks)
    return max(latest_old_exit, latest_due, latest_release + total_processing) + max(20, total_processing // 4)


def build_solution(assignments: dict[int, dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    events: dict[int, list[dict[str, Any]]] = {}
    for block_id, row in assignments.items():
        entry_time = int(row["entry_time"])
        exit_time = int(row["exit_time"])
        bay_id = int(row["bay_id"])
        events.setdefault(exit_time, []).append(
            {"type": "EXIT", "block_id": int(block_id), "bay_id": bay_id}
        )
        events.setdefault(entry_time, []).append(
            {
                "type": "ENTRY",
                "block_id": int(block_id),
                "bay_id": bay_id,
                "x": int(row["x"]),
                "y": int(row["y"]),
                "orient_idx": int(row["orient_idx"]),
            }
        )
    return {
        "operations": {
            str(time_idx): sorted(
                events[time_idx],
                key=lambda op: (0 if op["type"] == "EXIT" else 1, op["block_id"]),
            )
            for time_idx in sorted(events)
        }
    }


if __name__ == "__main__":
    main()
