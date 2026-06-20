"""Diagnose whether tardy blocks are blocked at their desired exit time."""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SUBMISSION_ROOT = ROOT / "ogc_solver"
BASELINE_ROOT = ROOT / "baseline" / "baseline"

sys.path.insert(0, str(BASELINE_ROOT))
sys.path.insert(0, str(SUBMISSION_ROOT))

from utils import Bay, Block, check_exit, check_feasibility  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("instance", type=Path)
    parser.add_argument("--solution", type=Path, default=None)
    parser.add_argument("--timelimit", type=float, default=5.0)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--top", type=int, default=12)
    args = parser.parse_args()

    with args.instance.open(encoding="utf-8") as handle:
        prob_info = json.load(handle)

    if args.solution is None:
        from myalgorithm import algorithm  # noqa: E402

        started_at = time.time()
        solution = algorithm(prob_info, args.timelimit)
        elapsed = time.time() - started_at
    else:
        with args.solution.open(encoding="utf-8") as handle:
            solution = json.load(handle)
        elapsed = None

    result = check_feasibility(prob_info, solution)
    assignments = parse_solution(solution)
    diagnosis = diagnose(prob_info, assignments, result, elapsed=elapsed, top=args.top)

    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("w", encoding="utf-8") as handle:
            json.dump(diagnosis, handle, indent=2)

    print(json.dumps(diagnosis, indent=2))


def parse_solution(solution: dict[str, Any]) -> dict[int, dict[str, Any]]:
    assignments: dict[int, dict[str, Any]] = {}
    operations = solution.get("operations", {})
    for time_key, ops_at_time in operations.items():
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
    return assignments


def diagnose(
    prob_info: dict[str, Any],
    assignments: dict[int, dict[str, Any]],
    result: dict[str, Any],
    *,
    elapsed: float | None,
    top: int,
) -> dict[str, Any]:
    blocks = prob_info.get("blocks", [])
    bays = [Bay.from_dict(data, bay_id) for bay_id, data in enumerate(prob_info.get("bays", []))]

    tardy_records = []
    blocker_counter: Counter[int] = Counter()
    pair_counter: Counter[tuple[int, int]] = Counter()
    ideal_blocker_counter: Counter[int] = Counter()
    ideal_pair_counter: Counter[tuple[int, int]] = Counter()
    cheap_ideal_blocker_counter: Counter[int] = Counter()
    cheap_ideal_pair_counter: Counter[tuple[int, int]] = Counter()
    blocked_tardiness = 0.0
    ideal_blocked_tardiness = 0.0
    cheap_ideal_blocked_tardiness = 0.0
    entry_delay_tardiness = 0.0
    desired_exit_not_earlier = 0
    no_direct_blocker = 0
    no_ideal_blocker = 0

    for block_id, assignment in sorted(assignments.items()):
        if block_id >= len(blocks) or assignment.get("exit_time") is None:
            continue
        block_data = blocks[block_id]
        entry = int(assignment["entry_time"])
        exit_time = int(assignment["exit_time"])
        due = int(block_data["due_date"])
        processing = int(block_data["processing_time"])
        tardiness = max(0, exit_time - due)
        if tardiness <= 0:
            continue

        desired_exit = max(entry + processing, due)
        ideal_entry = max(int(block_data["release_time"]), due - processing)
        ideal_exit = ideal_entry + processing
        entry_delay = max(0, entry - ideal_entry)
        if desired_exit >= exit_time:
            desired_exit_not_earlier += 1
            blockers: list[int] = []
        else:
            blockers = exit_blockers_at(prob_info, assignments, bays, block_id, desired_exit)
            if blockers:
                blocked_tardiness += tardiness
                for blocker_id in blockers:
                    blocker_counter[blocker_id] += 1
                    pair_counter[(block_id, blocker_id)] += 1
            else:
                no_direct_blocker += 1

        ideal_blockers = []
        cheap_ideal_blockers = []
        if ideal_exit < exit_time:
            cheap_ideal_blockers = cheap_exit_blockers_at(prob_info, assignments, target_id=block_id, exit_time=ideal_exit)
            if cheap_ideal_blockers:
                cheap_ideal_blocked_tardiness += tardiness
                for blocker_id in cheap_ideal_blockers:
                    cheap_ideal_blocker_counter[blocker_id] += 1
                    cheap_ideal_pair_counter[(block_id, blocker_id)] += 1
            ideal_blockers = exit_blockers_at(prob_info, assignments, bays, block_id, ideal_exit)
            if ideal_blockers:
                ideal_blocked_tardiness += tardiness
                for blocker_id in ideal_blockers:
                    ideal_blocker_counter[blocker_id] += 1
                    ideal_pair_counter[(block_id, blocker_id)] += 1
            else:
                no_ideal_blocker += 1
        if entry_delay > 0:
            entry_delay_tardiness += tardiness

        tardy_records.append(
            {
                "block_id": block_id,
                "bay_id": int(assignment["bay_id"]),
                "entry_time": entry,
                "exit_time": exit_time,
                "due_date": due,
                "release_time": int(block_data["release_time"]),
                "processing_time": processing,
                "ideal_entry": ideal_entry,
                "ideal_exit": ideal_exit,
                "entry_delay": entry_delay,
                "desired_exit": desired_exit,
                "tardiness": tardiness,
                "direct_exit_blockers": blockers,
                "later_due_blockers": [
                    blocker_id for blocker_id in blockers if int(blocks[blocker_id]["due_date"]) >= due
                ],
                "ideal_exit_blockers": ideal_blockers,
                "ideal_later_due_blockers": [
                    blocker_id for blocker_id in ideal_blockers if int(blocks[blocker_id]["due_date"]) >= due
                ],
                "cheap_ideal_exit_blockers": cheap_ideal_blockers,
                "cheap_ideal_later_due_blockers": [
                    blocker_id for blocker_id in cheap_ideal_blockers if int(blocks[blocker_id]["due_date"]) >= due
                ],
            }
        )

    total_tardiness = sum(record["tardiness"] for record in tardy_records)
    return {
        "instance": prob_info.get("name"),
        "elapsed": None if elapsed is None else round(elapsed, 3),
        "feasible": result.get("feasible"),
        "stage": result.get("stage"),
        "objective": result.get("objective"),
        "obj1": result.get("obj1"),
        "obj2": result.get("obj2"),
        "obj3": result.get("obj3"),
        "tardy_blocks": len(tardy_records),
        "total_tardiness": total_tardiness,
        "direct_blocked_tardy_blocks": sum(1 for record in tardy_records if record["direct_exit_blockers"]),
        "direct_blocked_tardiness": blocked_tardiness,
        "direct_blocked_tardiness_share": blocked_tardiness / total_tardiness if total_tardiness else None,
        "ideal_blocked_tardy_blocks": sum(1 for record in tardy_records if record["ideal_exit_blockers"]),
        "ideal_blocked_tardiness": ideal_blocked_tardiness,
        "ideal_blocked_tardiness_share": ideal_blocked_tardiness / total_tardiness if total_tardiness else None,
        "cheap_ideal_blocked_tardy_blocks": sum(
            1 for record in tardy_records if record["cheap_ideal_exit_blockers"]
        ),
        "cheap_ideal_blocked_tardiness": cheap_ideal_blocked_tardiness,
        "cheap_ideal_blocked_tardiness_share": (
            cheap_ideal_blocked_tardiness / total_tardiness if total_tardiness else None
        ),
        "entry_delayed_tardy_blocks": sum(1 for record in tardy_records if record["entry_delay"] > 0),
        "entry_delay_tardiness": entry_delay_tardiness,
        "entry_delay_tardiness_share": entry_delay_tardiness / total_tardiness if total_tardiness else None,
        "desired_exit_not_earlier_count": desired_exit_not_earlier,
        "no_direct_blocker_count": no_direct_blocker,
        "no_ideal_blocker_count": no_ideal_blocker,
        "top_blockers": [
            {"blocker": block_id, "count": count}
            for block_id, count in blocker_counter.most_common(top)
        ],
        "top_pairs": [
            {"victim": victim, "blocker": blocker, "count": count}
            for (victim, blocker), count in pair_counter.most_common(top)
        ],
        "top_ideal_blockers": [
            {"blocker": block_id, "count": count}
            for block_id, count in ideal_blocker_counter.most_common(top)
        ],
        "top_ideal_pairs": [
            {"victim": victim, "blocker": blocker, "count": count}
            for (victim, blocker), count in ideal_pair_counter.most_common(top)
        ],
        "top_cheap_ideal_blockers": [
            {"blocker": block_id, "count": count}
            for block_id, count in cheap_ideal_blocker_counter.most_common(top)
        ],
        "top_cheap_ideal_pairs": [
            {"victim": victim, "blocker": blocker, "count": count}
            for (victim, blocker), count in cheap_ideal_pair_counter.most_common(top)
        ],
        "top_tardy_blocks": sorted(tardy_records, key=lambda row: row["tardiness"], reverse=True)[:top],
    }


def exit_blockers_at(
    prob_info: dict[str, Any],
    assignments: dict[int, dict[str, Any]],
    bays: list[Bay],
    target_id: int,
    exit_time: int,
) -> list[int]:
    blocks = prob_info["blocks"]
    target_assignment = assignments[target_id]
    bay_id = int(target_assignment["bay_id"])
    target_block = make_block(blocks, target_id, target_assignment)
    present = [target_block]
    for other_id, other_assignment in assignments.items():
        if other_id == target_id or int(other_assignment.get("bay_id", -1)) != bay_id:
            continue
        if int(other_assignment["entry_time"]) < exit_time < int(other_assignment["exit_time"]):
            present.append(make_block(blocks, other_id, other_assignment))

    blockers = []
    seen = set()
    for obstruction in check_exit(bays[bay_id], present, target_block, fast=False):
        blocker_id = int(obstruction.existing_block.block_id)
        if blocker_id == target_id or blocker_id in seen:
            continue
        seen.add(blocker_id)
        blockers.append(blocker_id)
    return blockers


def make_block(blocks: list[dict[str, Any]], block_id: int, assignment: dict[str, Any]) -> Block:
    return Block(
        block_id,
        blocks[block_id],
        x=int(assignment["x"]),
        y=int(assignment["y"]),
        orient_idx=int(assignment["orient_idx"]),
    )


def cheap_exit_blockers_at(
    prob_info: dict[str, Any],
    assignments: dict[int, dict[str, Any]],
    *,
    target_id: int,
    exit_time: int,
) -> list[int]:
    """Return an over-approximation of exit blockers without Shapely.

    `check_exit` reports an obstruction when some target layer `k` overlaps a
    same-or-higher existing layer `j >= k`.  This proxy avoids Shapely by using
    layer bounding boxes plus a convex-polygon separating-axis test.  It is still
    an approximation if future shapes are non-convex, but it is much tighter
    than whole-layer AABB overlap.
    """

    blocks = prob_info["blocks"]
    target_assignment = assignments[target_id]
    bay_id = int(target_assignment["bay_id"])
    target_block = make_block(blocks, target_id, target_assignment)
    target_layers = target_block.layers_at_pos()
    target_layer_boxes = _layer_boxes(target_layers)

    blockers = []
    for other_id, other_assignment in assignments.items():
        if other_id == target_id or int(other_assignment.get("bay_id", -1)) != bay_id:
            continue
        if not (int(other_assignment["entry_time"]) < exit_time < int(other_assignment["exit_time"])):
            continue
        other_block = make_block(blocks, other_id, other_assignment)
        other_layers = other_block.layers_at_pos()
        if _cheap_exit_overlap(
            target_layers,
            target_layer_boxes,
            other_layers,
            _layer_boxes(other_layers),
        ):
            blockers.append(other_id)
    return blockers


def _cheap_exit_overlap(
    target_layers: list[list],
    target_boxes: list[tuple[float, float, float, float] | None],
    other_layers: list[list],
    other_boxes: list[tuple[float, float, float, float] | None],
) -> bool:
    for k, target_box in enumerate(target_boxes):
        if target_box is None:
            continue
        for j in range(k, len(other_boxes)):
            other_box = other_boxes[j]
            if (
                other_box is not None
                and _box_overlap(target_box, other_box)
                and _convex_polygons_overlap(target_layers[k], other_layers[j])
            ):
                return True
    return False


def _layer_boxes(layers: list[list]) -> list[tuple[float, float, float, float] | None]:
    boxes = []
    for layer in layers:
        if not layer:
            boxes.append(None)
            continue
        xs = [float(point[0]) for point in layer]
        ys = [float(point[1]) for point in layer]
        boxes.append((min(xs), min(ys), max(xs), max(ys)))
    return boxes


def _box_overlap(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> bool:
    return first[0] < second[2] and second[0] < first[2] and first[1] < second[3] and second[1] < first[3]


def _convex_polygons_overlap(first: list, second: list, *, eps: float = 1e-9) -> bool:
    if len(first) < 3 or len(second) < 3:
        return False
    first_points = [(float(point[0]), float(point[1])) for point in first]
    second_points = [(float(point[0]), float(point[1])) for point in second]
    for axis in _polygon_axes(first_points):
        if not _projection_overlaps(first_points, second_points, axis, eps=eps):
            return False
    for axis in _polygon_axes(second_points):
        if not _projection_overlaps(first_points, second_points, axis, eps=eps):
            return False
    return True


def _polygon_axes(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    axes = []
    for idx, (x1, y1) in enumerate(points):
        x2, y2 = points[(idx + 1) % len(points)]
        edge_x = x2 - x1
        edge_y = y2 - y1
        length = (edge_x * edge_x + edge_y * edge_y) ** 0.5
        if length <= 1e-12:
            continue
        axes.append((-edge_y / length, edge_x / length))
    return axes


def _projection_overlaps(
    first: list[tuple[float, float]],
    second: list[tuple[float, float]],
    axis: tuple[float, float],
    *,
    eps: float,
) -> bool:
    ax, ay = axis
    first_values = [x * ax + y * ay for x, y in first]
    second_values = [x * ax + y * ay for x, y in second]
    return min(max(first_values), max(second_values)) - max(min(first_values), min(second_values)) > eps


if __name__ == "__main__":
    main()
