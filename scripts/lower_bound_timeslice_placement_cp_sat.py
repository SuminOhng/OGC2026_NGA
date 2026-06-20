"""Compute mandatory time-slice placement lower bounds for OGC obj1.

This is an analysis tool, not submitted solver logic. For an integer time `t`,
a block must be present at `t` in every on-time schedule if:

    release + processing > t and t + processing > due_date

If a subset of such mandatory-at-t blocks cannot be placed simultaneously in
any integer placements, then at least one block in the subset must avoid `t`.
Because avoiding before `t` is impossible by the first inequality, avoiding it
requires starting at or after `t`, giving tardiness at least:

    t + processing - due_date

The tool searches high-pressure time slices, checks exact simultaneous
placement feasibility for selected mandatory blocks, and reports the best
certified obj1 lower bound found.
"""

from __future__ import annotations

import argparse
import json
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
from lower_bound_pair_relation_cp_sat import _precompute_geometry_options  # noqa: E402
from lower_bound_window_placement_cp_sat import (  # noqa: E402
    _placement_conflict_pairs,
    _selected_option_rows,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("instance", type=Path)
    parser.add_argument("--time-limit", type=float, default=10.0)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--max-times", type=int, default=20)
    parser.add_argument("--max-blocks", type=int, default=8)
    parser.add_argument("--max-options-per-block", type=int, default=20_000)
    parser.add_argument("--max-conflict-checks", type=int, default=5_000_000)
    parser.add_argument(
        "--selection",
        choices=["few-options", "min-delay", "area-pressure"],
        default="few-options",
    )
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    with args.instance.open(encoding="utf-8") as handle:
        prob_info = json.load(handle)

    result = timeslice_placement_lb(
        prob_info,
        time_limit=args.time_limit,
        workers=args.workers,
        max_times=args.max_times,
        max_blocks=args.max_blocks,
        max_options_per_block=args.max_options_per_block,
        max_conflict_checks=args.max_conflict_checks,
        selection=args.selection,
    )
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("w", encoding="utf-8") as handle:
            json.dump(result, handle, indent=2)
    print(json.dumps(result, indent=2))


def timeslice_placement_lb(
    prob_info: dict[str, Any],
    *,
    time_limit: float,
    workers: int,
    max_times: int,
    max_blocks: int,
    max_options_per_block: int,
    max_conflict_checks: int,
    selection: str,
) -> dict[str, Any]:
    try:
        from ortools.sat.python import cp_model  # noqa: F401
    except Exception as exc:  # pragma: no cover - environment-specific
        return {
            "instance": prob_info.get("name"),
            "available": False,
            "error": f"ortools import failed: {exc}",
        }

    geometry = _precompute_geometry_options(prob_info)
    candidate_times = _candidate_times(
        prob_info,
        geometry,
        max_times=max_times,
        max_blocks=max_blocks,
        max_options_per_block=max_options_per_block,
        selection=selection,
    )

    checked = []
    best_lb = 0.0
    best_certificate = None
    for row in candidate_times:
        probe = _check_time_slice(
            prob_info,
            geometry,
            time_value=row["time"],
            selected=row["selected_blocks"],
            time_limit=time_limit,
            workers=workers,
            max_conflict_checks=max_conflict_checks,
        )
        checked.append(probe)
        if probe.get("certified_obj1_lb", 0.0) > best_lb:
            best_lb = float(probe["certified_obj1_lb"])
            best_certificate = probe

    base = objective_lower_bound(prob_info)
    combined_obj1_lb = max(float(base["lower_bound_obj1"]), best_lb)
    w1 = float(prob_info.get("weights", {}).get("w1", 1.0))
    combined_weighted_lb = w1 * combined_obj1_lb + float(base["lower_bound_secondary"])
    return {
        "instance": prob_info.get("name"),
        "available": True,
        "selection": selection,
        "max_times": int(max_times),
        "max_blocks": int(max_blocks),
        "max_options_per_block": int(max_options_per_block),
        "max_conflict_checks": int(max_conflict_checks),
        "base_obj1_lb": base["lower_bound_obj1"],
        "base_weighted_lb": base["lower_bound"],
        "secondary_weighted_lb": base["lower_bound_secondary"],
        "timeslice_obj1_lb": best_lb,
        "combined_obj1_lb": combined_obj1_lb,
        "combined_weighted_lb": combined_weighted_lb,
        "best_certificate": best_certificate,
        "checked_times": checked,
    }


def _candidate_times(
    prob_info: dict[str, Any],
    geometry: dict[tuple[int, int], list[Any]],
    *,
    max_times: int,
    max_blocks: int,
    max_options_per_block: int,
    selection: str,
) -> list[dict[str, Any]]:
    blocks = prob_info.get("blocks", [])
    max_due = max(int(block.get("due_date", 0)) for block in blocks)
    rows = []
    for time_value in range(max_due + 1):
        mandatory = []
        for block_id, block in enumerate(blocks):
            option_count = _option_count(block_id, geometry)
            if option_count <= 0 or option_count > max_options_per_block:
                continue
            release = int(block.get("release_time", 0))
            processing = int(block.get("processing_time", 0))
            due = int(block.get("due_date", 0))
            if release + processing <= time_value:
                continue
            if time_value + processing <= due:
                continue
            avoid_after_delay = time_value + processing - due
            if avoid_after_delay <= 0:
                continue
            mandatory.append(
                {
                    "block_id": block_id,
                    "option_count": option_count,
                    "delay": avoid_after_delay,
                    "area": _min_base_area(block),
                    "release": release,
                    "due": due,
                    "processing": processing,
                }
            )
        if len(mandatory) < 2:
            continue
        selected = _select_mandatory(mandatory, max_blocks=max_blocks, selection=selection)
        if len(selected) < 2:
            continue
        rows.append(
            {
                "time": time_value,
                "mandatory_count": len(mandatory),
                "selected_blocks": [row["block_id"] for row in selected],
                "selected_delay_lb": min(row["delay"] for row in selected),
                "selected_options": sum(row["option_count"] for row in selected),
                "pressure": sum(row["area"] for row in selected),
            }
        )
    rows.sort(key=lambda row: (-row["selected_delay_lb"], -row["mandatory_count"], row["selected_options"]))
    return rows[:max_times]


def _select_mandatory(rows: list[dict[str, Any]], *, max_blocks: int, selection: str) -> list[dict[str, Any]]:
    if selection == "min-delay":
        key = lambda row: (-row["delay"], row["option_count"], -row["area"])
    elif selection == "area-pressure":
        key = lambda row: (-row["area"], row["option_count"], -row["delay"])
    else:
        key = lambda row: (row["option_count"], -row["delay"], -row["area"])
    return sorted(rows, key=key)[:max_blocks]


def _check_time_slice(
    prob_info: dict[str, Any],
    geometry: dict[tuple[int, int], list[Any]],
    *,
    time_value: int,
    selected: list[int],
    time_limit: float,
    workers: int,
    max_conflict_checks: int,
) -> dict[str, Any]:
    try:
        from ortools.sat.python import cp_model
    except Exception as exc:  # pragma: no cover - environment-specific
        return {"available": False, "time": time_value, "error": f"ortools import failed: {exc}"}

    option_rows = _selected_option_rows(selected, geometry)
    total_options = sum(len(rows) for rows in option_rows.values())
    conflict_pairs, conflict_stats = _placement_conflict_pairs(
        prob_info,
        selected,
        option_rows,
        max_conflict_checks=max_conflict_checks,
    )
    result = {
        "time": int(time_value),
        "selected_blocks": selected,
        "selected_block_count": len(selected),
        "total_options": total_options,
        **conflict_stats,
    }
    if not conflict_stats["complete"]:
        result["status"] = "SKIPPED_INCOMPLETE_CONFLICTS"
        result["certified_obj1_lb"] = 0.0
        return result

    model = cp_model.CpModel()
    option_vars = {}
    for block_id in selected:
        literals = []
        for option_index, _option in enumerate(option_rows[block_id]):
            literal = model.NewBoolVar(f"place_{block_id}_{option_index}")
            option_vars[(block_id, option_index)] = literal
            literals.append(literal)
        model.AddExactlyOne(literals)
    for left_id, left_option, right_id, right_option in conflict_pairs:
        model.AddBoolOr(
            [
                option_vars[(left_id, left_option)].Not(),
                option_vars[(right_id, right_option)].Not(),
            ]
        )

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = max(0.1, float(time_limit))
    solver.parameters.num_search_workers = max(1, int(workers))
    status = solver.Solve(model)
    status_name = solver.StatusName(status)
    result.update(
        {
            "status": status_name,
            "branches": solver.NumBranches(),
            "conflicts": solver.NumConflicts(),
            "wall_time": solver.WallTime(),
            "certified_obj1_lb": 0.0,
        }
    )
    if status == cp_model.INFEASIBLE:
        result["certified_obj1_lb"] = _timeslice_delay_lb(prob_info, selected, time_value)
    return result


def _timeslice_delay_lb(prob_info: dict[str, Any], selected: list[int], time_value: int) -> float:
    blocks = prob_info.get("blocks", [])
    delays = []
    for block_id in selected:
        block = blocks[block_id]
        delays.append(int(time_value) + int(block.get("processing_time", 0)) - int(block.get("due_date", 0)))
    return float(max(0, min(delays))) if delays else 0.0


def _option_count(block_id: int, geometry: dict[tuple[int, int], list[Any]]) -> int:
    return sum(len(options) for (other_id, _bay_id), options in geometry.items() if other_id == block_id)


def _min_base_area(block: dict[str, Any]) -> float:
    areas = []
    for orientation in block.get("shape", []):
        layers = orientation.get("layers", [])
        if layers:
            areas.append(_polygon_area(layers[0]))
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


if __name__ == "__main__":
    main()
