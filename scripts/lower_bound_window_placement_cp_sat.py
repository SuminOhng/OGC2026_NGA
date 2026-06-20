"""Compute a shared-placement CP-SAT lower bound for a selected OGC window.

This is an analysis tool, not submitted solver logic. For a small selected
subset of blocks, it enumerates every integer placement option in every
compatible bay. The CP model chooses one placement per selected block and
forbids time overlap between blocks whose chosen placements collide.

The model ignores unselected blocks and crane access paths, so its objective
bound is still a conservative lower bound for total tardiness. It is stronger
than pair/triplet probes because one block's chosen placement must be shared
consistently across all pair conflicts in the selected subset.
"""

from __future__ import annotations

import argparse
import itertools
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
from lower_bound_pair_relation_cp_sat import _PlacementOption, _precompute_geometry_options  # noqa: E402


@dataclass(frozen=True)
class _OptionRecord:
    bay_id: int
    option: _PlacementOption
    bbox: tuple[float, float, float, float]
    polys: tuple[Any, ...]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("instance", type=Path)
    parser.add_argument("--time-limit", type=float, default=30.0)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--max-blocks", type=int, default=18)
    parser.add_argument("--max-options-per-block", type=int, default=20_000)
    parser.add_argument("--max-conflict-checks", type=int, default=2_000_000)
    parser.add_argument("--compress-signatures", action="store_true")
    parser.add_argument(
        "--selection",
        choices=["tight-area", "early-due", "few-options", "manual"],
        default="tight-area",
    )
    parser.add_argument("--blocks", nargs="*", type=int, default=[])
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    with args.instance.open(encoding="utf-8") as handle:
        prob_info = json.load(handle)

    result = shared_placement_window_lb(
        prob_info,
        time_limit=args.time_limit,
        workers=args.workers,
        max_blocks=args.max_blocks,
        max_options_per_block=args.max_options_per_block,
        max_conflict_checks=args.max_conflict_checks,
        compress_signatures=args.compress_signatures,
        selection=args.selection,
        manual_blocks=args.blocks,
    )
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("w", encoding="utf-8") as handle:
            json.dump(result, handle, indent=2)
    print(json.dumps(result, indent=2))


def shared_placement_window_lb(
    prob_info: dict[str, Any],
    *,
    time_limit: float,
    workers: int,
    max_blocks: int,
    max_options_per_block: int,
    max_conflict_checks: int,
    compress_signatures: bool,
    selection: str,
    manual_blocks: list[int],
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
    selected = _select_blocks(
        prob_info,
        geometry,
        max_blocks=max_blocks,
        max_options_per_block=max_options_per_block,
        selection=selection,
        manual_blocks=manual_blocks,
    )
    if len(selected) < 2:
        return {
            "instance": prob_info.get("name"),
            "available": False,
            "error": "fewer than two selected blocks have exhaustive placement options",
            "selected_blocks": selected,
        }

    option_rows = _selected_option_rows(selected, geometry)
    total_options = sum(len(rows) for rows in option_rows.values())
    conflict_pairs, conflict_stats = _placement_conflict_pairs(
        prob_info,
        selected,
        option_rows,
        max_conflict_checks=max_conflict_checks,
    )
    if not conflict_stats["complete"]:
        return {
            "instance": prob_info.get("name"),
            "available": False,
            "error": "conflict enumeration exceeded max-conflict-checks; no bound reported",
            "selected_blocks": selected,
            "total_options": total_options,
            **conflict_stats,
        }
    compression_stats: dict[str, Any] = {"compressed": False}
    if compress_signatures:
        option_rows, conflict_pairs, compression_stats = _compress_by_conflict_signature(
            selected,
            option_rows,
            conflict_pairs,
        )
        total_options = sum(len(rows) for rows in option_rows.values())

    model = cp_model.CpModel()
    horizon = _safe_subset_horizon(blocks, selected)
    starts: dict[int, Any] = {}
    ends: dict[int, Any] = {}
    tardiness_vars = []
    option_vars: dict[tuple[int, int], Any] = {}

    for block_id in selected:
        block = blocks[block_id]
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

        literals = []
        for option_index, _option in enumerate(option_rows[block_id]):
            literal = model.NewBoolVar(f"place_{block_id}_{option_index}")
            option_vars[(block_id, option_index)] = literal
            literals.append(literal)
        model.AddExactlyOne(literals)

    for left_id, left_option, right_id, right_option in conflict_pairs:
        left_literal = option_vars[(left_id, left_option)]
        right_literal = option_vars[(right_id, right_option)]
        left_before = model.NewBoolVar(f"before_{left_id}_{left_option}_{right_id}_{right_option}")
        right_before = model.NewBoolVar(f"before_{right_id}_{right_option}_{left_id}_{left_option}")
        model.Add(ends[left_id] <= starts[right_id]).OnlyEnforceIf([left_literal, right_literal, left_before])
        model.Add(ends[right_id] <= starts[left_id]).OnlyEnforceIf([left_literal, right_literal, right_before])
        model.Add(left_before + right_before >= 1).OnlyEnforceIf([left_literal, right_literal])

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
        "selection": selection,
        "max_blocks": int(max_blocks),
        "max_options_per_block": int(max_options_per_block),
        "max_conflict_checks": int(max_conflict_checks),
        "compress_signatures": bool(compress_signatures),
        "selected_blocks": selected,
        "selected_block_count": len(selected),
        "total_options": total_options,
        "horizon": horizon,
        "base_obj1_lb": base_lb["lower_bound_obj1"],
        "base_weighted_lb": base_lb["lower_bound"],
        "secondary_weighted_lb": base_lb["lower_bound_secondary"],
        "window_placement_obj1_bound": cp_bound,
        "window_placement_obj1_incumbent": cp_incumbent,
        "combined_obj1_lb": combined_obj1_lb,
        "combined_weighted_lb": combined_weighted_lb,
        **conflict_stats,
        **compression_stats,
        "branches": solver.NumBranches(),
        "conflicts": solver.NumConflicts(),
        "wall_time": solver.WallTime(),
    }


def _select_blocks(
    prob_info: dict[str, Any],
    geometry: dict[tuple[int, int], list[_PlacementOption]],
    *,
    max_blocks: int,
    max_options_per_block: int,
    selection: str,
    manual_blocks: list[int],
) -> list[int]:
    blocks = prob_info.get("blocks", [])
    if selection == "manual":
        return [
            block_id
            for block_id in manual_blocks
            if 0 <= block_id < len(blocks)
            and 0 < _option_count(block_id, geometry) <= max_options_per_block
        ][:max_blocks]

    rows = []
    for block_id, block in enumerate(blocks):
        option_count = _option_count(block_id, geometry)
        if option_count <= 0 or option_count > max_options_per_block:
            continue
        release = int(block.get("release_time", 0))
        due = int(block.get("due_date", 0))
        processing = int(block.get("processing_time", 0))
        slack = max(0, due - release - processing)
        area = _min_base_area(block)
        if selection == "few-options":
            score = (option_count, slack, due, -area)
        elif selection == "early-due":
            score = (due, slack, -area)
        else:
            score = (slack, due, -area)
        rows.append((score, block_id))
    rows.sort()
    return [block_id for _score, block_id in rows[:max_blocks]]


def _option_count(block_id: int, geometry: dict[tuple[int, int], list[_PlacementOption]]) -> int:
    return sum(len(options) for (other_id, _bay_id), options in geometry.items() if other_id == block_id)


def _selected_option_rows(
    selected: list[int],
    geometry: dict[tuple[int, int], list[_PlacementOption]],
) -> dict[int, list[_OptionRecord]]:
    from utils import _poly_from_verts

    rows: dict[int, list[_OptionRecord]] = {block_id: [] for block_id in selected}
    for (block_id, bay_id), options in geometry.items():
        if block_id not in rows:
            continue
        for option in options:
            rows[block_id].append(
                _OptionRecord(
                    bay_id=bay_id,
                    option=option,
                    bbox=option.block.bounding_rect(),
                    polys=tuple(_poly_from_verts(layer) for layer in option.block.layers_at_pos()),
                )
            )
    return rows


def _placement_conflict_pairs(
    prob_info: dict[str, Any],
    selected: list[int],
    option_rows: dict[int, list[_OptionRecord]],
    *,
    max_conflict_checks: int,
) -> tuple[list[tuple[int, int, int, int]], dict[str, Any]]:
    conflict_pairs = []
    candidate_pairs = 0
    bbox_overlap_pairs = 0
    precise_checks = 0
    for left_id, right_id in itertools.combinations(selected, 2):
        for left_index, left_record in enumerate(option_rows[left_id]):
            for right_index, right_record in enumerate(option_rows[right_id]):
                if left_record.bay_id != right_record.bay_id:
                    continue
                candidate_pairs += 1
                if not _bbox_overlap(left_record.bbox, right_record.bbox):
                    continue
                bbox_overlap_pairs += 1
                precise_checks += 1
                if precise_checks > max_conflict_checks:
                    return conflict_pairs, {
                        "complete": False,
                        "candidate_option_pairs": candidate_pairs,
                        "bbox_overlap_pairs": bbox_overlap_pairs,
                        "precise_conflict_checks": precise_checks,
                        "placement_conflict_pairs": len(conflict_pairs),
                    }
                if _records_collide(left_record, right_record):
                    conflict_pairs.append((left_id, left_index, right_id, right_index))
    return conflict_pairs, {
        "complete": True,
        "candidate_option_pairs": candidate_pairs,
        "bbox_overlap_pairs": bbox_overlap_pairs,
        "precise_conflict_checks": precise_checks,
        "placement_conflict_pairs": len(conflict_pairs),
    }


def _compress_by_conflict_signature(
    selected: list[int],
    option_rows: dict[int, list[_OptionRecord]],
    conflict_pairs: list[tuple[int, int, int, int]],
) -> tuple[dict[int, list[_OptionRecord]], list[tuple[int, int, int, int]], dict[str, Any]]:
    selected_order = {block_id: index for index, block_id in enumerate(selected)}
    signatures: dict[int, list[list[int]]] = {}
    for block_id in selected:
        signatures[block_id] = [
            [0 for _other in selected]
            for _option in option_rows[block_id]
        ]

    for left_id, left_index, right_id, right_index in conflict_pairs:
        right_pos = selected_order[right_id]
        left_pos = selected_order[left_id]
        signatures[left_id][left_index][right_pos] |= 1 << right_index
        signatures[right_id][right_index][left_pos] |= 1 << left_index

    compressed_rows: dict[int, list[_OptionRecord]] = {}
    old_to_group: dict[tuple[int, int], int] = {}
    original_options = sum(len(rows) for rows in option_rows.values())
    for block_id in selected:
        groups: dict[tuple, int] = {}
        compressed_rows[block_id] = []
        for option_index, option in enumerate(option_rows[block_id]):
            key = (
                option.bay_id,
                tuple(signatures[block_id][option_index]),
            )
            group_index = groups.get(key)
            if group_index is None:
                group_index = len(compressed_rows[block_id])
                groups[key] = group_index
                compressed_rows[block_id].append(option)
            old_to_group[(block_id, option_index)] = group_index

    compressed_conflicts_set = set()
    for left_id, left_index, right_id, right_index in conflict_pairs:
        compressed_conflicts_set.add(
            (
                left_id,
                old_to_group[(left_id, left_index)],
                right_id,
                old_to_group[(right_id, right_index)],
            )
        )
    compressed_conflicts = sorted(compressed_conflicts_set)
    compressed_options = sum(len(rows) for rows in compressed_rows.values())
    return compressed_rows, compressed_conflicts, {
        "compressed": True,
        "original_options": original_options,
        "compressed_options": compressed_options,
        "original_conflict_pairs": len(conflict_pairs),
        "compressed_conflict_pairs": len(compressed_conflicts),
    }


def _bbox_overlap(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> bool:
    return left[0] < right[2] and right[0] < left[2] and left[1] < right[3] and right[1] < left[3]


def _records_collide(left: _OptionRecord, right: _OptionRecord) -> bool:
    for layer_index in range(min(len(left.polys), len(right.polys))):
        left_poly = left.polys[layer_index]
        right_poly = right.polys[layer_index]
        if left_poly is None or right_poly is None:
            continue
        try:
            inter = left_poly.intersection(right_poly)
        except Exception:
            continue
        if not inter.is_empty and inter.area > 0:
            return True
    return False


def _min_base_area(block: dict[str, Any]) -> float:
    areas = []
    for orientation in block.get("shape", []):
        layers = orientation.get("layers", [])
        if layers:
            areas.append(_base_layer_area(orientation))
    return min(areas) if areas else 0.0


def _safe_subset_horizon(blocks: list[dict[str, Any]], selected: list[int]) -> int:
    max_release = max(int(blocks[block_id].get("release_time", 0)) for block_id in selected)
    max_due = max(int(blocks[block_id].get("due_date", 0)) for block_id in selected)
    total_processing = sum(int(blocks[block_id].get("processing_time", 0)) for block_id in selected)
    return max(max_release, max_due) + total_processing


if __name__ == "__main__":
    main()
