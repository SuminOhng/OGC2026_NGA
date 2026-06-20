"""Compute an exact relaxed bay-assignment lower bound for obj2+obj3.

This is an analysis tool, not submitted solver logic. It ignores placement,
timing, and access constraints, and optimizes only bay assignment. Because this
is a relaxation of the OGC problem, the resulting weighted obj2+obj3 value is a
valid lower bound for the secondary objective contribution.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from fractions import Fraction
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from batch_eval import _bbox_fit_bays, objective_lower_bound  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("instance", type=Path)
    parser.add_argument("--time-limit", type=float, default=30.0)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    with args.instance.open(encoding="utf-8") as handle:
        prob_info = json.load(handle)

    result = secondary_assignment_lb(
        prob_info,
        time_limit=args.time_limit,
        workers=args.workers,
    )
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("w", encoding="utf-8") as handle:
            json.dump(result, handle, indent=2)
    print(json.dumps(result, indent=2))


def secondary_assignment_lb(
    prob_info: dict[str, Any],
    *,
    time_limit: float,
    workers: int,
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

    weights = prob_info.get("weights", {})
    w1 = int(weights.get("w1", 1))
    w2 = int(weights.get("w2", 1))
    w3 = int(weights.get("w3", 1))

    model = cp_model.CpModel()
    n_bays = len(bays)
    x: dict[tuple[int, int], Any] = {}
    load_terms: list[list[tuple[Any, int]]] = [[] for _ in bays]
    obj3_terms: list[tuple[Any, int]] = []

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
            x[(block_id, bay_id)] = var
            presences.append(var)
            load_terms[bay_id].append((var, workload))
            preference = preferences[bay_id] if bay_id < len(preferences) else 0
            obj3_terms.append((var, max_preference - preference))
        model.AddExactlyOne(presences)

    total_workload = sum(int(block.get("workload", 0)) for block in blocks)
    loads = [
        model.NewIntVar(0, total_workload, f"load_{bay_id}")
        for bay_id in range(n_bays)
    ]
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
            scaled_left = coeffs[left] * loads[left]
            scaled_right = coeffs[right] * loads[right]
            model.AddAbsEquality(diff, scaled_left - scaled_right)
            # Official obj2 floors the normalized max difference.
            model.Add(diff <= obj2 * denominator + denominator - 1)

    model.Minimize(w2 * obj2 + w3 * obj3)

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = max(0.1, float(time_limit))
    solver.parameters.num_search_workers = max(1, int(workers))
    status = solver.Solve(model)

    base_lb = objective_lower_bound(prob_info)
    cp_secondary_bound = max(float(base_lb["lower_bound_secondary"]), float(solver.BestObjectiveBound()))
    cp_secondary_incumbent = None
    cp_obj2 = None
    cp_obj3 = None
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        cp_secondary_incumbent = float(solver.ObjectiveValue())
        cp_obj2 = float(solver.Value(obj2))
        cp_obj3 = float(solver.Value(obj3))

    combined_weighted_lb = w1 * float(base_lb["lower_bound_obj1"]) + cp_secondary_bound
    return {
        "instance": prob_info.get("name"),
        "available": True,
        "status": solver.StatusName(status),
        "time_limit": float(time_limit),
        "workers": int(workers),
        "base_weighted_lb": base_lb["lower_bound"],
        "base_obj1_lb": base_lb["lower_bound_obj1"],
        "base_secondary_weighted_lb": base_lb["lower_bound_secondary"],
        "cp_secondary_bound": cp_secondary_bound,
        "cp_secondary_incumbent": cp_secondary_incumbent,
        "cp_obj2_incumbent": cp_obj2,
        "cp_obj3_incumbent": cp_obj3,
        "combined_weighted_lb": combined_weighted_lb,
        "denominator": denominator,
        "coefficients": coeffs,
        "branches": solver.NumBranches(),
        "conflicts": solver.NumConflicts(),
        "wall_time": solver.WallTime(),
    }


def _normalized_load_coefficients(bays: list[dict[str, Any]]) -> tuple[list[int], int]:
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


if __name__ == "__main__":
    main()
