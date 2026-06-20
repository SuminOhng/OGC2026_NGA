"""Build a conservative footprint-surrogate schedule with CP-SAT.

This is an analysis tool, not submitted solver logic.  It tests whether a
simpler world can produce good bay assignments and exit times before we spend
effort on actual tier-geometry relaxation.

Each placement option uses the orientation bounding box as a vertical-prism
footprint.  If two selected options in the same bay have overlapping footprints,
their processing intervals cannot overlap.  This intentionally removes most
crane-path exit obstructions from the model.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SUBMISSION_ROOT = ROOT / "ogc_solver"
BASELINE_ROOT = ROOT / "baseline" / "baseline"

sys.path.insert(0, str(BASELINE_ROOT))
sys.path.insert(0, str(SUBMISSION_ROOT))

from ogc_solver.state import fits_in_bay, orientation_bbox  # noqa: E402
from utils import check_feasibility  # noqa: E402


@dataclass(frozen=True)
class _Option:
    option_id: int
    block_id: int
    bay_id: int
    orient_idx: int
    x: int
    y: int
    rect: tuple[int, int, int, int]
    preference_penalty: int
    slot: str


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("instance", type=Path)
    parser.add_argument("--time-limit", type=float, default=30.0)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--grid-step", type=int, default=10)
    parser.add_argument("--mode", choices=["grid", "lanes", "edge_lanes"], default="grid")
    parser.add_argument("--lane-step", type=int, default=None)
    parser.add_argument("--x-anchor-count", type=int, default=3)
    parser.add_argument("--edge-cap-strategy", choices=["ranked", "balanced"], default="ranked")
    parser.add_argument("--max-options-per-block", type=int, default=16)
    parser.add_argument("--max-conflict-pairs", type=int, default=500_000)
    parser.add_argument("--max-blocks", type=int, default=None)
    parser.add_argument("--greedy-schedule-cp-limit", type=float, default=2.0)
    parser.add_argument("--neighborhood-cp-limit", type=float, default=0.0)
    parser.add_argument("--neighborhood-blocks", type=int, default=8)
    parser.add_argument("--neighborhood-objective", choices=["tardiness", "objective", "both"], default="tardiness")
    parser.add_argument("--neighborhood-selection", choices=["tardy", "blockers", "both"], default="tardy")
    parser.add_argument("--neighborhood-rounds", type=int, default=1)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--save-solution", type=Path, default=None)
    args = parser.parse_args()

    with args.instance.open(encoding="utf-8") as handle:
        prob_info = json.load(handle)

    if args.max_blocks is not None:
        prob_info = _slice_instance_by_due_date(prob_info, args.max_blocks)

    result = solve_surrogate(
        prob_info,
        time_limit=args.time_limit,
        workers=args.workers,
        mode=args.mode,
        grid_step=args.grid_step,
        lane_step=args.lane_step,
        x_anchor_count=args.x_anchor_count,
        edge_cap_strategy=args.edge_cap_strategy,
        max_options_per_block=args.max_options_per_block,
        max_conflict_pairs=args.max_conflict_pairs,
        greedy_schedule_cp_limit=args.greedy_schedule_cp_limit,
        neighborhood_cp_limit=args.neighborhood_cp_limit,
        neighborhood_blocks=args.neighborhood_blocks,
        neighborhood_objective=args.neighborhood_objective,
        neighborhood_selection=args.neighborhood_selection,
        neighborhood_rounds=args.neighborhood_rounds,
    )

    solution = result.pop("_solution", None)
    if solution is not None and args.save_solution is not None:
        args.save_solution.parent.mkdir(parents=True, exist_ok=True)
        with args.save_solution.open("w", encoding="utf-8") as handle:
            json.dump(solution, handle)
        result["solution_path"] = str(args.save_solution)

    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("w", encoding="utf-8") as handle:
            json.dump(result, handle, indent=2)

    print(json.dumps(result, indent=2))


def solve_surrogate(
    prob_info: dict[str, Any],
    *,
    time_limit: float,
    workers: int,
    mode: str,
    grid_step: int,
    lane_step: int | None,
    x_anchor_count: int,
    edge_cap_strategy: str,
    max_options_per_block: int,
    max_conflict_pairs: int,
    greedy_schedule_cp_limit: float,
    neighborhood_cp_limit: float,
    neighborhood_blocks: int,
    neighborhood_objective: str,
    neighborhood_selection: str,
    neighborhood_rounds: int,
) -> dict[str, Any]:
    try:
        from ortools.sat.python import cp_model
    except Exception as exc:  # pragma: no cover - environment-specific
        return {"instance": prob_info.get("name"), "available": False, "error": f"ortools import failed: {exc}"}

    started_at = time.time()
    blocks = prob_info.get("blocks", [])
    bays = prob_info.get("bays", [])
    if not blocks or not bays:
        return {"instance": prob_info.get("name"), "available": False, "error": "empty instance"}

    if mode == "lanes":
        options_by_block, option_count_before_cap = _build_lane_options(
            prob_info,
            lane_step=max(1, int(lane_step if lane_step is not None else grid_step)),
            max_options_per_block=max(1, max_options_per_block),
        )
    elif mode == "edge_lanes":
        options_by_block, option_count_before_cap = _build_edge_lane_options(
            prob_info,
            lane_step=max(1, int(lane_step if lane_step is not None else grid_step)),
            x_anchor_count=max(2, int(x_anchor_count)),
            cap_strategy=edge_cap_strategy,
            max_options_per_block=max(1, max_options_per_block),
        )
    else:
        options_by_block, option_count_before_cap = _build_grid_options(
            prob_info,
            grid_step=max(1, grid_step),
            max_options_per_block=max(1, max_options_per_block),
        )
    missing = [block_id for block_id, options in options_by_block.items() if not options]
    if missing:
        return {
            "instance": prob_info.get("name"),
            "available": True,
            "status": "NO_OPTIONS",
            "missing_option_blocks": missing[:20],
            "missing_option_count": len(missing),
        }

    all_options = [option for options in options_by_block.values() for option in options]
    conflict_pairs, conflict_truncated = _conflict_pairs(all_options, max_conflict_pairs=max_conflict_pairs)
    conflicts_by_option = _conflicts_by_option(conflict_pairs)
    serial_solution, serial_assignments = _serial_solution_from_options(prob_info, options_by_block)
    serial_official = check_feasibility(prob_info, serial_solution)
    serial_obj1 = sum(
        max(0, int(row["exit_time"]) - int(blocks[int(row["block_id"])]["due_date"]))
        for row in serial_assignments
    )
    greedy_solution, greedy_assignments, greedy_meta = _greedy_conflict_graph_solution(
        prob_info,
        options_by_block,
        conflicts_by_option,
    )
    greedy_official = check_feasibility(prob_info, greedy_solution)
    greedy_obj1 = sum(
        max(0, int(row["exit_time"]) - int(blocks[int(row["block_id"])]["due_date"]))
        for row in greedy_assignments
    )
    schedule_cp_solution, schedule_cp_assignments, schedule_cp_meta = _polish_fixed_options_schedule_cp(
        prob_info,
        greedy_assignments,
        conflicts_by_option,
        time_limit=max(0.0, float(greedy_schedule_cp_limit)),
        workers=workers,
    )
    schedule_cp_official = check_feasibility(prob_info, schedule_cp_solution)
    schedule_cp_obj1 = sum(
        max(0, int(row["exit_time"]) - int(blocks[int(row["block_id"])]["due_date"]))
        for row in schedule_cp_assignments
    )
    option_by_id = {option.option_id: option for option in all_options}
    neighborhood_solution, neighborhood_assignments, neighborhood_meta = _polish_tardy_option_neighborhood_cp(
        prob_info,
        schedule_cp_assignments,
        options_by_block,
        option_by_id,
        conflicts_by_option,
        max_flexible_blocks=max(0, int(neighborhood_blocks)),
        time_limit=max(0.0, float(neighborhood_cp_limit)),
        workers=workers,
        objective_mode=neighborhood_objective,
        selection_mode=neighborhood_selection,
        max_rounds=neighborhood_rounds,
    )
    neighborhood_official = check_feasibility(prob_info, neighborhood_solution)
    neighborhood_obj1 = sum(
        max(0, int(row["exit_time"]) - int(blocks[int(row["block_id"])]["due_date"]))
        for row in neighborhood_assignments
    )

    model = cp_model.CpModel()
    horizon = _safe_horizon(blocks)
    weights = prob_info.get("weights", {})
    w1 = int(round(float(weights.get("w1", 1.0))))
    w2 = int(round(float(weights.get("w2", 1.0))))
    w3 = int(round(float(weights.get("w3", 1.0))))
    load_scale = 1000

    starts: dict[int, Any] = {}
    ends: dict[int, Any] = {}
    tardiness_vars: dict[int, Any] = {}
    option_lits: dict[int, Any] = {}
    option_intervals: dict[int, Any] = {}

    for block_id, block in enumerate(blocks):
        release = int(block.get("release_time", 0))
        processing = int(block.get("processing_time", 0))
        due = int(block.get("due_date", 0))
        start = model.NewIntVar(release, horizon, f"s_{block_id}")
        end = model.NewIntVar(release + processing, horizon + processing, f"e_{block_id}")
        model.Add(end == start + processing)
        tardiness = model.NewIntVar(0, horizon + processing, f"t_{block_id}")
        model.Add(tardiness >= end - due)
        model.Add(tardiness >= 0)
        starts[block_id] = start
        ends[block_id] = end
        tardiness_vars[block_id] = tardiness

        lits = []
        for option in options_by_block[block_id]:
            lit = model.NewBoolVar(f"o_{option.option_id}")
            interval = model.NewOptionalIntervalVar(start, processing, end, lit, f"iv_{option.option_id}")
            option_lits[option.option_id] = lit
            option_intervals[option.option_id] = interval
            lits.append(lit)
        model.AddExactlyOne(lits)

    for left_id, right_id in conflict_pairs:
        model.AddNoOverlap([option_intervals[left_id], option_intervals[right_id]])

    total_tardiness = sum(tardiness_vars.values())
    preference_cost = sum(option.preference_penalty * option_lits[option.option_id] for option in all_options)
    obj2_scaled = _add_scaled_obj2(model, prob_info, all_options, option_lits, load_scale=load_scale)
    start_tie_break = sum(starts.values())
    model.Minimize(
        w1 * load_scale * total_tardiness
        + w2 * obj2_scaled
        + w3 * load_scale * preference_cost
        + start_tie_break
    )
    _add_serial_hint(model, prob_info, options_by_block, option_lits, starts, ends)

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = max(0.1, float(time_limit))
    solver.parameters.num_search_workers = max(1, int(workers))
    status = solver.Solve(model)
    status_name = solver.StatusName(status)
    elapsed = time.time() - started_at

    feasible_statuses = {cp_model.OPTIMAL, cp_model.FEASIBLE}
    result: dict[str, Any] = {
        "instance": prob_info.get("name"),
        "available": True,
        "status": status_name,
        "elapsed": round(elapsed, 3),
        "n_blocks": len(blocks),
        "n_bays": len(bays),
        "mode": mode,
        "grid_step": grid_step,
        "lane_step": lane_step if lane_step is not None else grid_step,
        "x_anchor_count": x_anchor_count,
        "edge_cap_strategy": edge_cap_strategy,
        "max_options_per_block": max_options_per_block,
        "option_count_before_cap": option_count_before_cap,
        "option_count": len(all_options),
        "conflict_pairs": len(conflict_pairs),
        "conflict_truncated": conflict_truncated,
        "objective_bound": solver.BestObjectiveBound() / load_scale if status in feasible_statuses else None,
        "serial_official_feasible": serial_official.get("feasible"),
        "serial_official_stage": serial_official.get("stage"),
        "serial_official_objective": serial_official.get("objective"),
        "serial_official_obj1": serial_official.get("obj1"),
        "serial_official_obj2": serial_official.get("obj2"),
        "serial_official_obj3": serial_official.get("obj3"),
        "serial_surrogate_obj1": serial_obj1,
        "greedy_official_feasible": greedy_official.get("feasible"),
        "greedy_official_stage": greedy_official.get("stage"),
        "greedy_official_objective": greedy_official.get("objective"),
        "greedy_official_obj1": greedy_official.get("obj1"),
        "greedy_official_obj2": greedy_official.get("obj2"),
        "greedy_official_obj3": greedy_official.get("obj3"),
        "greedy_surrogate_obj1": greedy_obj1,
        "greedy_strategy": greedy_meta.get("strategy"),
        "greedy_score_mode": greedy_meta.get("score_mode"),
        "schedule_cp_status": schedule_cp_meta.get("status"),
        "schedule_cp_official_feasible": schedule_cp_official.get("feasible"),
        "schedule_cp_official_stage": schedule_cp_official.get("stage"),
        "schedule_cp_official_objective": schedule_cp_official.get("objective"),
        "schedule_cp_official_obj1": schedule_cp_official.get("obj1"),
        "schedule_cp_official_obj2": schedule_cp_official.get("obj2"),
        "schedule_cp_official_obj3": schedule_cp_official.get("obj3"),
        "schedule_cp_surrogate_obj1": schedule_cp_obj1,
        "neighborhood_cp_status": neighborhood_meta.get("status"),
        "neighborhood_cp_objective_mode": neighborhood_meta.get("objective_mode"),
        "neighborhood_cp_selection_mode": neighborhood_meta.get("selection_mode"),
        "neighborhood_cp_rounds": neighborhood_meta.get("rounds"),
        "neighborhood_cp_flexible_blocks": neighborhood_meta.get("flexible_blocks"),
        "neighborhood_cp_official_feasible": neighborhood_official.get("feasible"),
        "neighborhood_cp_official_stage": neighborhood_official.get("stage"),
        "neighborhood_cp_official_objective": neighborhood_official.get("objective"),
        "neighborhood_cp_official_obj1": neighborhood_official.get("obj1"),
        "neighborhood_cp_official_obj2": neighborhood_official.get("obj2"),
        "neighborhood_cp_official_obj3": neighborhood_official.get("obj3"),
        "neighborhood_cp_surrogate_obj1": neighborhood_obj1,
    }
    if status not in feasible_statuses:
        fallback_solution, source = _best_fallback_solution(
            (greedy_solution, greedy_official, "greedy_conflict_graph"),
            (schedule_cp_solution, schedule_cp_official, "fixed_option_schedule_cp"),
            (neighborhood_solution, neighborhood_official, "tardy_option_neighborhood_cp"),
        )
        if fallback_solution is not None:
            result["_solution"] = fallback_solution
            result["solution_source"] = source
        return result

    assignments = []
    for block_id in range(len(blocks)):
        chosen = None
        for option in options_by_block[block_id]:
            if solver.BooleanValue(option_lits[option.option_id]):
                chosen = option
                break
        if chosen is None:
            continue
        assignments.append(
            {
                "block_id": block_id,
                "bay_id": chosen.bay_id,
                "x": chosen.x,
                "y": chosen.y,
                "orient_idx": chosen.orient_idx,
                "entry_time": int(solver.Value(starts[block_id])),
                "exit_time": int(solver.Value(ends[block_id])),
            }
        )

    solution = {"operations": _build_operations(assignments)}
    official = check_feasibility(prob_info, solution)
    surrogate_obj1 = sum(
        max(0, int(row["exit_time"]) - int(blocks[int(row["block_id"])]["due_date"])) for row in assignments
    )
    result.update(
        {
            "surrogate_obj1": surrogate_obj1,
            "surrogate_preference_cost": sum(
                option_by_id[option.option_id].preference_penalty
                for options in options_by_block.values()
                for option in options
                if solver.BooleanValue(option_lits[option.option_id])
            ),
            "official_feasible": official.get("feasible"),
            "official_stage": official.get("stage"),
            "official_objective": official.get("objective"),
            "official_obj1": official.get("obj1"),
            "official_obj2": official.get("obj2"),
            "official_obj3": official.get("obj3"),
        }
    )
    best_solution, source = _best_fallback_solution(
        (greedy_solution, greedy_official, "greedy_conflict_graph"),
        (schedule_cp_solution, schedule_cp_official, "fixed_option_schedule_cp"),
        (neighborhood_solution, neighborhood_official, "tardy_option_neighborhood_cp"),
        (solution, official, "cp_sat"),
    )
    if best_solution is not None:
        result["solution_source"] = source
        result["_solution"] = best_solution
    if not official.get("feasible"):
        result["official_violations"] = official.get("violations", [])[:5]
    return result


def _build_grid_options(
    prob_info: dict[str, Any],
    *,
    grid_step: int,
    max_options_per_block: int,
) -> tuple[dict[int, list[_Option]], int]:
    options_by_block: dict[int, list[_Option]] = {}
    option_id = 0
    raw_count = 0
    for block_id, block in enumerate(prob_info.get("blocks", [])):
        preferences = block.get("bay_preferences", [])
        s_max = max(preferences) if preferences else 0
        rows: list[tuple[tuple[int, int, int, int, int], _Option]] = []
        for bay_id, bay in enumerate(prob_info.get("bays", [])):
            pref = int(preferences[bay_id]) if bay_id < len(preferences) else 0
            penalty = int(s_max - pref)
            for orient_idx in range(len(block.get("shape", []))):
                bbox = orientation_bbox(block, orient_idx)
                min_x, min_y, max_x, max_y = bbox
                width = int(math.ceil(max_x - min_x))
                height = int(math.ceil(max_y - min_y))
                if width <= 0 or height <= 0:
                    continue
                max_left = int(math.floor(float(bay["width"]) - width))
                max_bottom = int(math.floor(float(bay["height"]) - height))
                if max_left < 0 or max_bottom < 0:
                    continue
                for left in _grid_points(max_left, grid_step):
                    for bottom in _grid_points(max_bottom, grid_step):
                        x = int(math.ceil(left - min_x))
                        y = int(math.ceil(bottom - min_y))
                        if not fits_in_bay(bay, bbox, x, y):
                            continue
                        rect = (
                            int(math.floor(x + min_x)),
                            int(math.floor(y + min_y)),
                            int(math.ceil(x + max_x)),
                            int(math.ceil(y + max_y)),
                        )
                        option = _Option(
                            option_id=option_id,
                            block_id=block_id,
                            bay_id=bay_id,
                            orient_idx=orient_idx,
                            x=x,
                            y=y,
                            rect=rect,
                            preference_penalty=penalty,
                            slot=f"bay{bay_id}:grid:{left}:{bottom}",
                        )
                        option_id += 1
                        raw_count += 1
                        rank = (penalty, bay_id, bottom, left, orient_idx)
                        rows.append((rank, option))
        rows.sort(key=lambda item: item[0])
        options_by_block[block_id] = _diverse_option_cap(
            rows,
            max_options=max_options_per_block,
            n_bays=len(prob_info.get("bays", [])),
        )
    return options_by_block, raw_count


def _build_lane_options(
    prob_info: dict[str, Any],
    *,
    lane_step: int,
    max_options_per_block: int,
) -> tuple[dict[int, list[_Option]], int]:
    options_by_block: dict[int, list[_Option]] = {}
    option_id = 0
    raw_count = 0
    n_bays = len(prob_info.get("bays", []))
    for block_id, block in enumerate(prob_info.get("blocks", [])):
        preferences = block.get("bay_preferences", [])
        s_max = max(preferences) if preferences else 0
        rows: list[tuple[tuple[int, int, int, int, int], _Option]] = []
        for bay_id, bay in enumerate(prob_info.get("bays", [])):
            pref = int(preferences[bay_id]) if bay_id < len(preferences) else 0
            penalty = int(s_max - pref)
            for orient_idx in range(len(block.get("shape", []))):
                bbox = orientation_bbox(block, orient_idx)
                min_x, min_y, max_x, max_y = bbox
                width = int(math.ceil(max_x - min_x))
                height = int(math.ceil(max_y - min_y))
                if width <= 0 or height <= 0:
                    continue
                max_bottom = int(math.floor(float(bay["height"]) - height))
                if width > float(bay["width"]) or max_bottom < 0:
                    continue
                x = int(math.ceil(-min_x))
                for bottom in _grid_points(max_bottom, lane_step):
                    y = int(math.ceil(bottom - min_y))
                    if not fits_in_bay(bay, bbox, x, y):
                        continue
                    rect = (
                        int(math.floor(x + min_x)),
                        int(math.floor(y + min_y)),
                        int(math.ceil(x + max_x)),
                        int(math.ceil(y + max_y)),
                    )
                    option = _Option(
                        option_id=option_id,
                        block_id=block_id,
                        bay_id=bay_id,
                        orient_idx=orient_idx,
                        x=x,
                        y=y,
                        rect=rect,
                        preference_penalty=penalty,
                        slot=f"bay{bay_id}:lane:{bottom}",
                    )
                    option_id += 1
                    raw_count += 1
                    rank = (penalty, bay_id, bottom, height, orient_idx)
                    rows.append((rank, option))
        rows.sort(key=lambda item: item[0])
        options_by_block[block_id] = _diverse_option_cap(rows, max_options=max_options_per_block, n_bays=n_bays)
    return options_by_block, raw_count


def _build_edge_lane_options(
    prob_info: dict[str, Any],
    *,
    lane_step: int,
    x_anchor_count: int,
    cap_strategy: str,
    max_options_per_block: int,
) -> tuple[dict[int, list[_Option]], int]:
    options_by_block: dict[int, list[_Option]] = {}
    option_id = 0
    raw_count = 0
    n_bays = len(prob_info.get("bays", []))
    for block_id, block in enumerate(prob_info.get("blocks", [])):
        preferences = block.get("bay_preferences", [])
        s_max = max(preferences) if preferences else 0
        rows: list[tuple[tuple[int, ...], _Option]] = []
        for bay_id, bay in enumerate(prob_info.get("bays", [])):
            pref = int(preferences[bay_id]) if bay_id < len(preferences) else 0
            penalty = int(s_max - pref)
            for orient_idx in range(len(block.get("shape", []))):
                bbox = orientation_bbox(block, orient_idx)
                min_x, min_y, max_x, max_y = bbox
                width = int(math.ceil(max_x - min_x))
                height = int(math.ceil(max_y - min_y))
                if width <= 0 or height <= 0:
                    continue
                max_left = int(math.floor(float(bay["width"]) - width))
                max_bottom = int(math.floor(float(bay["height"]) - height))
                if max_left < 0 or max_bottom < 0:
                    continue
                for bottom in _grid_points(max_bottom, lane_step):
                    for left in _edge_left_points(max_left, x_anchor_count=x_anchor_count):
                        x = int(math.ceil(left - min_x))
                        y = int(math.ceil(bottom - min_y))
                        if not fits_in_bay(bay, bbox, x, y):
                            continue
                        rect = (
                            int(math.floor(x + min_x)),
                            int(math.floor(y + min_y)),
                            int(math.ceil(x + max_x)),
                            int(math.ceil(y + max_y)),
                        )
                        option = _Option(
                            option_id=option_id,
                            block_id=block_id,
                            bay_id=bay_id,
                            orient_idx=orient_idx,
                            x=x,
                            y=y,
                            rect=rect,
                            preference_penalty=penalty,
                            slot=f"bay{bay_id}:edge_lane:{left}:{bottom}",
                        )
                        option_id += 1
                        raw_count += 1
                        center_distance = abs(left * 2 - max_left)
                        rank = (penalty, bay_id, bottom, center_distance, left, height, orient_idx)
                        rows.append((rank, option))
        rows.sort(key=lambda item: item[0])
        if cap_strategy == "balanced":
            options_by_block[block_id] = _balanced_edge_option_cap(
                rows,
                max_options=max_options_per_block,
                n_bays=n_bays,
            )
        else:
            options_by_block[block_id] = _diverse_option_cap(rows, max_options=max_options_per_block, n_bays=n_bays)
    return options_by_block, raw_count


def _edge_left_points(max_left: int, *, x_anchor_count: int) -> list[int]:
    if max_left <= 0:
        return [0]
    count = max(2, x_anchor_count)
    points = {0, max_left}
    for index in range(1, count - 1):
        points.add(int(round(max_left * index / (count - 1))))
    return sorted(points)


def _balanced_edge_option_cap(
    rows: list[tuple[tuple[int, ...], _Option]],
    *,
    max_options: int,
    n_bays: int,
) -> list[_Option]:
    if len(rows) <= max_options:
        return [option for _rank, option in rows]

    selected: list[_Option] = []
    selected_ids: set[int] = set()
    per_bay_quota = max(1, max_options // max(1, n_bays))
    for bay_id in range(n_bays):
        bay_rows = [(rank, option) for rank, option in rows if option.bay_id == bay_id]
        quota = min(per_bay_quota, max_options - len(selected))
        if quota <= 0:
            break
        y_quota = max(1, (quota + 1) // 2)
        _append_spread_by_axis(
            selected,
            selected_ids,
            bay_rows,
            quota=y_quota,
            axis_index=1,
        )
        if len([option for option in selected if option.bay_id == bay_id]) < quota:
            _append_spread_by_axis(
                selected,
                selected_ids,
                bay_rows,
                quota=quota,
                axis_index=0,
            )
        if len([option for option in selected if option.bay_id == bay_id]) < quota:
            _append_ranked_unique_slots(selected, selected_ids, bay_rows, quota=quota)
        if len(selected) >= max_options:
            break

    seen_slots = {option.slot for option in selected}
    for _rank, option in rows:
        if len(selected) >= max_options:
            break
        if option.option_id in selected_ids or option.slot in seen_slots:
            continue
        selected.append(option)
        selected_ids.add(option.option_id)
        seen_slots.add(option.slot)
    if len(selected) < max_options:
        for _rank, option in rows:
            if len(selected) >= max_options:
                break
            if option.option_id not in selected_ids:
                selected.append(option)
                selected_ids.add(option.option_id)
    return selected


def _append_spread_by_axis(
    selected: list[_Option],
    selected_ids: set[int],
    rows: list[tuple[tuple[int, ...], _Option]],
    *,
    quota: int,
    axis_index: int,
) -> None:
    current_count = sum(1 for option in selected if rows and option.bay_id == rows[0][1].bay_id)
    remaining = quota - current_count
    if remaining <= 0:
        return
    by_coord: dict[int, list[tuple[tuple[int, ...], _Option]]] = {}
    for rank, option in rows:
        if option.option_id in selected_ids:
            continue
        coord = option.rect[axis_index]
        by_coord.setdefault(coord, []).append((rank, option))
    if not by_coord:
        return
    coords = _spread_values(sorted(by_coord), remaining)
    for coord in coords:
        if sum(1 for option in selected if option.bay_id == rows[0][1].bay_id) >= quota:
            break
        for _rank, option in by_coord[coord]:
            if option.option_id in selected_ids:
                continue
            selected.append(option)
            selected_ids.add(option.option_id)
            break


def _append_ranked_unique_slots(
    selected: list[_Option],
    selected_ids: set[int],
    rows: list[tuple[tuple[int, ...], _Option]],
    *,
    quota: int,
) -> None:
    if not rows:
        return
    bay_id = rows[0][1].bay_id
    seen_slots = {option.slot for option in selected}
    for _rank, option in rows:
        if sum(1 for chosen in selected if chosen.bay_id == bay_id) >= quota:
            break
        if option.option_id in selected_ids or option.slot in seen_slots:
            continue
        selected.append(option)
        selected_ids.add(option.option_id)
        seen_slots.add(option.slot)


def _spread_values(values: list[int], count: int) -> list[int]:
    if count >= len(values):
        return values
    if count <= 1:
        return [values[0]]
    indexes = {int(round(index * (len(values) - 1) / (count - 1))) for index in range(count)}
    return [values[index] for index in sorted(indexes)]


def _diverse_option_cap(
    rows: list[tuple[tuple[int, ...], _Option]],
    *,
    max_options: int,
    n_bays: int,
) -> list[_Option]:
    if len(rows) <= max_options:
        return [option for _rank, option in rows]

    selected: list[_Option] = []
    selected_ids: set[int] = set()
    per_bay_quota = max(1, max_options // max(1, n_bays))
    for bay_id in range(n_bays):
        count = 0
        seen_slots: set[str] = set()
        for _rank, option in rows:
            if option.bay_id != bay_id or option.option_id in selected_ids:
                continue
            if option.slot in seen_slots:
                continue
            selected.append(option)
            selected_ids.add(option.option_id)
            seen_slots.add(option.slot)
            count += 1
            if count >= per_bay_quota or len(selected) >= max_options:
                break
        if count < per_bay_quota:
            for _rank, option in rows:
                if option.bay_id != bay_id or option.option_id in selected_ids:
                    continue
                selected.append(option)
                selected_ids.add(option.option_id)
                count += 1
                if count >= per_bay_quota or len(selected) >= max_options:
                    break
        if len(selected) >= max_options:
            break

    seen_slots = {option.slot for option in selected}
    for _rank, option in rows:
        if len(selected) >= max_options:
            break
        if option.option_id in selected_ids:
            continue
        if option.slot in seen_slots:
            continue
        selected.append(option)
        selected_ids.add(option.option_id)
        seen_slots.add(option.slot)
    for _rank, option in rows:
        if len(selected) >= max_options:
            break
        if option.option_id in selected_ids:
            continue
        selected.append(option)
        selected_ids.add(option.option_id)
    return selected


def _grid_points(max_value: int, step: int) -> list[int]:
    values = list(range(0, max_value + 1, step))
    if not values or values[-1] != max_value:
        values.append(max_value)
    return sorted(set(values))


def _conflict_pairs(options: list[_Option], *, max_conflict_pairs: int) -> tuple[list[tuple[int, int]], bool]:
    by_bay: dict[int, list[_Option]] = {}
    for option in options:
        by_bay.setdefault(option.bay_id, []).append(option)

    pairs: list[tuple[int, int]] = []
    truncated = False
    for bay_options in by_bay.values():
        for idx, left in enumerate(bay_options):
            for right in bay_options[idx + 1 :]:
                if left.block_id == right.block_id:
                    continue
                if _rect_overlap(left.rect, right.rect):
                    pairs.append((left.option_id, right.option_id))
                    if len(pairs) >= max_conflict_pairs:
                        return pairs, True
    return pairs, truncated


def _conflicts_by_option(conflict_pairs: list[tuple[int, int]]) -> dict[int, set[int]]:
    conflicts: dict[int, set[int]] = {}
    for left, right in conflict_pairs:
        conflicts.setdefault(left, set()).add(right)
        conflicts.setdefault(right, set()).add(left)
    return conflicts


def _rect_overlap(left: tuple[int, int, int, int], right: tuple[int, int, int, int]) -> bool:
    return left[0] < right[2] and right[0] < left[2] and left[1] < right[3] and right[1] < left[3]


def _add_scaled_obj2(model: Any, prob_info: dict[str, Any], options: list[_Option], option_lits: dict[int, Any], *, load_scale: int) -> Any:
    bays = prob_info.get("bays", [])
    blocks = prob_info.get("blocks", [])
    bay_areas = [max(1.0, float(bay["width"]) * float(bay["height"])) for bay in bays]
    avg_area = sum(bay_areas) / len(bay_areas)
    bay_weights = [max(1, int(round(avg_area / area * load_scale))) for area in bay_areas]
    max_load = sum(int(block.get("workload", 0)) for block in blocks)
    normalized_loads = []
    for bay_id in range(len(bays)):
        expr = sum(
            int(blocks[option.block_id].get("workload", 0)) * bay_weights[bay_id] * option_lits[option.option_id]
            for option in options
            if option.bay_id == bay_id
        )
        var = model.NewIntVar(0, max_load * max(bay_weights), f"load_{bay_id}")
        model.Add(var == expr)
        normalized_loads.append(var)

    obj2 = model.NewIntVar(0, max_load * max(bay_weights), "obj2_scaled")
    for left in range(len(normalized_loads)):
        for right in range(left + 1, len(normalized_loads)):
            diff = model.NewIntVar(-max_load * max(bay_weights), max_load * max(bay_weights), f"load_diff_{left}_{right}")
            abs_diff = model.NewIntVar(0, max_load * max(bay_weights), f"abs_load_diff_{left}_{right}")
            model.Add(diff == normalized_loads[left] - normalized_loads[right])
            model.AddAbsEquality(abs_diff, diff)
            model.Add(obj2 >= abs_diff)
    return obj2


def _add_serial_hint(
    model: Any,
    prob_info: dict[str, Any],
    options_by_block: dict[int, list[_Option]],
    option_lits: dict[int, Any],
    starts: dict[int, Any],
    ends: dict[int, Any],
) -> None:
    blocks = prob_info.get("blocks", [])
    cursors: dict[int, int] = {}
    chosen_by_block: dict[int, _Option] = {}
    for block_id, options in options_by_block.items():
        if not options:
            continue
        chosen = options[0]
        chosen_by_block[block_id] = chosen
        for option in options:
            model.AddHint(option_lits[option.option_id], 1 if option.option_id == chosen.option_id else 0)

    for block_id in sorted(
        chosen_by_block,
        key=lambda bid: (
            int(blocks[bid].get("due_date", 0)),
            int(blocks[bid].get("release_time", 0)),
            bid,
        ),
    ):
        block = blocks[block_id]
        chosen = chosen_by_block[block_id]
        release = int(block.get("release_time", 0))
        processing = int(block.get("processing_time", 0))
        start = max(release, cursors.get(chosen.bay_id, 0))
        end = start + processing
        cursors[chosen.bay_id] = end
        model.AddHint(starts[block_id], start)
        model.AddHint(ends[block_id], end)


def _serial_solution_from_options(
    prob_info: dict[str, Any],
    options_by_block: dict[int, list[_Option]],
) -> tuple[dict[str, Any], list[dict[str, int]]]:
    blocks = prob_info.get("blocks", [])
    cursors: dict[int, int] = {}
    assignments: list[dict[str, int]] = []
    for block_id in sorted(
        options_by_block,
        key=lambda bid: (
            int(blocks[bid].get("due_date", 0)),
            int(blocks[bid].get("release_time", 0)),
            bid,
        ),
    ):
        options = options_by_block[block_id]
        if not options:
            continue
        option = options[0]
        release = int(blocks[block_id].get("release_time", 0))
        processing = int(blocks[block_id].get("processing_time", 0))
        start = max(release, cursors.get(option.bay_id, 0))
        end = start + processing
        cursors[option.bay_id] = end
        assignments.append(
            {
                "block_id": block_id,
                "bay_id": option.bay_id,
                "x": option.x,
                "y": option.y,
                "orient_idx": option.orient_idx,
                "entry_time": start,
                "exit_time": end,
            }
        )
    return {"operations": _build_operations(assignments)}, assignments


def _greedy_conflict_graph_solution(
    prob_info: dict[str, Any],
    options_by_block: dict[int, list[_Option]],
    conflicts_by_option: dict[int, set[int]],
) -> tuple[dict[str, Any], list[dict[str, int]], dict[str, str]]:
    best_solution = None
    best_assignments: list[dict[str, int]] | None = None
    best_result = None
    best_meta: dict[str, str] = {}
    block_conflict_scores = _block_conflict_scores(options_by_block, conflicts_by_option)
    for strategy in (
        "edd",
        "release",
        "tight_slack",
        "long_processing",
        "workload_desc",
        "conflict_desc",
        "due_conflict",
        "slack_conflict",
        "block_id",
    ):
        for score_mode in (
            "objective",
            "tardiness",
            "balance",
            "objective_conflict",
            "tardiness_conflict",
            "balance_conflict",
            "objective_due_spread",
            "tardiness_due_spread",
            "balance_due_spread",
        ):
            solution, assignments = _single_greedy_conflict_graph_solution(
                prob_info,
                options_by_block,
                conflicts_by_option,
                block_conflict_scores,
                strategy=strategy,
                score_mode=score_mode,
            )
            solution, assignments = _relocate_tardy_blocks(
                prob_info,
                assignments,
                options_by_block,
                conflicts_by_option,
                max_passes=2,
            )
            result = check_feasibility(prob_info, solution)
            if not result.get("feasible"):
                continue
            if best_result is None or _official_rank(result) < _official_rank(best_result):
                best_solution = solution
                best_assignments = assignments
                best_result = result
                best_meta = {"strategy": strategy, "score_mode": score_mode}

    if best_solution is None or best_assignments is None:
        solution, assignments = _single_greedy_conflict_graph_solution(
            prob_info,
            options_by_block,
            conflicts_by_option,
            block_conflict_scores,
            strategy="edd",
            score_mode="objective",
        )
        return solution, assignments, {"strategy": "edd", "score_mode": "objective"}
    return best_solution, best_assignments, best_meta


def _single_greedy_conflict_graph_solution(
    prob_info: dict[str, Any],
    options_by_block: dict[int, list[_Option]],
    conflicts_by_option: dict[int, set[int]],
    block_conflict_scores: dict[int, float],
    *,
    strategy: str,
    score_mode: str,
) -> tuple[dict[str, Any], list[dict[str, int]]]:
    blocks = prob_info.get("blocks", [])
    weights = prob_info.get("weights", {})
    w1 = float(weights.get("w1", 1.0))
    w2 = float(weights.get("w2", 1.0))
    w3 = float(weights.get("w3", 1.0))
    bay_loads = [0.0 for _ in prob_info.get("bays", [])]
    option_by_id = {
        option.option_id: option
        for options in options_by_block.values()
        for option in options
    }
    placed: list[tuple[int, int, int, int]] = []
    assignments: list[dict[str, int]] = []
    placed_due_by_bay: dict[int, list[tuple[int, int]]] = {}

    for block_id in sorted(
        options_by_block,
        key=lambda bid: _greedy_order_key(blocks, bid, strategy, block_conflict_scores),
    ):
        block = blocks[block_id]
        release = int(block.get("release_time", 0))
        processing = int(block.get("processing_time", 0))
        due = int(block.get("due_date", 0))
        best_key = None
        best_row = None
        for option in options_by_block[block_id]:
            start = _earliest_nonconflicting_start(
                option.option_id,
                release,
                processing,
                placed,
                conflicts_by_option,
            )
            end = start + processing
            tardiness = max(0, end - due)
            projected_load = bay_loads[option.bay_id] + float(block.get("workload", 0))
            load_spread = _projected_load_spread(bay_loads, option.bay_id, projected_load)
            due_crowding = _due_crowding_penalty(
                due,
                option.option_id,
                placed_due_by_bay.get(option.bay_id, []),
                conflicts_by_option,
            )
            key = _greedy_option_key(
                score_mode,
                w1=w1,
                w2=w2,
                w3=w3,
                tardiness=tardiness,
                preference_penalty=option.preference_penalty,
                load_spread=load_spread,
                option_conflicts=len(conflicts_by_option.get(option.option_id, set())),
                due_crowding=due_crowding,
                end=end,
                option_id=option.option_id,
            )
            if best_key is None or key < best_key:
                best_key = key
                best_row = {
                    "block_id": block_id,
                    "bay_id": option.bay_id,
                    "x": option.x,
                    "y": option.y,
                    "orient_idx": option.orient_idx,
                    "entry_time": start,
                    "exit_time": end,
                    "_option_id": option.option_id,
                }
        if best_row is None:
            continue
        option_id = int(best_row["_option_id"])
        placed.append((option_id, int(best_row["entry_time"]), int(best_row["exit_time"]), block_id))
        bay_loads[int(best_row["bay_id"])] += float(block.get("workload", 0))
        placed_due_by_bay.setdefault(int(best_row["bay_id"]), []).append(
            (int(blocks[block_id].get("due_date", 0)), option_id)
        )
        assignments.append(best_row)
    return {"operations": _build_operations(assignments)}, assignments


def _block_conflict_scores(
    options_by_block: dict[int, list[_Option]],
    conflicts_by_option: dict[int, set[int]],
) -> dict[int, float]:
    scores: dict[int, float] = {}
    for block_id, options in options_by_block.items():
        if not options:
            scores[block_id] = 0.0
            continue
        scores[block_id] = sum(len(conflicts_by_option.get(option.option_id, set())) for option in options) / len(options)
    return scores


def _relocate_tardy_blocks(
    prob_info: dict[str, Any],
    assignments: list[dict[str, int]],
    options_by_block: dict[int, list[_Option]],
    conflicts_by_option: dict[int, set[int]],
    *,
    max_passes: int,
) -> tuple[dict[str, Any], list[dict[str, int]]]:
    blocks = prob_info.get("blocks", [])
    current = [dict(row) for row in assignments]
    for _pass in range(max_passes):
        improved = False
        order = sorted(
            range(len(current)),
            key=lambda idx: (
                -max(0, int(current[idx]["exit_time"]) - int(blocks[int(current[idx]["block_id"])]["due_date"])),
                int(blocks[int(current[idx]["block_id"])]["due_date"]),
            ),
        )
        for row_idx in order:
            row = current[row_idx]
            block_id = int(row["block_id"])
            block = blocks[block_id]
            due = int(block.get("due_date", 0))
            current_tardiness = max(0, int(row["exit_time"]) - due)
            if current_tardiness <= 0:
                continue
            placed = [
                (
                    int(other["_option_id"]),
                    int(other["entry_time"]),
                    int(other["exit_time"]),
                    int(other["block_id"]),
                )
                for idx, other in enumerate(current)
                if idx != row_idx and "_option_id" in other
            ]
            candidate_row = _best_relocation_for_block(
                prob_info,
                block_id,
                options_by_block.get(block_id, []),
                placed,
                conflicts_by_option,
                incumbent=row,
            )
            if candidate_row is None:
                continue
            candidate_tardiness = max(0, int(candidate_row["exit_time"]) - due)
            if candidate_tardiness < current_tardiness:
                current[row_idx] = candidate_row
                improved = True
        if not improved:
            break
    solution = {"operations": _build_operations(current)}
    return solution, current


def _polish_fixed_options_schedule_cp(
    prob_info: dict[str, Any],
    assignments: list[dict[str, int]],
    conflicts_by_option: dict[int, set[int]],
    *,
    time_limit: float,
    workers: int,
) -> tuple[dict[str, Any], list[dict[str, int]], dict[str, str]]:
    if time_limit <= 0.0:
        return {"operations": _build_operations(assignments)}, assignments, {"status": "DISABLED"}

    budgets = [float(time_limit)]
    if time_limit > 3.0:
        budgets.insert(0, 3.0)

    best_solution = {"operations": _build_operations(assignments)}
    best_assignments = assignments
    best_result = check_feasibility(prob_info, best_solution)
    statuses = ["HINT"]
    for budget in budgets:
        candidate_solution, candidate_assignments, meta = _solve_fixed_options_schedule_cp_once(
            prob_info,
            best_assignments,
            conflicts_by_option,
            time_limit=budget,
            workers=workers,
        )
        statuses.append(f"{budget:g}s:{meta.get('status')}")
        candidate_result = check_feasibility(prob_info, candidate_solution)
        if candidate_result.get("feasible") and (
            not best_result.get("feasible") or _official_rank(candidate_result) < _official_rank(best_result)
        ):
            best_solution = candidate_solution
            best_assignments = candidate_assignments
            best_result = candidate_result
    return best_solution, best_assignments, {"status": ",".join(statuses)}


def _polish_tardy_option_neighborhood_cp(
    prob_info: dict[str, Any],
    assignments: list[dict[str, int]],
    options_by_block: dict[int, list[_Option]],
    option_by_id: dict[int, _Option],
    conflicts_by_option: dict[int, set[int]],
    *,
    max_flexible_blocks: int,
    time_limit: float,
    workers: int,
    objective_mode: str,
    selection_mode: str,
    max_rounds: int,
) -> tuple[dict[str, Any], list[dict[str, int]], dict[str, Any]]:
    if time_limit <= 0.0 or max_flexible_blocks <= 0:
        return {
            "operations": _build_operations(assignments)
        }, assignments, {
            "status": "DISABLED",
            "flexible_blocks": [],
            "objective_mode": objective_mode,
            "selection_mode": selection_mode,
            "rounds": 0,
        }

    objective_modes = ["tardiness", "objective"] if objective_mode == "both" else [objective_mode]
    selection_modes = ["tardy", "blockers"] if selection_mode == "both" else [selection_mode]
    sizes = sorted({max(1, min(max_flexible_blocks, size)) for size in (4, 8, max_flexible_blocks)})
    best_solution = {"operations": _build_operations(assignments)}
    best_assignments = assignments
    best_result = check_feasibility(prob_info, best_solution)
    current_solution = best_solution
    current_assignments = best_assignments
    current_result = best_result
    statuses = ["HINT"]
    best_flexible: list[int] = []
    best_mode = objective_modes[0]
    best_selection = selection_modes[0]
    rounds = max(1, int(max_rounds))
    per_size_time = max(
        0.1,
        float(time_limit) / (rounds * len(sizes) * len(objective_modes) * len(selection_modes)),
    )
    completed_rounds = 0
    for round_idx in range(rounds):
        round_solution = current_solution
        round_assignments = current_assignments
        round_result = current_result
        round_flexible: list[int] = []
        round_mode = best_mode
        round_selection = best_selection
        for active_selection in selection_modes:
            for mode in objective_modes:
                for size in sizes:
                    candidate_solution, candidate_assignments, meta = _solve_tardy_option_neighborhood_cp_once(
                        prob_info,
                        current_assignments,
                        options_by_block,
                        option_by_id,
                        conflicts_by_option,
                        max_flexible_blocks=size,
                        time_limit=per_size_time,
                        workers=workers,
                        objective_mode=mode,
                        selection_mode=active_selection,
                    )
                    statuses.append(f"r{round_idx + 1}:{active_selection}:{mode}:{size}:{meta.get('status')}")
                    candidate_result = check_feasibility(prob_info, candidate_solution)
                    if candidate_result.get("feasible") and (
                        not round_result.get("feasible") or _official_rank(candidate_result) < _official_rank(round_result)
                    ):
                        round_solution = candidate_solution
                        round_assignments = candidate_assignments
                        round_result = candidate_result
                        round_flexible = list(meta.get("flexible_blocks") or [])
                        round_mode = mode
                        round_selection = active_selection
                    if candidate_result.get("feasible") and (
                        not best_result.get("feasible") or _official_rank(candidate_result) < _official_rank(best_result)
                    ):
                        best_solution = candidate_solution
                        best_assignments = candidate_assignments
                        best_result = candidate_result
                        best_flexible = list(meta.get("flexible_blocks") or [])
                        best_mode = mode
                        best_selection = active_selection
        completed_rounds = round_idx + 1
        if not round_result.get("feasible") or _official_rank(round_result) >= _official_rank(current_result):
            break
        current_solution = round_solution
        current_assignments = round_assignments
        current_result = round_result
        if _official_rank(current_result) == _official_rank(best_result):
            best_flexible = round_flexible
            best_mode = round_mode
            best_selection = round_selection
    return best_solution, best_assignments, {
        "status": ",".join(statuses),
        "flexible_blocks": best_flexible,
        "objective_mode": best_mode if objective_mode == "both" else objective_mode,
        "selection_mode": best_selection if selection_mode == "both" else selection_mode,
        "rounds": completed_rounds,
    }


def _solve_tardy_option_neighborhood_cp_once(
    prob_info: dict[str, Any],
    assignments: list[dict[str, int]],
    options_by_block: dict[int, list[_Option]],
    option_by_id: dict[int, _Option],
    conflicts_by_option: dict[int, set[int]],
    *,
    max_flexible_blocks: int,
    time_limit: float,
    workers: int,
    objective_mode: str,
    selection_mode: str,
) -> tuple[dict[str, Any], list[dict[str, int]], dict[str, Any]]:
    try:
        from ortools.sat.python import cp_model
    except Exception as exc:  # pragma: no cover - environment-specific
        return {"operations": _build_operations(assignments)}, assignments, {
            "status": f"IMPORT_FAILED: {exc}",
            "flexible_blocks": [],
            "selection_mode": selection_mode,
        }

    blocks = prob_info.get("blocks", [])
    assignment_by_block = {int(row["block_id"]): dict(row) for row in assignments}
    flexible = _select_neighborhood_block_ids(
        blocks,
        assignments,
        conflicts_by_option,
        max_flexible_blocks,
        mode=selection_mode,
    )
    if not flexible:
        return {"operations": _build_operations(assignments)}, assignments, {
            "status": "NO_TARDY",
            "flexible_blocks": [],
            "selection_mode": selection_mode,
        }

    model = cp_model.CpModel()
    horizon = _safe_horizon(blocks)
    starts: dict[int, Any] = {}
    ends: dict[int, Any] = {}
    tardiness_vars = []
    fixed_intervals: dict[int, Any] = {}
    option_intervals: dict[int, dict[int, Any]] = {}
    option_lits: dict[int, dict[int, Any]] = {}
    flat_option_lits: dict[int, Any] = {}

    for block_id, row in assignment_by_block.items():
        block = blocks[block_id]
        release = int(block.get("release_time", 0))
        processing = int(block.get("processing_time", 0))
        due = int(block.get("due_date", 0))
        start = model.NewIntVar(release, horizon, f"s_{block_id}")
        end = model.NewIntVar(release + processing, horizon + processing, f"e_{block_id}")
        tardiness = model.NewIntVar(0, horizon + processing, f"t_{block_id}")
        model.Add(end == start + processing)
        model.Add(tardiness >= end - due)
        model.Add(tardiness >= 0)
        model.AddHint(start, int(row["entry_time"]))
        model.AddHint(end, int(row["exit_time"]))
        starts[block_id] = start
        ends[block_id] = end
        tardiness_vars.append(tardiness)

        if block_id in flexible:
            lits = []
            option_intervals[block_id] = {}
            option_lits[block_id] = {}
            current_option_id = int(row.get("_option_id", -1))
            for option in options_by_block.get(block_id, []):
                lit = model.NewBoolVar(f"o_{option.option_id}")
                interval = model.NewOptionalIntervalVar(start, processing, end, lit, f"iv_{option.option_id}")
                option_lits[block_id][option.option_id] = lit
                flat_option_lits[option.option_id] = lit
                option_intervals[block_id][option.option_id] = interval
                lits.append(lit)
                model.AddHint(lit, 1 if option.option_id == current_option_id else 0)
            if not lits:
                interval = model.NewIntervalVar(start, processing, end, f"iv_fixed_{block_id}")
                fixed_intervals[block_id] = interval
            else:
                model.AddExactlyOne(lits)
        else:
            interval = model.NewIntervalVar(start, processing, end, f"iv_fixed_{block_id}")
            fixed_intervals[block_id] = interval

    block_ids = sorted(assignment_by_block)
    for idx, left_id in enumerate(block_ids):
        for right_id in block_ids[idx + 1 :]:
            _add_neighborhood_conflict_constraints(
                model,
                left_id,
                right_id,
                assignment_by_block,
                fixed_intervals,
                option_intervals,
                conflicts_by_option,
            )

    preference_penalty = []
    for block_id, lit_by_option in option_lits.items():
        for option_id, lit in lit_by_option.items():
            preference_penalty.append(int(option_by_id[option_id].preference_penalty) * lit)
    if objective_mode == "objective":
        weights = prob_info.get("weights", {})
        w1 = int(round(float(weights.get("w1", 1.0))))
        w2 = int(round(float(weights.get("w2", 1.0))))
        w3 = int(round(float(weights.get("w3", 1.0))))
        load_scale = 1000
        obj2_scaled = _add_neighborhood_scaled_obj2(
            model,
            prob_info,
            assignment_by_block,
            option_by_id,
            flat_option_lits,
            load_scale=load_scale,
        )
        model.Minimize(
            w1 * load_scale * sum(tardiness_vars)
            + w2 * obj2_scaled
            + w3 * load_scale * sum(preference_penalty)
            + sum(starts.values())
        )
    else:
        model.Minimize(sum(tardiness_vars) * 1000 + sum(starts.values()) + sum(preference_penalty))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = max(0.1, float(time_limit))
    solver.parameters.num_search_workers = max(1, int(workers))
    status = solver.Solve(model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return {"operations": _build_operations(assignments)}, assignments, {
            "status": solver.StatusName(status),
            "flexible_blocks": flexible,
            "selection_mode": selection_mode,
        }

    polished = []
    for block_id in block_ids:
        base = dict(assignment_by_block[block_id])
        if block_id in option_lits:
            chosen_option = None
            for option_id, lit in option_lits[block_id].items():
                if solver.BooleanValue(lit):
                    chosen_option = option_by_id[option_id]
                    break
            if chosen_option is not None:
                base.update(
                    {
                        "bay_id": chosen_option.bay_id,
                        "x": chosen_option.x,
                        "y": chosen_option.y,
                        "orient_idx": chosen_option.orient_idx,
                        "_option_id": chosen_option.option_id,
                    }
                )
        base["entry_time"] = int(solver.Value(starts[block_id]))
        base["exit_time"] = int(solver.Value(ends[block_id]))
        polished.append(base)
    return {"operations": _build_operations(polished)}, polished, {
        "status": solver.StatusName(status),
        "flexible_blocks": flexible,
        "selection_mode": selection_mode,
    }


def _add_neighborhood_scaled_obj2(
    model: Any,
    prob_info: dict[str, Any],
    assignment_by_block: dict[int, dict[str, int]],
    option_by_id: dict[int, _Option],
    option_lits: dict[int, Any],
    *,
    load_scale: int,
) -> Any:
    bays = prob_info.get("bays", [])
    if not bays:
        return 0
    blocks = prob_info.get("blocks", [])
    bay_areas = [max(1, int(round(float(bay["width"]) * float(bay["height"])))) for bay in bays]
    avg_area = sum(bay_areas) / len(bay_areas)
    bay_weights = [max(1, int(round(avg_area / area * load_scale))) for area in bay_areas]
    flexible_blocks = {option_by_id[option_id].block_id for option_id in option_lits}
    max_load = sum(int(block.get("workload", 0)) for block in blocks)

    normalized_loads = []
    for bay_id in range(len(bays)):
        expr = 0
        for block_id, row in assignment_by_block.items():
            if block_id in flexible_blocks:
                continue
            if int(row["bay_id"]) == bay_id:
                expr += int(blocks[block_id].get("workload", 0)) * bay_weights[bay_id]
        for option_id, lit in option_lits.items():
            option = option_by_id[option_id]
            if option.bay_id == bay_id:
                expr += int(blocks[option.block_id].get("workload", 0)) * bay_weights[bay_id] * lit
        var = model.NewIntVar(0, max_load * max(bay_weights), f"n_load_{bay_id}")
        model.Add(var == expr)
        normalized_loads.append(var)

    obj2 = model.NewIntVar(0, max_load * max(bay_weights), "n_obj2_scaled")
    for left in range(len(normalized_loads)):
        for right in range(left + 1, len(normalized_loads)):
            diff = model.NewIntVar(-max_load * max(bay_weights), max_load * max(bay_weights), f"n_load_diff_{left}_{right}")
            abs_diff = model.NewIntVar(0, max_load * max(bay_weights), f"n_abs_load_diff_{left}_{right}")
            model.Add(diff == normalized_loads[left] - normalized_loads[right])
            model.AddAbsEquality(abs_diff, diff)
            model.Add(obj2 >= abs_diff)
    return obj2


def _select_neighborhood_block_ids(
    blocks: list[dict[str, Any]],
    assignments: list[dict[str, int]],
    conflicts_by_option: dict[int, set[int]],
    limit: int,
    *,
    mode: str,
) -> list[int]:
    if mode == "tardy":
        return _top_tardy_block_ids(blocks, assignments, limit)
    if mode != "blockers":
        return _top_tardy_block_ids(blocks, assignments, limit)

    tardy_rows = _tardy_block_rows(blocks, assignments)
    if not tardy_rows or limit <= 0:
        return []

    assignment_by_block = {int(row["block_id"]): row for row in assignments}
    seed_count = max(1, min(len(tardy_rows), min(limit, 8)))
    seeds = [block_id for _tardiness, _due, block_id in tardy_rows[:seed_count]]
    selected: list[int] = list(seeds)
    selected_set = set(selected)
    blocker_scores: dict[int, tuple[int, int, int, int]] = {}

    for seed_id in seeds:
        seed = assignment_by_block[seed_id]
        seed_option = int(seed.get("_option_id", -1))
        seed_conflicts = conflicts_by_option.get(seed_option, set())
        if not seed_conflicts:
            continue
        seed_due = int(blocks[seed_id].get("due_date", 0))
        seed_tardiness = max(0, int(seed["exit_time"]) - seed_due)
        seed_start = int(seed["entry_time"])
        seed_end = int(seed["exit_time"])

        for other in assignments:
            other_id = int(other["block_id"])
            if other_id == seed_id or other_id in selected_set:
                continue
            other_option = int(other.get("_option_id", -1))
            if other_option not in seed_conflicts:
                continue
            overlap = min(seed_end, int(other["exit_time"])) - max(seed_start, int(other["entry_time"]))
            if overlap <= 0:
                continue
            other_due = int(blocks[other_id].get("due_date", 0))
            other_tardiness = max(0, int(other["exit_time"]) - other_due)
            score = (
                seed_tardiness * 1000 + overlap,
                other_tardiness,
                -abs(seed_due - other_due),
                -other_id,
            )
            if score > blocker_scores.get(other_id, (-1, -1, -10**9, -10**9)):
                blocker_scores[other_id] = score

    for block_id, _score in sorted(blocker_scores.items(), key=lambda item: item[1], reverse=True):
        if len(selected) >= limit:
            break
        selected.append(block_id)
        selected_set.add(block_id)

    if len(selected) < limit:
        for _tardiness, _due, block_id in tardy_rows:
            if block_id in selected_set:
                continue
            selected.append(block_id)
            selected_set.add(block_id)
            if len(selected) >= limit:
                break
    return selected


def _tardy_block_rows(blocks: list[dict[str, Any]], assignments: list[dict[str, int]]) -> list[tuple[int, int, int]]:
    rows = []
    for row in assignments:
        block_id = int(row["block_id"])
        tardiness = max(0, int(row["exit_time"]) - int(blocks[block_id].get("due_date", 0)))
        if tardiness <= 0:
            continue
        rows.append((tardiness, -int(blocks[block_id].get("due_date", 0)), block_id))
    rows.sort(reverse=True)
    return rows


def _top_tardy_block_ids(blocks: list[dict[str, Any]], assignments: list[dict[str, int]], limit: int) -> list[int]:
    return [block_id for _tardiness, _due, block_id in _tardy_block_rows(blocks, assignments)[:limit]]


def _add_neighborhood_conflict_constraints(
    model: Any,
    left_id: int,
    right_id: int,
    assignment_by_block: dict[int, dict[str, int]],
    fixed_intervals: dict[int, Any],
    option_intervals: dict[int, dict[int, Any]],
    conflicts_by_option: dict[int, set[int]],
) -> None:
    left_options = option_intervals.get(left_id)
    right_options = option_intervals.get(right_id)
    if left_options is None and right_options is None:
        left_option = int(assignment_by_block[left_id].get("_option_id", -1))
        right_option = int(assignment_by_block[right_id].get("_option_id", -1))
        if right_option in conflicts_by_option.get(left_option, set()):
            model.AddNoOverlap([fixed_intervals[left_id], fixed_intervals[right_id]])
        return

    if left_options is not None and right_options is not None:
        for left_option, left_interval in left_options.items():
            conflicts = conflicts_by_option.get(left_option, set())
            for right_option, right_interval in right_options.items():
                if right_option in conflicts:
                    model.AddNoOverlap([left_interval, right_interval])
        return

    if left_options is not None:
        fixed_option = int(assignment_by_block[right_id].get("_option_id", -1))
        fixed_interval = fixed_intervals[right_id]
        for left_option, left_interval in left_options.items():
            if fixed_option in conflicts_by_option.get(left_option, set()):
                model.AddNoOverlap([left_interval, fixed_interval])
        return

    fixed_option = int(assignment_by_block[left_id].get("_option_id", -1))
    fixed_interval = fixed_intervals[left_id]
    for right_option, right_interval in right_options.items():
        if right_option in conflicts_by_option.get(fixed_option, set()):
            model.AddNoOverlap([fixed_interval, right_interval])


def _solve_fixed_options_schedule_cp_once(
    prob_info: dict[str, Any],
    assignments: list[dict[str, int]],
    conflicts_by_option: dict[int, set[int]],
    *,
    time_limit: float,
    workers: int,
) -> tuple[dict[str, Any], list[dict[str, int]], dict[str, str]]:
    if time_limit <= 0.0:
        return {"operations": _build_operations(assignments)}, assignments, {"status": "DISABLED"}
    try:
        from ortools.sat.python import cp_model
    except Exception as exc:  # pragma: no cover - environment-specific
        return {"operations": _build_operations(assignments)}, assignments, {"status": f"IMPORT_FAILED: {exc}"}

    blocks = prob_info.get("blocks", [])
    if not assignments:
        return {"operations": {}}, [], {"status": "EMPTY"}

    model = cp_model.CpModel()
    horizon = _safe_horizon(blocks)
    starts: dict[int, Any] = {}
    ends: dict[int, Any] = {}
    intervals: dict[int, Any] = {}
    tardiness_vars = []
    option_by_block: dict[int, int] = {}
    assignment_by_block: dict[int, dict[str, int]] = {}

    for row in assignments:
        block_id = int(row["block_id"])
        block = blocks[block_id]
        release = int(block.get("release_time", 0))
        processing = int(block.get("processing_time", 0))
        due = int(block.get("due_date", 0))
        start = model.NewIntVar(release, horizon, f"s_{block_id}")
        end = model.NewIntVar(release + processing, horizon + processing, f"e_{block_id}")
        interval = model.NewIntervalVar(start, processing, end, f"iv_{block_id}")
        tardiness = model.NewIntVar(0, horizon + processing, f"t_{block_id}")
        model.Add(end == start + processing)
        model.Add(tardiness >= end - due)
        model.Add(tardiness >= 0)
        starts[block_id] = start
        ends[block_id] = end
        intervals[block_id] = interval
        tardiness_vars.append(tardiness)
        option_by_block[block_id] = int(row.get("_option_id", -1))
        assignment_by_block[block_id] = row
        model.AddHint(start, int(row["entry_time"]))
        model.AddHint(end, int(row["exit_time"]))

    block_ids = sorted(assignment_by_block)
    for idx, left_id in enumerate(block_ids):
        left_option = option_by_block[left_id]
        if left_option < 0:
            continue
        left_conflicts = conflicts_by_option.get(left_option, set())
        for right_id in block_ids[idx + 1 :]:
            right_option = option_by_block[right_id]
            if right_option in left_conflicts:
                model.AddNoOverlap([intervals[left_id], intervals[right_id]])

    model.Minimize(sum(tardiness_vars) * 1000 + sum(starts.values()))
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = max(0.1, float(time_limit))
    solver.parameters.num_search_workers = max(1, int(workers))
    status = solver.Solve(model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return {"operations": _build_operations(assignments)}, assignments, {"status": solver.StatusName(status)}

    polished = []
    for block_id in block_ids:
        row = dict(assignment_by_block[block_id])
        row["entry_time"] = int(solver.Value(starts[block_id]))
        row["exit_time"] = int(solver.Value(ends[block_id]))
        polished.append(row)
    return {"operations": _build_operations(polished)}, polished, {"status": solver.StatusName(status)}


def _best_fallback_solution(*candidates: tuple[dict[str, Any], dict[str, Any], str]) -> tuple[dict[str, Any] | None, str | None]:
    best_solution = None
    best_source = None
    best_result = None
    for solution, result, source in candidates:
        if not result.get("feasible"):
            continue
        if best_result is None or _official_rank(result) < _official_rank(best_result):
            best_solution = solution
            best_result = result
            best_source = source
    return best_solution, best_source


def _best_relocation_for_block(
    prob_info: dict[str, Any],
    block_id: int,
    options: list[_Option],
    placed: list[tuple[int, int, int, int]],
    conflicts_by_option: dict[int, set[int]],
    *,
    incumbent: dict[str, int],
) -> dict[str, int] | None:
    blocks = prob_info.get("blocks", [])
    block = blocks[block_id]
    release = int(block.get("release_time", 0))
    processing = int(block.get("processing_time", 0))
    due = int(block.get("due_date", 0))
    current_key = (
        max(0, int(incumbent["exit_time"]) - due),
        int(incumbent["exit_time"]),
        0,
        int(incumbent.get("_option_id", 10**9)),
    )
    best_key = current_key
    best_row = None
    for option in options:
        start = _earliest_nonconflicting_start(
            option.option_id,
            release,
            processing,
            placed,
            conflicts_by_option,
        )
        end = start + processing
        key = (
            max(0, end - due),
            end,
            option.preference_penalty,
            option.option_id,
        )
        if key < best_key:
            best_key = key
            best_row = {
                "block_id": block_id,
                "bay_id": option.bay_id,
                "x": option.x,
                "y": option.y,
                "orient_idx": option.orient_idx,
                "entry_time": start,
                "exit_time": end,
                "_option_id": option.option_id,
            }
    return best_row


def _official_rank(result: dict[str, Any]) -> tuple[float, float, float]:
    return (
        float(result.get("objective") or math.inf),
        float(result.get("obj1") or math.inf),
        float(result.get("obj3") or math.inf),
    )


def _greedy_order_key(
    blocks: list[dict[str, Any]],
    block_id: int,
    strategy: str,
    block_conflict_scores: dict[int, float],
) -> tuple:
    block = blocks[block_id]
    release = int(block.get("release_time", 0))
    due = int(block.get("due_date", 0))
    processing = int(block.get("processing_time", 0))
    workload = int(block.get("workload", 0))
    slack = due - release - processing
    conflict_score = float(block_conflict_scores.get(block_id, 0.0))
    if strategy == "release":
        return (release, due, -processing, block_id)
    if strategy == "tight_slack":
        return (slack, due, release, -processing, block_id)
    if strategy == "long_processing":
        return (-processing, due, release, block_id)
    if strategy == "workload_desc":
        return (-workload, due, release, block_id)
    if strategy == "conflict_desc":
        return (-conflict_score, due, slack, block_id)
    if strategy == "due_conflict":
        return (due, -conflict_score, release, block_id)
    if strategy == "slack_conflict":
        return (slack, -conflict_score, due, block_id)
    if strategy == "block_id":
        return (block_id,)
    return (due, release, -processing, block_id)


def _greedy_option_key(
    score_mode: str,
    *,
    w1: float,
    w2: float,
    w3: float,
    tardiness: int,
    preference_penalty: int,
    load_spread: float,
    option_conflicts: int,
    due_crowding: float,
    end: int,
    option_id: int,
) -> tuple[float, float, float, int, int]:
    objective_score = w1 * tardiness + w3 * preference_penalty + w2 * load_spread
    conflict_first = score_mode.endswith("_conflict")
    due_spread_first = score_mode.endswith("_due_spread")
    base_mode = score_mode.removesuffix("_conflict").removesuffix("_due_spread")
    soft_key = preference_penalty
    if conflict_first:
        soft_key = option_conflicts
    elif due_spread_first:
        soft_key = due_crowding
    if base_mode == "tardiness":
        return (tardiness, objective_score, soft_key, end, option_id)
    if base_mode == "balance":
        return (w1 * tardiness + w2 * load_spread, tardiness, soft_key, end, option_id)
    return (objective_score, tardiness, soft_key, end, option_id)


def _due_crowding_penalty(
    due: int,
    option_id: int,
    placed_due_options: list[tuple[int, int]],
    conflicts_by_option: dict[int, set[int]],
) -> float:
    if not placed_due_options:
        return 0.0
    conflicts = conflicts_by_option.get(option_id, set())
    penalty = 0.0
    for other_due, other_option in placed_due_options:
        gap = abs(int(due) - int(other_due))
        if gap >= 200:
            continue
        weight = 2.0 if other_option in conflicts else 0.5
        penalty += weight * (200 - gap)
    return penalty


def _earliest_nonconflicting_start(
    option_id: int,
    release: int,
    processing: int,
    placed: list[tuple[int, int, int, int]],
    conflicts_by_option: dict[int, set[int]],
) -> int:
    conflicts = conflicts_by_option.get(option_id, set())
    start = int(release)
    while True:
        shifted = False
        end = start + processing
        for placed_option, placed_start, placed_end, _block_id in placed:
            if placed_option not in conflicts:
                continue
            if start < placed_end and placed_start < end:
                start = placed_end
                shifted = True
                break
        if not shifted:
            return start


def _projected_load_spread(bay_loads: list[float], bay_id: int, projected_load: float) -> float:
    if not bay_loads:
        return 0.0
    loads = list(bay_loads)
    loads[bay_id] = projected_load
    return max(loads) - min(loads)


def _safe_horizon(blocks: list[dict[str, Any]]) -> int:
    max_release = max(int(block.get("release_time", 0)) for block in blocks)
    max_due = max(int(block.get("due_date", 0)) for block in blocks)
    total_processing = sum(int(block.get("processing_time", 0)) for block in blocks)
    return max(max_due, max_release) + total_processing + 10


def _build_operations(assignments: list[dict[str, int]]) -> dict[str, list[dict[str, int]]]:
    buckets: dict[int, list[tuple[int, int, dict[str, int]]]] = {}
    for row in assignments:
        block_id = int(row["block_id"])
        bay_id = int(row["bay_id"])
        buckets.setdefault(int(row["exit_time"]), []).append(
            (0, block_id, {"type": "EXIT", "block_id": block_id, "bay_id": bay_id})
        )
        buckets.setdefault(int(row["entry_time"]), []).append(
            (
                1,
                block_id,
                {
                    "type": "ENTRY",
                    "block_id": block_id,
                    "bay_id": bay_id,
                    "x": int(row["x"]),
                    "y": int(row["y"]),
                    "orient_idx": int(row["orient_idx"]),
                },
            )
        )
    return {str(time_idx): [item[2] for item in sorted(items)] for time_idx, items in sorted(buckets.items())}


def _slice_instance_by_due_date(prob_info: dict[str, Any], max_blocks: int) -> dict[str, Any]:
    selected = sorted(
        range(len(prob_info.get("blocks", []))),
        key=lambda block_id: (
            int(prob_info["blocks"][block_id].get("due_date", 0)),
            int(prob_info["blocks"][block_id].get("release_time", 0)),
            block_id,
        ),
    )[:max_blocks]
    id_map = {old_id: new_id for new_id, old_id in enumerate(selected)}
    sliced = {
        key: value
        for key, value in prob_info.items()
        if key not in {"blocks", "name"}
    }
    sliced["name"] = f"{prob_info.get('name', 'instance')}_due_prefix_{max_blocks}"
    sliced["blocks"] = [prob_info["blocks"][old_id] for old_id in selected]
    sliced["_source_block_ids"] = {str(id_map[old_id]): old_id for old_id in selected}
    return sliced


if __name__ == "__main__":
    main()
