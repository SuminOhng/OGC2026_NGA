"""Compute relaxed CP-SAT lower bounds for OGC training instances.

This is an analysis tool, not submitted solver logic.  It relaxes irregular
geometry and crane paths to a bay-level cumulative area resource, then minimizes
total tardiness.  Because constraints are relaxed, the solver's objective bound
is a conservative lower bound for the true OGC `obj1`.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from batch_eval import _base_layer_area, _bbox_fit_bays, _orientation_size, objective_lower_bound  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("instance", type=Path)
    parser.add_argument("--time-limit", type=float, default=30.0)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--area-scale", type=int, default=100)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    with args.instance.open(encoding="utf-8") as handle:
        prob_info = json.load(handle)

    result = cumulative_area_tardiness_lb(
        prob_info,
        time_limit=args.time_limit,
        workers=args.workers,
        area_scale=args.area_scale,
    )
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("w", encoding="utf-8") as handle:
            json.dump(result, handle, indent=2)
    print(json.dumps(result, indent=2))


def cumulative_area_tardiness_lb(
    prob_info: dict[str, Any],
    *,
    time_limit: float,
    workers: int,
    area_scale: int,
) -> dict[str, Any]:
    try:
        from ortools.sat.python import cp_model
    except Exception as exc:  # pragma: no cover - environment-specific
        return {
            "instance": prob_info.get("name"),
            "available": False,
            "error": f"ortools import failed: {exc}",
        }

    blocks = prob_info.get("blocks", [])
    bays = prob_info.get("bays", [])
    if not blocks or not bays:
        return {"instance": prob_info.get("name"), "available": False, "error": "empty instance"}

    model = cp_model.CpModel()
    horizon = _safe_horizon(blocks)
    bay_capacities = [
        max(1, math.ceil(float(bay["width"]) * float(bay["height"]) * area_scale))
        for bay in bays
    ]

    tardiness_vars = []
    optional_intervals_by_bay: list[list[Any]] = [[] for _ in bays]
    demands_by_bay: list[list[int]] = [[] for _ in bays]

    for block_id, block in enumerate(blocks):
        release = int(block.get("release_time", 0))
        processing = int(block.get("processing_time", 0))
        due = int(block.get("due_date", 0))
        compatible_bays = _bbox_fit_bays(block, bays)
        if not compatible_bays:
            compatible_bays = list(range(len(bays)))

        start = model.NewIntVar(release, horizon, f"s_{block_id}")
        end = model.NewIntVar(release + processing, horizon + processing, f"e_{block_id}")
        model.Add(end == start + processing)
        tardiness = model.NewIntVar(0, horizon + processing, f"tard_{block_id}")
        model.Add(tardiness >= end - due)
        tardiness_vars.append(tardiness)

        presences = []
        for bay_id in compatible_bays:
            presence = model.NewBoolVar(f"x_{block_id}_{bay_id}")
            interval = model.NewOptionalIntervalVar(
                start,
                processing,
                end,
                presence,
                f"int_{block_id}_{bay_id}",
            )
            demand = _scaled_min_area_for_bay(block, bays[bay_id], area_scale)
            optional_intervals_by_bay[bay_id].append(interval)
            demands_by_bay[bay_id].append(demand)
            presences.append(presence)
        model.AddExactlyOne(presences)

    for bay_id, intervals in enumerate(optional_intervals_by_bay):
        if intervals:
            model.AddCumulative(intervals, demands_by_bay[bay_id], bay_capacities[bay_id])

    model.Minimize(sum(tardiness_vars))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = max(0.1, float(time_limit))
    solver.parameters.num_search_workers = max(1, int(workers))
    status = solver.Solve(model)
    status_name = solver.StatusName(status)

    base_lb = objective_lower_bound(prob_info)
    cp_bound = max(0.0, float(solver.BestObjectiveBound()))
    cp_incumbent = None
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        cp_incumbent = float(solver.ObjectiveValue())

    weights = prob_info.get("weights", {})
    w1 = float(weights.get("w1", 1.0))
    combined_obj1_lb = max(float(base_lb["lower_bound_obj1"]), cp_bound)
    combined_weighted_lb = w1 * combined_obj1_lb + float(base_lb["lower_bound_secondary"])

    return {
        "instance": prob_info.get("name"),
        "available": True,
        "status": status_name,
        "time_limit": float(time_limit),
        "workers": int(workers),
        "area_scale": int(area_scale),
        "horizon": horizon,
        "base_obj1_lb": base_lb["lower_bound_obj1"],
        "base_weighted_lb": base_lb["lower_bound"],
        "secondary_weighted_lb": base_lb["lower_bound_secondary"],
        "cp_obj1_bound": cp_bound,
        "cp_obj1_incumbent": cp_incumbent,
        "combined_obj1_lb": combined_obj1_lb,
        "combined_weighted_lb": combined_weighted_lb,
        "branches": solver.NumBranches(),
        "conflicts": solver.NumConflicts(),
        "wall_time": solver.WallTime(),
    }


def _safe_horizon(blocks: list[dict[str, Any]]) -> int:
    max_release = max(int(block.get("release_time", 0)) for block in blocks)
    max_due = max(int(block.get("due_date", 0)) for block in blocks)
    total_processing = sum(int(block.get("processing_time", 0)) for block in blocks)
    return max(max_due, max_release) + total_processing


def _scaled_min_area_for_bay(block: dict[str, Any], bay: dict[str, Any], area_scale: int) -> int:
    bay_width = float(bay.get("width", 0.0))
    bay_height = float(bay.get("height", 0.0))
    candidate_areas = []
    for orientation in block.get("shape", []):
        width, height = _orientation_size(orientation)
        if width <= bay_width and height <= bay_height:
            candidate_areas.append(max(0.0, _base_layer_area(orientation)))
    if not candidate_areas:
        return 1
    # Floor the demand to keep the relaxation conservative.
    return max(1, math.floor(min(candidate_areas) * area_scale))


if __name__ == "__main__":
    main()
