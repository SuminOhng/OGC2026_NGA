"""Probe the internal due-window chain repair on a saved solution.

This is an analysis tool. It imports private solver helpers to answer whether
the existing local repair kernel can improve a particular incumbent without
rerunning the whole solver.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SUBMISSION_ROOT = ROOT / "ogc_solver"
BASELINE_ROOT = ROOT / "baseline" / "baseline"

sys.path.insert(0, str(BASELINE_ROOT))
sys.path.insert(0, str(SUBMISSION_ROOT))

from utils import check_feasibility  # noqa: E402
from ogc_solver.alns.engine import _assignments_from_solution, _due_window_chain_repair  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("instance", type=Path)
    parser.add_argument("--solution", type=Path, required=True)
    parser.add_argument("--time-limit", type=float, default=20.0)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--save-solution", type=Path, default=None)
    args = parser.parse_args()

    with args.instance.open(encoding="utf-8") as handle:
        prob_info = json.load(handle)
    with args.solution.open(encoding="utf-8") as handle:
        solution = json.load(handle)

    initial_result = check_feasibility(prob_info, solution)
    started_at = time.monotonic()
    candidate = None
    candidate_result = None
    if initial_result.get("feasible"):
        assignments = _assignments_from_solution(solution)
        candidate, candidate_result = _due_window_chain_repair(
            prob_info,
            assignments,
            initial_result,
            time.monotonic() + max(0.1, args.time_limit),
        )
    elapsed = time.monotonic() - started_at

    payload = {
        "instance": prob_info.get("name", args.instance.stem),
        "elapsed": round(elapsed, 3),
        "time_limit": float(args.time_limit),
        "initial": _summary(initial_result),
        "candidate": _summary(candidate_result) if candidate_result is not None else None,
    }
    if candidate is not None and args.save_solution is not None:
        args.save_solution.parent.mkdir(parents=True, exist_ok=True)
        with args.save_solution.open("w", encoding="utf-8") as handle:
            json.dump(candidate, handle)
        payload["candidate_solution_path"] = str(args.save_solution)

    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
    print(json.dumps(payload, indent=2))


def _summary(result: dict | None) -> dict | None:
    if result is None:
        return None
    return {
        "feasible": result.get("feasible"),
        "stage": result.get("stage"),
        "objective": result.get("objective"),
        "obj1": result.get("obj1"),
        "obj2": result.get("obj2"),
        "obj3": result.get("obj3"),
    }


if __name__ == "__main__":
    main()
