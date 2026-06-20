"""Compute a pair-relation relaxed CP-SAT lower bound for OGC obj1.

This is an analysis tool, not submitted solver logic. It relaxes exact
multi-block placement, but keeps a safe pairwise implication:

For a block pair assigned to the same bay, overlapping intervals must follow
at least one entry/exit order that is possible for some integer placement pair
in that bay. Pairwise placement choices do not have to be globally consistent,
so this remains a relaxation and its objective bound is a conservative lower
bound for total tardiness.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SUBMISSION_ROOT = ROOT / "ogc_solver"
BASELINE_ROOT = ROOT / "baseline" / "baseline"

sys.path.insert(0, str(BASELINE_ROOT))
sys.path.insert(0, str(SUBMISSION_ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from batch_eval import _base_layer_area, objective_lower_bound  # noqa: E402
from ogc_solver.state import fits_in_bay, orientation_bbox  # noqa: E402


FIFO_LEFT_RIGHT = 1 << 0
LEFT_CONTAINS_RIGHT = 1 << 1
RIGHT_CONTAINS_LEFT = 1 << 2
FIFO_RIGHT_LEFT = 1 << 3
ALL_OVERLAP_RELATIONS = FIFO_LEFT_RIGHT | LEFT_CONTAINS_RIGHT | RIGHT_CONTAINS_LEFT | FIFO_RIGHT_LEFT


@dataclass(frozen=True)
class _PlacementOption:
    orient_idx: int
    x: int
    y: int
    block: Any


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("instance", type=Path)
    parser.add_argument("--time-limit", type=float, default=30.0)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--area-scale", type=int, default=100)
    parser.add_argument("--max-pair-checks", type=int, default=200_000)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    with args.instance.open(encoding="utf-8") as handle:
        prob_info = json.load(handle)

    result = pair_relation_tardiness_lb(
        prob_info,
        time_limit=args.time_limit,
        workers=args.workers,
        area_scale=args.area_scale,
        max_pair_checks=args.max_pair_checks,
    )
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("w", encoding="utf-8") as handle:
            json.dump(result, handle, indent=2)
    print(json.dumps(result, indent=2))


def pair_relation_tardiness_lb(
    prob_info: dict[str, Any],
    *,
    time_limit: float,
    workers: int,
    area_scale: int,
    max_pair_checks: int,
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
    relation_masks, unknown_tests, checked_tests = _pair_relation_masks(
        prob_info,
        geometry,
        max_pair_checks=max_pair_checks,
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
            demands_by_bay[bay_id].append(_scaled_min_area_for_bay(block, geometry.get((block_id, bay_id), []), area_scale))
            presences[(block_id, bay_id)] = presence
            block_presences.append(presence)
        model.AddExactlyOne(block_presences)

    for bay_id, intervals in enumerate(optional_intervals_by_bay):
        if intervals:
            model.AddCumulative(intervals, demands_by_bay[bay_id], bay_capacities[bay_id])

    constrained_pairs = 0
    no_overlap_only_pairs = 0
    partial_relation_pairs = 0
    for (bay_id, left_id, right_id), mask in relation_masks.items():
        left_presence = presences.get((left_id, bay_id))
        right_presence = presences.get((right_id, bay_id))
        if left_presence is None or right_presence is None:
            continue
        if mask == ALL_OVERLAP_RELATIONS:
            continue

        relation_literals = []
        left_before_right = model.NewBoolVar(f"no_overlap_{left_id}_{right_id}_b{bay_id}")
        model.Add(ends[left_id] <= starts[right_id]).OnlyEnforceIf([left_presence, right_presence, left_before_right])
        relation_literals.append(left_before_right)

        right_before_left = model.NewBoolVar(f"no_overlap_{right_id}_{left_id}_b{bay_id}")
        model.Add(ends[right_id] <= starts[left_id]).OnlyEnforceIf([left_presence, right_presence, right_before_left])
        relation_literals.append(right_before_left)

        if mask & FIFO_LEFT_RIGHT:
            lit = model.NewBoolVar(f"fifo_{left_id}_{right_id}_b{bay_id}")
            model.Add(starts[left_id] <= starts[right_id]).OnlyEnforceIf([left_presence, right_presence, lit])
            model.Add(ends[left_id] <= ends[right_id]).OnlyEnforceIf([left_presence, right_presence, lit])
            relation_literals.append(lit)
        if mask & LEFT_CONTAINS_RIGHT:
            lit = model.NewBoolVar(f"contains_{left_id}_{right_id}_b{bay_id}")
            model.Add(starts[left_id] <= starts[right_id]).OnlyEnforceIf([left_presence, right_presence, lit])
            model.Add(ends[right_id] <= ends[left_id]).OnlyEnforceIf([left_presence, right_presence, lit])
            relation_literals.append(lit)
        if mask & RIGHT_CONTAINS_LEFT:
            lit = model.NewBoolVar(f"contains_{right_id}_{left_id}_b{bay_id}")
            model.Add(starts[right_id] <= starts[left_id]).OnlyEnforceIf([left_presence, right_presence, lit])
            model.Add(ends[left_id] <= ends[right_id]).OnlyEnforceIf([left_presence, right_presence, lit])
            relation_literals.append(lit)
        if mask & FIFO_RIGHT_LEFT:
            lit = model.NewBoolVar(f"fifo_{right_id}_{left_id}_b{bay_id}")
            model.Add(starts[right_id] <= starts[left_id]).OnlyEnforceIf([left_presence, right_presence, lit])
            model.Add(ends[right_id] <= ends[left_id]).OnlyEnforceIf([left_presence, right_presence, lit])
            relation_literals.append(lit)

        model.Add(sum(relation_literals) == 1).OnlyEnforceIf([left_presence, right_presence])
        constrained_pairs += 1
        if mask == 0:
            no_overlap_only_pairs += 1
        else:
            partial_relation_pairs += 1

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
        "max_pair_checks": int(max_pair_checks),
        "horizon": horizon,
        "base_obj1_lb": base_lb["lower_bound_obj1"],
        "base_weighted_lb": base_lb["lower_bound"],
        "secondary_weighted_lb": base_lb["lower_bound_secondary"],
        "pair_relation_obj1_bound": cp_bound,
        "pair_relation_obj1_incumbent": cp_incumbent,
        "combined_obj1_lb": combined_obj1_lb,
        "combined_weighted_lb": combined_weighted_lb,
        "relation_pairs": len(relation_masks),
        "constrained_pairs": constrained_pairs,
        "no_overlap_only_pairs": no_overlap_only_pairs,
        "partial_relation_pairs": partial_relation_pairs,
        "unknown_pair_tests": unknown_tests,
        "checked_pair_tests": checked_tests,
        "branches": solver.NumBranches(),
        "conflicts": solver.NumConflicts(),
        "wall_time": solver.WallTime(),
    }


def _precompute_geometry_options(prob_info: dict[str, Any]) -> dict[tuple[int, int], list[_PlacementOption]]:
    from utils import Bay, Block

    blocks = prob_info.get("blocks", [])
    bays = [Bay.from_dict(data, bay_id) for bay_id, data in enumerate(prob_info.get("bays", []))]
    options: dict[tuple[int, int], list[_PlacementOption]] = {}
    for block_id, block_data in enumerate(blocks):
        for bay_id, bay in enumerate(bays):
            rows = []
            bay_data = prob_info["bays"][bay_id]
            for orient_idx in range(len(block_data.get("shape", []))):
                bbox = orientation_bbox(block_data, orient_idx)
                min_x, min_y, max_x, max_y = bbox
                first_x = max(0, math.ceil(-min_x))
                first_y = max(0, math.ceil(-min_y))
                last_x = math.floor(float(bay_data["width"]) - max_x)
                last_y = math.floor(float(bay_data["height"]) - max_y)
                for x in range(first_x, last_x + 1):
                    for y in range(first_y, last_y + 1):
                        if not fits_in_bay(bay_data, bbox, x, y):
                            continue
                        rows.append(
                            _PlacementOption(
                                orient_idx=orient_idx,
                                x=x,
                                y=y,
                                block=Block(block_id, block_data, x=x, y=y, orient_idx=orient_idx),
                            )
                        )
            if rows:
                options[(block_id, bay_id)] = rows
    return options


def _pair_relation_masks(
    prob_info: dict[str, Any],
    geometry: dict[tuple[int, int], list[_PlacementOption]],
    *,
    max_pair_checks: int,
) -> tuple[dict[tuple[int, int, int], int], int, int]:
    blocks = prob_info.get("blocks", [])
    bays = prob_info.get("bays", [])
    masks: dict[tuple[int, int, int], int] = {}
    unknown_tests = 0
    checked_tests = 0
    for bay_id in range(len(bays)):
        for left_id in range(len(blocks)):
            left_options = geometry.get((left_id, bay_id), [])
            if not left_options:
                continue
            for right_id in range(left_id + 1, len(blocks)):
                if not _on_time_intervals_can_overlap(blocks[left_id], blocks[right_id]):
                    continue
                right_options = geometry.get((right_id, bay_id), [])
                if not right_options:
                    continue
                checked_tests += 1
                mask, complete = _pair_relation_mask(
                    prob_info,
                    bay_id,
                    left_options,
                    right_options,
                    max_pair_checks=max_pair_checks,
                )
                if not complete and mask != ALL_OVERLAP_RELATIONS:
                    unknown_tests += 1
                    continue
                if mask != ALL_OVERLAP_RELATIONS:
                    masks[(bay_id, left_id, right_id)] = mask
    return masks, unknown_tests, checked_tests


def _on_time_intervals_can_overlap(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_release = int(left.get("release_time", 0))
    right_release = int(right.get("release_time", 0))
    left_due = int(left.get("due_date", 0))
    right_due = int(right.get("due_date", 0))
    return max(left_release, right_release) < min(left_due, right_due)


def _pair_relation_mask(
    prob_info: dict[str, Any],
    bay_id: int,
    left_options: list[_PlacementOption],
    right_options: list[_PlacementOption],
    *,
    max_pair_checks: int,
) -> tuple[int, bool]:
    from utils import Bay, check_collisions, check_entry, check_exit

    bay = Bay.from_dict(prob_info["bays"][bay_id], bay_id)
    mask = 0
    checks = 0
    for left in left_options:
        for right in right_options:
            checks += 1
            if checks > max_pair_checks:
                return mask, False
            left_block = left.block
            right_block = right.block
            if check_collisions(bay, [left_block, right_block]):
                continue
            left_then_right_entry = not check_entry(bay, [left_block], right_block, fast=True)
            right_then_left_entry = not check_entry(bay, [right_block], left_block, fast=True)
            left_first_exit = not check_exit(bay, [left_block, right_block], left_block, fast=True)
            right_first_exit = not check_exit(bay, [left_block, right_block], right_block, fast=True)
            if left_then_right_entry and left_first_exit:
                mask |= FIFO_LEFT_RIGHT
            if left_then_right_entry and right_first_exit:
                mask |= LEFT_CONTAINS_RIGHT
            if right_then_left_entry and left_first_exit:
                mask |= RIGHT_CONTAINS_LEFT
            if right_then_left_entry and right_first_exit:
                mask |= FIFO_RIGHT_LEFT
            if mask == ALL_OVERLAP_RELATIONS:
                return mask, True
    return mask, True


def _scaled_min_area_for_bay(block: dict[str, Any], options: list[_PlacementOption], area_scale: int) -> int:
    if not options:
        return 1
    areas = []
    for option in options:
        orientation = block.get("shape", [])[option.orient_idx]
        areas.append(max(0.0, _base_layer_area(orientation)))
    return max(1, math.floor(min(areas) * area_scale))


def _safe_horizon(blocks: list[dict[str, Any]]) -> int:
    max_release = max(int(block.get("release_time", 0)) for block in blocks)
    max_due = max(int(block.get("due_date", 0)) for block in blocks)
    total_processing = sum(int(block.get("processing_time", 0)) for block in blocks)
    return max(max_due, max_release) + total_processing


if __name__ == "__main__":
    main()
