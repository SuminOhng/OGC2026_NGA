"""Evaluate ogc_solver on all training instances."""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import time
from fractions import Fraction
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SUBMISSION_ROOT = ROOT / "ogc_solver"
BASELINE_ROOT = ROOT / "baseline" / "baseline"

sys.path.insert(0, str(BASELINE_ROOT))
sys.path.insert(0, str(SUBMISSION_ROOT))


def natural_key(path: Path) -> tuple:
    stem = path.stem
    suffix = stem.split("_")[-1]
    return (stem.rsplit("_", 1)[0], int(suffix) if suffix.isdigit() else suffix)


def selected_instances(train_dir: Path, names: list[str]) -> list[Path]:
    paths = sorted(train_dir.glob("*.json"), key=natural_key)
    if not names:
        return paths

    selected = []
    by_name = {path.stem: path for path in paths}
    for name in names:
        stem = Path(name).stem
        if stem not in by_name:
            raise SystemExit(f"Unknown instance: {name}")
        selected.append(by_name[stem])
    return selected


def objective_lower_bound(
    prob_info: dict,
    *,
    secondary_cp_time_limit: float = 0.0,
    secondary_cp_workers: int = 8,
) -> dict:
    """Return a conservative weighted objective lower bound.

    The bound is intentionally optimistic:
    - obj1 uses only the mandatory processing window ``release + processing``.
    - obj1 also uses a continuous area-time relaxation over compatible bay sets.
    - for two-bay instances, obj2+obj3 uses an exact assignment relaxation.
    - otherwise obj2 is bounded by zero and obj3 uses the independent best bay,
      unless an optional CP-SAT assignment bound is requested.
    """

    weights = prob_info.get("weights", {})
    w1 = float(weights.get("w1", 1.0))

    release_processing_obj1_lb = 0.0
    for block in prob_info.get("blocks", []):
        release = int(block.get("release_time", 0))
        processing = int(block.get("processing_time", 0))
        due = int(block.get("due_date", 0))
        release_processing_obj1_lb += max(0, release + processing - due)

    area_time_obj1_lb = _area_time_obj1_lower_bound(prob_info)
    energetic_obj1_lb = _energetic_obj1_lower_bound(prob_info)
    obj1_lb = max(release_processing_obj1_lb, area_time_obj1_lb, energetic_obj1_lb)
    secondary_lb = _secondary_assignment_lower_bound(
        prob_info,
        cp_time_limit=secondary_cp_time_limit,
        cp_workers=secondary_cp_workers,
    )
    obj2_lb = secondary_lb["obj2"]
    obj3_lb = secondary_lb["obj3"]
    objective_lb = w1 * obj1_lb + secondary_lb["weighted"]
    return {
        "lower_bound": objective_lb,
        "lower_bound_obj1": obj1_lb,
        "lower_bound_obj1_release_processing": release_processing_obj1_lb,
        "lower_bound_obj1_area_time": area_time_obj1_lb,
        "lower_bound_obj1_energetic": energetic_obj1_lb,
        "lower_bound_obj2": obj2_lb,
        "lower_bound_obj3": obj3_lb,
        "lower_bound_secondary": secondary_lb["weighted"],
        "lower_bound_secondary_method": secondary_lb["method"],
        "lower_bound_secondary_status": secondary_lb.get("status"),
        "lower_bound_secondary_cp_obj2_incumbent": secondary_lb.get("cp_obj2_incumbent"),
        "lower_bound_secondary_cp_obj3_incumbent": secondary_lb.get("cp_obj3_incumbent"),
    }


def _bbox_fit_bays(block: dict, bays: list[dict]) -> list[int]:
    fit_bays = []
    orientation_sizes = [_orientation_size(orientation) for orientation in block.get("shape", [])]
    for bay_id, bay in enumerate(bays):
        bay_width = float(bay.get("width", 0.0))
        bay_height = float(bay.get("height", 0.0))
        if any(width <= bay_width and height <= bay_height for width, height in orientation_sizes):
            fit_bays.append(bay_id)
    return fit_bays


def _area_time_obj1_lower_bound(prob_info: dict) -> float:
    """Continuous footprint-capacity relaxation for total tardiness.

    For any bay subset S and time window [a, d], every block whose compatible
    bay set is contained in S, with release >= a and due <= d, must place its
    minimum base-layer area times processing duration inside S if it is on time.
    If that area-time demand exceeds total bay area * (d-a), the excess must
    spill after d. Dividing by the largest possible base-layer area in that
    group gives a conservative lower bound on total tardiness.
    """

    blocks = prob_info.get("blocks", [])
    bays = prob_info.get("bays", [])
    n_bays = len(bays)
    if not blocks or not bays or n_bays > 20:
        return 0.0

    bay_areas = [float(bay.get("width", 0.0)) * float(bay.get("height", 0.0)) for bay in bays]
    block_infos = []
    for block in blocks:
        orientation_infos = []
        for orientation in block.get("shape", []):
            width, height = _orientation_size(orientation)
            bay_mask = 0
            for bay_id, bay in enumerate(bays):
                if width <= float(bay.get("width", 0.0)) and height <= float(bay.get("height", 0.0)):
                    bay_mask |= 1 << bay_id
            if bay_mask:
                orientation_infos.append((bay_mask, max(0.0, _base_layer_area(orientation))))
        if not orientation_infos:
            continue
        fit_mask = 0
        for bay_mask, _area in orientation_infos:
            fit_mask |= bay_mask
        block_infos.append(
            {
                "release": int(block.get("release_time", 0)),
                "due": int(block.get("due_date", 0)),
                "processing": int(block.get("processing_time", 0)),
                "fit_mask": fit_mask,
                "orientations": orientation_infos,
            }
        )

    if not block_infos:
        return 0.0

    best_lb = 0.0
    full_mask = (1 << n_bays) - 1
    for subset_mask in range(1, full_mask + 1):
        capacity = sum(bay_areas[bay_id] for bay_id in range(n_bays) if subset_mask & (1 << bay_id))
        if capacity <= 0:
            continue
        subset_jobs = [info for info in block_infos if info["fit_mask"] & ~subset_mask == 0]
        if not subset_jobs:
            continue
        releases = sorted({info["release"] for info in subset_jobs})
        dues = sorted({info["due"] for info in subset_jobs})
        for start in releases:
            for due in dues:
                if due <= start:
                    continue
                demand = 0.0
                max_area = 0.0
                for info in subset_jobs:
                    if info["release"] < start or info["due"] > due:
                        continue
                    feasible_areas = [area for bay_mask, area in info["orientations"] if bay_mask & subset_mask]
                    if not feasible_areas:
                        continue
                    min_area = min(feasible_areas)
                    max_area = max(max_area, max(feasible_areas))
                    demand += min_area * info["processing"]
                if demand <= 0 or max_area <= 0:
                    continue
                excess = demand - capacity * (due - start)
                if excess > 0:
                    best_lb = max(best_lb, excess / max_area)
    return best_lb


def _energetic_obj1_lower_bound(prob_info: dict) -> float:
    """Energetic cumulative-resource relaxation for total tardiness.

    For a block with release r, due d, and processing p, the minimum processing
    amount that must happen inside an interval [a, b] if the block exits on time
    is:

        max(0, p - max(0, a-r) - max(0, d-b))

    Multiplying this compulsory processing by a relaxed footprint area gives
    compulsory area-time demand. Any demand exceeding bay-subset area capacity
    must be relieved by tardiness. One unit of tardiness can reduce at most one
    time unit of compulsory processing for one block, so excess divided by the
    largest footprint area in the interval is a conservative total-tardiness LB.
    """

    blocks = prob_info.get("blocks", [])
    bays = prob_info.get("bays", [])
    n_bays = len(bays)
    if not blocks or not bays or n_bays > 20:
        return 0.0

    bay_areas = [float(bay.get("width", 0.0)) * float(bay.get("height", 0.0)) for bay in bays]
    block_infos = _block_energy_infos(prob_info)
    if not block_infos:
        return 0.0

    time_points = sorted({info["release"] for info in block_infos} | {info["due"] for info in block_infos})
    if len(time_points) < 2:
        return 0.0

    best_lb = 0.0
    full_mask = (1 << n_bays) - 1
    for subset_mask in range(1, full_mask + 1):
        capacity = sum(bay_areas[bay_id] for bay_id in range(n_bays) if subset_mask & (1 << bay_id))
        if capacity <= 0:
            continue
        subset_jobs = [info for info in block_infos if info["fit_mask"] & ~subset_mask == 0]
        if not subset_jobs:
            continue
        for start_index, start in enumerate(time_points):
            for end in time_points[start_index + 1 :]:
                interval_length = end - start
                if interval_length <= 0:
                    continue
                demand = 0.0
                max_area = 0.0
                for info in subset_jobs:
                    compulsory = _compulsory_processing_in_interval(
                        start,
                        end,
                        info["release"],
                        info["due"],
                        info["processing"],
                    )
                    if compulsory <= 0:
                        continue
                    feasible_areas = [area for bay_mask, area in info["orientations"] if bay_mask & subset_mask]
                    if not feasible_areas:
                        continue
                    min_area = min(feasible_areas)
                    max_area = max(max_area, max(feasible_areas))
                    demand += min_area * compulsory
                if demand <= 0 or max_area <= 0:
                    continue
                excess = demand - capacity * interval_length
                if excess > 0:
                    best_lb = max(best_lb, excess / max_area)
    return best_lb


def _block_energy_infos(prob_info: dict) -> list[dict]:
    blocks = prob_info.get("blocks", [])
    bays = prob_info.get("bays", [])
    infos = []
    for block in blocks:
        orientation_infos = []
        for orientation in block.get("shape", []):
            width, height = _orientation_size(orientation)
            bay_mask = 0
            for bay_id, bay in enumerate(bays):
                if width <= float(bay.get("width", 0.0)) and height <= float(bay.get("height", 0.0)):
                    bay_mask |= 1 << bay_id
            if bay_mask:
                orientation_infos.append((bay_mask, max(0.0, _base_layer_area(orientation))))
        if not orientation_infos:
            continue
        fit_mask = 0
        for bay_mask, _area in orientation_infos:
            fit_mask |= bay_mask
        infos.append(
            {
                "release": int(block.get("release_time", 0)),
                "due": int(block.get("due_date", 0)),
                "processing": int(block.get("processing_time", 0)),
                "fit_mask": fit_mask,
                "orientations": orientation_infos,
            }
        )
    return infos


def _compulsory_processing_in_interval(
    interval_start: int,
    interval_end: int,
    release: int,
    due: int,
    processing: int,
) -> float:
    before_interval = max(0, interval_start - release)
    after_interval = max(0, due - interval_end)
    return max(0.0, float(processing - before_interval - after_interval))


def _secondary_assignment_lower_bound(
    prob_info: dict,
    *,
    cp_time_limit: float = 0.0,
    cp_workers: int = 8,
) -> dict:
    blocks = prob_info.get("blocks", [])
    bays = prob_info.get("bays", [])
    weights = prob_info.get("weights", {})
    w2 = float(weights.get("w2", 1.0))
    w3 = float(weights.get("w3", 1.0))
    if len(bays) == 2:
        return _two_bay_secondary_assignment_lower_bound(prob_info)

    obj3 = _independent_preference_lower_bound(blocks, bays)
    fallback = {
        "obj2": 0.0,
        "obj3": obj3,
        "weighted": w2 * 0.0 + w3 * obj3,
        "method": "independent_preference",
    }
    if cp_time_limit > 0 and len(bays) >= 2:
        cp_bound = _cp_secondary_assignment_lower_bound(
            prob_info,
            fallback=fallback,
            time_limit=cp_time_limit,
            workers=cp_workers,
        )
        if cp_bound is not None:
            return cp_bound
    return fallback


def _cp_secondary_assignment_lower_bound(
    prob_info: dict,
    *,
    fallback: dict,
    time_limit: float,
    workers: int,
) -> dict | None:
    try:
        from ortools.sat.python import cp_model
    except Exception:
        return None

    blocks = prob_info.get("blocks", [])
    bays = prob_info.get("bays", [])
    if not blocks or not bays:
        return None

    weights = prob_info.get("weights", {})
    w2 = int(weights.get("w2", 1))
    w3 = int(weights.get("w3", 1))
    n_bays = len(bays)
    model = cp_model.CpModel()
    load_terms: list[list[tuple]] = [[] for _ in bays]
    obj3_terms: list[tuple] = []

    for block_id, block in enumerate(blocks):
        workload = int(block.get("workload", 0))
        preferences = [int(value) for value in block.get("bay_preferences", [])]
        max_preference = max(preferences) if preferences else 0
        fit_bays = _bbox_fit_bays(block, bays)
        if not fit_bays:
            fit_bays = list(range(min(n_bays, len(preferences)))) or list(range(n_bays))

        presences = []
        for bay_id in fit_bays:
            var = model.NewBoolVar(f"x_{block_id}_{bay_id}")
            presences.append(var)
            load_terms[bay_id].append((var, workload))
            preference = preferences[bay_id] if bay_id < len(preferences) else 0
            obj3_terms.append((var, max_preference - preference))
        model.AddExactlyOne(presences)

    total_workload = sum(int(block.get("workload", 0)) for block in blocks)
    loads = [model.NewIntVar(0, total_workload, f"load_{bay_id}") for bay_id in range(n_bays)]
    for bay_id, terms in enumerate(load_terms):
        model.Add(loads[bay_id] == sum(var * coeff for var, coeff in terms))

    obj3_upper = sum(max(0, coeff) for _var, coeff in obj3_terms)
    obj3 = model.NewIntVar(0, obj3_upper, "obj3")
    model.Add(obj3 == sum(var * coeff for var, coeff in obj3_terms))

    coeffs, denominator = _normalized_load_coefficients(bays)
    max_scaled_diff = max(coeffs) * total_workload if coeffs else 0
    obj2 = model.NewIntVar(0, max(0, max_scaled_diff // denominator + 1), "obj2")
    for left in range(n_bays):
        for right in range(left + 1, n_bays):
            diff = model.NewIntVar(0, max_scaled_diff, f"diff_{left}_{right}")
            model.AddAbsEquality(diff, coeffs[left] * loads[left] - coeffs[right] * loads[right])
            model.Add(diff <= obj2 * denominator + denominator - 1)

    model.Minimize(w2 * obj2 + w3 * obj3)
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = max(0.1, float(time_limit))
    solver.parameters.num_search_workers = max(1, int(workers))
    status = solver.Solve(model)

    weighted_bound = max(float(fallback["weighted"]), float(solver.BestObjectiveBound()))
    result = dict(fallback)
    result["weighted"] = weighted_bound
    result["method"] = "cp_sat_assignment_bound"
    result["status"] = solver.StatusName(status)
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        result["cp_obj2_incumbent"] = float(solver.Value(obj2))
        result["cp_obj3_incumbent"] = float(solver.Value(obj3))
    return result


def _two_bay_secondary_assignment_lower_bound(prob_info: dict) -> dict:
    blocks = prob_info.get("blocks", [])
    bays = prob_info.get("bays", [])
    weights = prob_info.get("weights", {})
    w2 = float(weights.get("w2", 1.0))
    w3 = float(weights.get("w3", 1.0))
    if len(bays) != 2:
        raise ValueError("two-bay assignment lower bound requires exactly two bays")

    dp: dict[int, float] = {0: 0.0}
    total_workload = 0
    for block in blocks:
        workload = int(block.get("workload", 0))
        total_workload += workload
        preferences = [float(value) for value in block.get("bay_preferences", [])]
        if len(preferences) < 2:
            continue
        max_preference = max(preferences)
        penalties = [max_preference - preferences[0], max_preference - preferences[1]]
        fit_bays = _bbox_fit_bays(block, bays)
        if not fit_bays:
            fit_bays = [0, 1]

        next_dp: dict[int, float] = {}
        for load0, penalty in dp.items():
            if 0 in fit_bays:
                _dp_min_update(next_dp, load0 + workload, penalty + penalties[0])
            if 1 in fit_bays:
                _dp_min_update(next_dp, load0, penalty + penalties[1])
        dp = next_dp

    bay_areas = [float(bay["width"]) * float(bay["height"]) for bay in bays]
    avg_area = sum(bay_areas) / 2.0
    bay_weights = [avg_area / area if area > 0 else 1.0 for area in bay_areas]
    best_weighted = None
    best_obj2 = 0.0
    best_obj3 = 0.0
    for load0, obj3 in dp.items():
        load1 = total_workload - load0
        obj2 = int(abs(bay_weights[0] * load0 - bay_weights[1] * load1))
        weighted = w2 * obj2 + w3 * obj3
        if best_weighted is None or weighted < best_weighted:
            best_weighted = weighted
            best_obj2 = float(obj2)
            best_obj3 = float(obj3)
    return {
        "obj2": best_obj2,
        "obj3": best_obj3,
        "weighted": float(best_weighted if best_weighted is not None else 0.0),
        "method": "two_bay_assignment_dp",
    }


def _normalized_load_coefficients(bays: list[dict]) -> tuple[list[int], int]:
    areas = [
        Fraction(str(bay.get("width", 0))) * Fraction(str(bay.get("height", 0)))
        for bay in bays
    ]
    n_bays = len(bays)
    total_area = sum(areas, Fraction(0, 1))
    avg_area = total_area / n_bays
    weights = [avg_area / area if area > 0 else Fraction(1, 1) for area in areas]
    denominator = 1
    for weight in weights:
        denominator = math.lcm(denominator, weight.denominator)
    return [int(weight * denominator) for weight in weights], denominator


def _dp_min_update(dp: dict[int, float], key: int, value: float) -> None:
    previous = dp.get(key)
    if previous is None or value < previous:
        dp[key] = value


def _independent_preference_lower_bound(blocks: list[dict], bays: list[dict]) -> float:
    obj3 = 0.0
    for block in blocks:
        preferences = [float(value) for value in block.get("bay_preferences", [])]
        if not preferences:
            continue
        max_preference = max(preferences)
        feasible_bays = _bbox_fit_bays(block, bays)
        if not feasible_bays:
            feasible_bays = range(min(len(preferences), len(bays)))
        best_preference = max(preferences[bay_id] for bay_id in feasible_bays)
        obj3 += max_preference - best_preference
    return obj3


def _base_layer_area(orientation: dict) -> float:
    layers = orientation.get("layers", [])
    if not layers:
        return 0.0
    return _polygon_area(layers[0])


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


def _orientation_size(orientation: dict) -> tuple[float, float]:
    xs = []
    ys = []
    for layer in orientation.get("layers", []):
        for point in layer:
            if len(point) < 2:
                continue
            xs.append(float(point[0]))
            ys.append(float(point[1]))
    if not xs or not ys:
        return (0.0, 0.0)
    return (max(xs) - min(xs), max(ys) - min(ys))


def _result_row(
    instance_path: Path,
    prob_info: dict,
    elapsed: float,
    result: dict,
    *,
    secondary_cp_time_limit: float,
    secondary_cp_workers: int,
) -> dict:
    objective = result.get("objective")
    obj1 = result.get("obj1")
    lower_bound = objective_lower_bound(
        prob_info,
        secondary_cp_time_limit=secondary_cp_time_limit,
        secondary_cp_workers=secondary_cp_workers,
    )
    objective_gap = None
    relative_gap = None
    obj1_gap = None
    if objective is not None:
        objective_gap = float(objective) - float(lower_bound["lower_bound"])
        if objective:
            relative_gap = objective_gap / float(objective)
    if obj1 is not None:
        obj1_gap = float(obj1) - float(lower_bound["lower_bound_obj1"])

    row = {
        "instance": prob_info.get("name", instance_path.stem),
        "elapsed": round(elapsed, 3),
        "feasible": result["feasible"],
        "stage": result["stage"],
        "objective": objective,
        "obj1": obj1,
        "obj2": result.get("obj2"),
        "obj3": result.get("obj3"),
        "obj1_share": None,
        "lower_bound": lower_bound["lower_bound"],
        "lower_bound_obj1": lower_bound["lower_bound_obj1"],
        "lower_bound_obj1_release_processing": lower_bound["lower_bound_obj1_release_processing"],
        "lower_bound_obj1_area_time": lower_bound["lower_bound_obj1_area_time"],
        "lower_bound_obj1_energetic": lower_bound["lower_bound_obj1_energetic"],
        "lower_bound_obj2": lower_bound["lower_bound_obj2"],
        "lower_bound_obj3": lower_bound["lower_bound_obj3"],
        "lower_bound_secondary": lower_bound["lower_bound_secondary"],
        "lower_bound_secondary_method": lower_bound["lower_bound_secondary_method"],
        "lower_bound_secondary_status": lower_bound["lower_bound_secondary_status"],
        "lower_bound_secondary_cp_obj2_incumbent": lower_bound["lower_bound_secondary_cp_obj2_incumbent"],
        "lower_bound_secondary_cp_obj3_incumbent": lower_bound["lower_bound_secondary_cp_obj3_incumbent"],
        "objective_gap": objective_gap,
        "relative_gap": relative_gap,
        "obj1_gap": obj1_gap,
    }
    if objective not in (None, 0) and obj1 is not None:
        row["obj1_share"] = prob_info.get("weights", {}).get("w1", 1.0) * obj1 / objective
    return row


def _bounds_only_row(
    instance_path: Path,
    prob_info: dict,
    *,
    secondary_cp_time_limit: float,
    secondary_cp_workers: int,
) -> dict:
    lower_bound = objective_lower_bound(
        prob_info,
        secondary_cp_time_limit=secondary_cp_time_limit,
        secondary_cp_workers=secondary_cp_workers,
    )
    return {
        "instance": prob_info.get("name", instance_path.stem),
        "blocks": len(prob_info.get("blocks", [])),
        "bays": len(prob_info.get("bays", [])),
        "lower_bound": lower_bound["lower_bound"],
        "lower_bound_obj1": lower_bound["lower_bound_obj1"],
        "lower_bound_obj1_release_processing": lower_bound["lower_bound_obj1_release_processing"],
        "lower_bound_obj1_area_time": lower_bound["lower_bound_obj1_area_time"],
        "lower_bound_obj1_energetic": lower_bound["lower_bound_obj1_energetic"],
        "lower_bound_obj2": lower_bound["lower_bound_obj2"],
        "lower_bound_obj3": lower_bound["lower_bound_obj3"],
        "lower_bound_secondary": lower_bound["lower_bound_secondary"],
        "lower_bound_secondary_method": lower_bound["lower_bound_secondary_method"],
        "lower_bound_secondary_status": lower_bound["lower_bound_secondary_status"],
        "lower_bound_secondary_cp_obj2_incumbent": lower_bound["lower_bound_secondary_cp_obj2_incumbent"],
        "lower_bound_secondary_cp_obj3_incumbent": lower_bound["lower_bound_secondary_cp_obj3_incumbent"],
    }


def write_rows(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() == ".csv":
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]) if rows else [])
            writer.writeheader()
            writer.writerows(rows)
        return
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--train-dir", type=Path, default=ROOT / "train")
    parser.add_argument("--timelimit", type=float, default=60.0)
    parser.add_argument("--instances", nargs="*", default=[], help="Optional instance names, e.g. prob_1 prob_25")
    parser.add_argument("--output", type=Path, default=None, help="Optional .jsonl or .csv result path")
    parser.add_argument("--bounds-only", action="store_true", help="Only compute lower bounds; do not run solver")
    parser.add_argument(
        "--secondary-cp-time-limit",
        type=float,
        default=0.0,
        help="Optional per-instance CP-SAT time limit for exact relaxed obj2+obj3 assignment bounds.",
    )
    parser.add_argument("--secondary-cp-workers", type=int, default=8)
    args = parser.parse_args()

    if not args.bounds_only:
        from myalgorithm import algorithm  # noqa: E402
        from utils import check_feasibility  # noqa: E402

    rows = []
    for instance_path in selected_instances(args.train_dir, args.instances):
        with instance_path.open(encoding="utf-8") as handle:
            prob_info = json.load(handle)

        if args.bounds_only:
            rows.append(
                _bounds_only_row(
                    instance_path,
                    prob_info,
                    secondary_cp_time_limit=args.secondary_cp_time_limit,
                    secondary_cp_workers=args.secondary_cp_workers,
                )
            )
            print(json.dumps(rows[-1]))
            continue

        started_at = time.time()
        solution = algorithm(prob_info, args.timelimit)
        elapsed = time.time() - started_at
        result = check_feasibility(prob_info, solution)
        rows.append(
            _result_row(
                instance_path,
                prob_info,
                elapsed,
                result,
                secondary_cp_time_limit=args.secondary_cp_time_limit,
                secondary_cp_workers=args.secondary_cp_workers,
            )
        )
        print(json.dumps(rows[-1]))

    if args.bounds_only:
        if args.output is not None:
            write_rows(args.output, rows)
        print(
            json.dumps(
                {
                    "instances": len(rows),
                    "sum_lower_bound": sum(float(row["lower_bound"]) for row in rows),
                    "output": str(args.output) if args.output is not None else None,
                },
                indent=2,
            )
        )
        return

    feasible_count = sum(1 for row in rows if row["feasible"])
    objective_rows = [row for row in rows if row["objective"] is not None]
    sum_objective = sum(float(row["objective"]) for row in objective_rows)
    sum_lower_bound = sum(float(row["lower_bound"]) for row in objective_rows)
    if args.output is not None:
        write_rows(args.output, rows)
    print(
        json.dumps(
            {
                "instances": len(rows),
                "feasible": feasible_count,
                "infeasible": len(rows) - feasible_count,
                "sum_objective": sum_objective,
                "sum_lower_bound": sum_lower_bound,
                "sum_objective_gap": sum_objective - sum_lower_bound,
                "relative_gap": (sum_objective - sum_lower_bound) / sum_objective if sum_objective else None,
                "output": str(args.output) if args.output is not None else None,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
