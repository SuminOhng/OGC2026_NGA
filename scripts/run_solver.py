"""Run ogc_solver on one local problem instance."""

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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("instance", type=Path)
    parser.add_argument("--timelimit", type=float, default=60.0)
    args = parser.parse_args()

    with args.instance.open(encoding="utf-8") as handle:
        prob_info = json.load(handle)

    started_at = time.time()
    solution = algorithm(prob_info, args.timelimit)
    elapsed = time.time() - started_at
    result = check_feasibility(prob_info, solution)

    print(
        json.dumps(
            {
                "instance": prob_info.get("name", args.instance.stem),
                "elapsed": round(elapsed, 3),
                "feasible": result["feasible"],
                "stage": result["stage"],
                "objective": result.get("objective"),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
