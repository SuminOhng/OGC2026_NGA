"""Score a saved OGC solution and report conservative lower-bound gaps."""

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
from utils import check_feasibility  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("instance", type=Path)
    parser.add_argument("solution", type=Path)
    parser.add_argument("--secondary-cp-time-limit", type=float, default=0.0)
    parser.add_argument("--secondary-cp-workers", type=int, default=8)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    with args.instance.open(encoding="utf-8") as handle:
        prob_info = json.load(handle)
    with args.solution.open(encoding="utf-8") as handle:
        solution = json.load(handle)

    row = score_solution(
        prob_info,
        solution,
        instance_name=prob_info.get("name", args.instance.stem),
        solution_path=args.solution,
        secondary_cp_time_limit=args.secondary_cp_time_limit,
        secondary_cp_workers=args.secondary_cp_workers,
    )
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("w", encoding="utf-8") as handle:
            json.dump(row, handle, indent=2)
    print(json.dumps(row, indent=2))


def score_solution(
    prob_info: dict[str, Any],
    solution: dict[str, Any],
    *,
    instance_name: str,
    solution_path: Path,
    secondary_cp_time_limit: float,
    secondary_cp_workers: int,
) -> dict[str, Any]:
    result = check_feasibility(prob_info, solution)
    lower_bound = objective_lower_bound(
        prob_info,
        secondary_cp_time_limit=secondary_cp_time_limit,
        secondary_cp_workers=secondary_cp_workers,
    )
    objective = result.get("objective")
    obj1 = result.get("obj1")
    objective_gap = None
    relative_gap = None
    obj1_gap = None
    if objective is not None:
        objective_gap = float(objective) - float(lower_bound["lower_bound"])
        if float(objective) != 0.0:
            relative_gap = objective_gap / float(objective)
    if obj1 is not None:
        obj1_gap = float(obj1) - float(lower_bound["lower_bound_obj1"])

    return {
        "instance": instance_name,
        "solution": str(solution_path),
        "feasible": result.get("feasible"),
        "stage": result.get("stage"),
        "objective": objective,
        "obj1": obj1,
        "obj2": result.get("obj2"),
        "obj3": result.get("obj3"),
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


if __name__ == "__main__":
    main()
