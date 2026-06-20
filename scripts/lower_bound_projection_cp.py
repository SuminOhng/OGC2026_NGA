"""Compute projection-resource energetic lower bounds for OGC obj1.

This is an analysis tool, not submitted solver logic. It strengthens the
bay-area relaxation with simple 2D packing necessary conditions:

- If a block is taller than half of any bay where it is placed, then tall blocks
  in that bay cannot be stacked vertically and must consume x-width capacity.
- If a block is wider than half of any bay where it is placed, then wide blocks
  in that bay cannot be placed side-by-side and must consume y-height capacity.

For a bay subset and time interval, the tool computes compulsory on-time
processing and checks these projection capacities. The resulting excess divided
by the largest possible demand gives a conservative total-tardiness lower bound.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from batch_eval import _compulsory_processing_in_interval, _orientation_size, objective_lower_bound  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("instance", type=Path)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    with args.instance.open(encoding="utf-8") as handle:
        prob_info = json.load(handle)

    result = projection_energetic_lb(prob_info)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("w", encoding="utf-8") as handle:
            json.dump(result, handle, indent=2)
    print(json.dumps(result, indent=2))


def projection_energetic_lb(prob_info: dict[str, Any]) -> dict[str, Any]:
    blocks = prob_info.get("blocks", [])
    bays = prob_info.get("bays", [])
    if not blocks or not bays or len(bays) > 20:
        return {"instance": prob_info.get("name"), "available": False, "error": "empty or too many bays"}

    infos = _projection_infos(prob_info)
    time_points = sorted({row["release"] for row in infos} | {row["due"] for row in infos})
    if len(time_points) < 2:
        return {"instance": prob_info.get("name"), "available": True, "projection_obj1_lb": 0.0}

    best_x = _best_projection_lb(infos, bays, time_points, kind="x")
    best_y = _best_projection_lb(infos, bays, time_points, kind="y")
    base = objective_lower_bound(prob_info)
    combined_obj1_lb = max(float(base["lower_bound_obj1"]), best_x["bound"], best_y["bound"])
    w1 = float(prob_info.get("weights", {}).get("w1", 1.0))
    combined_weighted_lb = w1 * combined_obj1_lb + float(base["lower_bound_secondary"])
    return {
        "instance": prob_info.get("name"),
        "available": True,
        "base_obj1_lb": base["lower_bound_obj1"],
        "base_weighted_lb": base["lower_bound"],
        "projection_x_obj1_lb": best_x["bound"],
        "projection_x_witness": best_x["witness"],
        "projection_y_obj1_lb": best_y["bound"],
        "projection_y_witness": best_y["witness"],
        "combined_obj1_lb": combined_obj1_lb,
        "combined_weighted_lb": combined_weighted_lb,
    }


def _projection_infos(prob_info: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    bays = prob_info.get("bays", [])
    for block in prob_info.get("blocks", []):
        options = []
        for bay_id, bay in enumerate(bays):
            bay_width = float(bay.get("width", 0.0))
            bay_height = float(bay.get("height", 0.0))
            for orientation in block.get("shape", []):
                width, height = _orientation_size(orientation)
                if width <= bay_width + 1e-9 and height <= bay_height + 1e-9:
                    x_demand = width if height > bay_height * 0.5 else 0.0
                    y_demand = height if width > bay_width * 0.5 else 0.0
                    options.append(
                        {
                            "bay_id": bay_id,
                            "x_demand": float(x_demand),
                            "y_demand": float(y_demand),
                        }
                    )
        if not options:
            continue
        fit_mask = 0
        for option in options:
            fit_mask |= 1 << int(option["bay_id"])
        rows.append(
            {
                "release": int(block.get("release_time", 0)),
                "due": int(block.get("due_date", 0)),
                "processing": int(block.get("processing_time", 0)),
                "fit_mask": fit_mask,
                "options": options,
            }
        )
    return rows


def _best_projection_lb(
    infos: list[dict[str, Any]],
    bays: list[dict[str, Any]],
    time_points: list[int],
    *,
    kind: str,
) -> dict[str, Any]:
    n_bays = len(bays)
    full_mask = (1 << n_bays) - 1
    best = 0.0
    best_witness = None
    for subset_mask in range(1, full_mask + 1):
        capacity = _subset_projection_capacity(bays, subset_mask, kind)
        if capacity <= 0:
            continue
        subset_jobs = [row for row in infos if row["fit_mask"] & ~subset_mask == 0]
        if not subset_jobs:
            continue
        for start_index, start in enumerate(time_points):
            for end in time_points[start_index + 1 :]:
                interval_length = end - start
                if interval_length <= 0:
                    continue
                demand = 0.0
                max_demand = 0.0
                active_jobs = 0
                for row in subset_jobs:
                    compulsory = _compulsory_processing_in_interval(
                        start,
                        end,
                        row["release"],
                        row["due"],
                        row["processing"],
                    )
                    if compulsory <= 0:
                        continue
                    projection_demand = _min_projection_demand(row["options"], subset_mask, kind)
                    if projection_demand <= 0:
                        continue
                    active_jobs += 1
                    demand += projection_demand * compulsory
                    max_demand = max(max_demand, projection_demand)
                if demand <= 0 or max_demand <= 0:
                    continue
                excess = demand - capacity * interval_length
                if excess > 0:
                    bound = excess / max_demand
                    if bound > best:
                        best = bound
                        best_witness = {
                            "subset_mask": subset_mask,
                            "start": start,
                            "end": end,
                            "capacity": capacity,
                            "demand": demand,
                            "active_jobs": active_jobs,
                            "max_demand": max_demand,
                        }
    return {"bound": best, "witness": best_witness}


def _subset_projection_capacity(bays: list[dict[str, Any]], subset_mask: int, kind: str) -> float:
    key = "width" if kind == "x" else "height"
    return sum(float(bay.get(key, 0.0)) for bay_id, bay in enumerate(bays) if subset_mask & (1 << bay_id))


def _min_projection_demand(options: list[dict[str, Any]], subset_mask: int, kind: str) -> float:
    key = f"{kind}_demand"
    candidates = [
        float(option[key])
        for option in options
        if subset_mask & (1 << int(option["bay_id"]))
    ]
    if not candidates:
        return 0.0
    return min(candidates)


if __name__ == "__main__":
    main()
