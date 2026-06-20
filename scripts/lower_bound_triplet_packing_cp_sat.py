"""Compute a triplet-packing relaxed CP-SAT lower bound for OGC obj1.

This is an analysis tool, not submitted solver logic. It keeps the usual
relaxed bay-area cumulative model and adds only certified triplet constraints:

If three blocks assigned to the same bay have no collision-free integer
placement triple in that bay, then their processing intervals cannot share a
common time point in that bay. Triplets that are too expensive to certify are
left relaxed, so the resulting objective bound remains conservative.
"""

from __future__ import annotations

import argparse
import itertools
import json
import math
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SUBMISSION_ROOT = ROOT / "ogc_solver"
BASELINE_ROOT = ROOT / "baseline" / "baseline"

sys.path.insert(0, str(BASELINE_ROOT))
sys.path.insert(0, str(SUBMISSION_ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from batch_eval import objective_lower_bound  # noqa: E402
from lower_bound_pair_relation_cp_sat import (  # noqa: E402
    _PlacementOption,
    _precompute_geometry_options,
    _safe_horizon,
    _scaled_min_area_for_bay,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("instance", type=Path)
    parser.add_argument("--time-limit", type=float, default=30.0)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--area-scale", type=int, default=100)
    parser.add_argument("--max-triplet-checks", type=int, default=50_000)
    parser.add_argument("--max-triplets", type=int, default=2_000)
    parser.add_argument(
        "--candidate-order",
        choices=["area-desc", "product-asc"],
        default="area-desc",
        help="Which certified triplet candidates to inspect first.",
    )
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    with args.instance.open(encoding="utf-8") as handle:
        prob_info = json.load(handle)

    result = triplet_packing_tardiness_lb(
        prob_info,
        time_limit=args.time_limit,
        workers=args.workers,
        area_scale=args.area_scale,
        max_triplet_checks=args.max_triplet_checks,
        max_triplets=args.max_triplets,
        candidate_order=args.candidate_order,
    )
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("w", encoding="utf-8") as handle:
            json.dump(result, handle, indent=2)
    print(json.dumps(result, indent=2))


def triplet_packing_tardiness_lb(
    prob_info: dict[str, Any],
    *,
    time_limit: float,
    workers: int,
    area_scale: int,
    max_triplet_checks: int,
    max_triplets: int,
    candidate_order: str,
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

    geometry = _precompute_geometry_options(prob_info)
    triplets, triplet_stats = _certified_incompatible_triplets(
        prob_info,
        geometry,
        max_triplet_checks=max_triplet_checks,
        max_triplets=max_triplets,
        candidate_order=candidate_order,
    )

    model = cp_model.CpModel()
    horizon = _safe_horizon(blocks)
    bay_capacities = [
        max(1, math.ceil(float(bay["width"]) * float(bay["height"]) * area_scale))
        for bay in bays
    ]

    starts: dict[int, Any] = {}
    ends: dict[int, Any] = {}
    presences: dict[tuple[int, int], Any] = {}
    tardiness_vars = []
    optional_intervals_by_bay: list[list[Any]] = [[] for _ in bays]
    demands_by_bay: list[list[int]] = [[] for _ in bays]

    for block_id, block in enumerate(blocks):
        release = int(block.get("release_time", 0))
        processing = int(block.get("processing_time", 0))
        due = int(block.get("due_date", 0))
        start = model.NewIntVar(release, horizon, f"s_{block_id}")
        end = model.NewIntVar(release + processing, horizon + processing, f"e_{block_id}")
        model.Add(end == start + processing)
        tardiness = model.NewIntVar(0, horizon + processing, f"tard_{block_id}")
        model.Add(tardiness >= end - due)
        starts[block_id] = start
        ends[block_id] = end
        tardiness_vars.append(tardiness)

        compatible_bays = [
            bay_id
            for bay_id in range(len(bays))
            if geometry.get((block_id, bay_id))
        ]
        if not compatible_bays:
            compatible_bays = list(range(len(bays)))

        block_presences = []
        for bay_id in compatible_bays:
            presence = model.NewBoolVar(f"x_{block_id}_{bay_id}")
            interval = model.NewOptionalIntervalVar(
                start,
                processing,
                end,
                presence,
                f"int_{block_id}_{bay_id}",
            )
            optional_intervals_by_bay[bay_id].append(interval)
            demands_by_bay[bay_id].append(
                _scaled_min_area_for_bay(block, geometry.get((block_id, bay_id), []), area_scale)
            )
            presences[(block_id, bay_id)] = presence
            block_presences.append(presence)
        model.AddExactlyOne(block_presences)

    for bay_id, intervals in enumerate(optional_intervals_by_bay):
        if intervals:
            model.AddCumulative(intervals, demands_by_bay[bay_id], bay_capacities[bay_id])

    triplet_constraints = 0
    for bay_id, first_id, second_id, third_id in triplets:
        triplet_presence = [
            presences.get((first_id, bay_id)),
            presences.get((second_id, bay_id)),
            presences.get((third_id, bay_id)),
        ]
        if any(presence is None for presence in triplet_presence):
            continue
        block_ids = [first_id, second_id, third_id]
        separation_literals = []
        for left_id, right_id in itertools.permutations(block_ids, 2):
            lit = model.NewBoolVar(f"sep_{left_id}_{right_id}_b{bay_id}")
            model.Add(ends[left_id] <= starts[right_id]).OnlyEnforceIf([*triplet_presence, lit])
            separation_literals.append(lit)
        model.Add(sum(separation_literals) >= 1).OnlyEnforceIf(triplet_presence)
        triplet_constraints += 1

    model.Minimize(sum(tardiness_vars))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = max(0.1, float(time_limit))
    solver.parameters.num_search_workers = max(1, int(workers))
    status = solver.Solve(model)

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
        "status": solver.StatusName(status),
        "time_limit": float(time_limit),
        "workers": int(workers),
        "area_scale": int(area_scale),
        "max_triplet_checks": int(max_triplet_checks),
        "max_triplets": int(max_triplets),
        "candidate_order": candidate_order,
        "horizon": horizon,
        "base_obj1_lb": base_lb["lower_bound_obj1"],
        "base_weighted_lb": base_lb["lower_bound"],
        "secondary_weighted_lb": base_lb["lower_bound_secondary"],
        "triplet_packing_obj1_bound": cp_bound,
        "triplet_packing_obj1_incumbent": cp_incumbent,
        "combined_obj1_lb": combined_obj1_lb,
        "combined_weighted_lb": combined_weighted_lb,
        "certified_incompatible_triplets": len(triplets),
        "triplet_constraints": triplet_constraints,
        **triplet_stats,
        "branches": solver.NumBranches(),
        "conflicts": solver.NumConflicts(),
        "wall_time": solver.WallTime(),
    }


def _certified_incompatible_triplets(
    prob_info: dict[str, Any],
    geometry: dict[tuple[int, int], list[_PlacementOption]],
    *,
    max_triplet_checks: int,
    max_triplets: int,
    candidate_order: str,
) -> tuple[list[tuple[int, int, int, int]], dict[str, int]]:
    candidates = _triplet_candidates(prob_info, geometry, candidate_order=candidate_order)
    incompatible = []
    checked = 0
    unknown = 0
    feasible = 0
    for _score, bay_id, first_id, second_id, third_id in candidates:
        if checked >= max_triplets:
            break
        checked += 1
        can_coexist, complete = _triplet_can_coexist(
            prob_info,
            bay_id,
            geometry[(first_id, bay_id)],
            geometry[(second_id, bay_id)],
            geometry[(third_id, bay_id)],
            max_triplet_checks=max_triplet_checks,
        )
        if can_coexist:
            feasible += 1
        elif complete:
            incompatible.append((bay_id, first_id, second_id, third_id))
        else:
            unknown += 1
    return incompatible, {
        "candidate_triplets": len(candidates),
        "checked_triplets": checked,
        "unknown_triplets": unknown,
        "coexistence_feasible_triplets": feasible,
    }


def _triplet_candidates(
    prob_info: dict[str, Any],
    geometry: dict[tuple[int, int], list[_PlacementOption]],
    *,
    candidate_order: str,
) -> list[tuple[tuple[float, int], int, int, int, int]]:
    blocks = prob_info.get("blocks", [])
    bays = prob_info.get("bays", [])
    min_areas = {
        key: _min_base_area(options)
        for key, options in geometry.items()
    }
    rows = []
    for bay_id in range(len(bays)):
        bay_block_ids = [block_id for block_id in range(len(blocks)) if (block_id, bay_id) in geometry]
        for first_id, second_id, third_id in itertools.combinations(bay_block_ids, 3):
            if not _on_time_intervals_can_common_overlap(
                blocks[first_id],
                blocks[second_id],
                blocks[third_id],
            ):
                continue
            option_product = (
                len(geometry[(first_id, bay_id)])
                * len(geometry[(second_id, bay_id)])
                * len(geometry[(third_id, bay_id)])
            )
            area_sum = (
                min_areas[(first_id, bay_id)]
                + min_areas[(second_id, bay_id)]
                + min_areas[(third_id, bay_id)]
            )
            if candidate_order == "product-asc":
                score = (float(option_product), -option_product)
            else:
                score = (-area_sum, option_product)
            rows.append((score, bay_id, first_id, second_id, third_id))
    rows.sort()
    return rows


def _min_base_area(options: list[_PlacementOption]) -> float:
    if not options:
        return 0.0
    areas = []
    for option in options:
        layer = option.block.block_data["shape"][option.orient_idx].get("layers", [[]])[0]
        areas.append(_polygon_area(layer))
    return min(areas) if areas else 0.0


def _polygon_area(points: list) -> float:
    if len(points) < 3:
        return 0.0
    area = 0.0
    previous_x = float(points[-1][0])
    previous_y = float(points[-1][1])
    for point in points:
        x = float(point[0])
        y = float(point[1])
        area += previous_x * y - x * previous_y
        previous_x = x
        previous_y = y
    return abs(area) * 0.5


def _on_time_intervals_can_common_overlap(
    first: dict[str, Any],
    second: dict[str, Any],
    third: dict[str, Any],
) -> bool:
    latest_release = max(
        int(first.get("release_time", 0)),
        int(second.get("release_time", 0)),
        int(third.get("release_time", 0)),
    )
    earliest_due = min(
        int(first.get("due_date", 0)),
        int(second.get("due_date", 0)),
        int(third.get("due_date", 0)),
    )
    return latest_release < earliest_due


def _triplet_can_coexist(
    prob_info: dict[str, Any],
    bay_id: int,
    first_options: list[_PlacementOption],
    second_options: list[_PlacementOption],
    third_options: list[_PlacementOption],
    *,
    max_triplet_checks: int,
) -> tuple[bool, bool]:
    from utils import Bay, check_collisions

    bay = Bay.from_dict(prob_info["bays"][bay_id], bay_id)
    checks = 0
    for first in first_options:
        for second in second_options:
            if check_collisions(bay, [first.block, second.block]):
                continue
            for third in third_options:
                checks += 1
                if checks > max_triplet_checks:
                    return False, False
                if not check_collisions(bay, [first.block, third.block]) and not check_collisions(
                    bay,
                    [second.block, third.block],
                ):
                    return True, True
    return False, True


if __name__ == "__main__":
    main()
