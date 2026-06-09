"""Evaluate ogc_solver on all training instances."""

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

from myalgorithm import algorithm  # noqa: E402
from utils import check_feasibility  # noqa: E402


def natural_key(path: Path) -> tuple:
    stem = path.stem
    suffix = stem.split("_")[-1]
    return (stem.rsplit("_", 1)[0], int(suffix) if suffix.isdigit() else suffix)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--train-dir", type=Path, default=ROOT / "train")
    parser.add_argument("--timelimit", type=float, default=60.0)
    args = parser.parse_args()

    rows = []
    for instance_path in sorted(args.train_dir.glob("*.json"), key=natural_key):
        with instance_path.open(encoding="utf-8") as handle:
            prob_info = json.load(handle)

        started_at = time.time()
        solution = algorithm(prob_info, args.timelimit)
        elapsed = time.time() - started_at
        result = check_feasibility(prob_info, solution)
        rows.append(
            {
                "instance": prob_info.get("name", instance_path.stem),
                "elapsed": round(elapsed, 3),
                "feasible": result["feasible"],
                "stage": result["stage"],
                "objective": result.get("objective"),
            }
        )
        print(json.dumps(rows[-1]))

    feasible_count = sum(1 for row in rows if row["feasible"])
    print(
        json.dumps(
            {
                "instances": len(rows),
                "feasible": feasible_count,
                "infeasible": len(rows) - feasible_count,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
