"""Run conservative surrogate probes over a small instance matrix.

This is an analysis helper, not submitted solver logic.  It calls
`surrogate_footprint_probe.solve_surrogate` directly and writes one JSON object
per instance, excluding the bulky solution unless `--save-solutions` is used.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from surrogate_footprint_probe import _slice_instance_by_due_date, solve_surrogate


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instances", nargs="+", required=True)
    parser.add_argument("--train-dir", type=Path, default=ROOT / "train")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--save-solutions", action="store_true")
    parser.add_argument("--solution-dir", type=Path, default=ROOT / ".codex_workspace" / "results" / "surrogate_solutions")
    parser.add_argument("--max-blocks", type=int, default=None)
    parser.add_argument("--mode", choices=["grid", "lanes", "edge_lanes"], default="edge_lanes")
    parser.add_argument("--lane-step", type=int, default=8)
    parser.add_argument("--grid-step", type=int, default=10)
    parser.add_argument("--x-anchor-count", type=int, default=5)
    parser.add_argument("--edge-cap-strategy", choices=["ranked", "balanced"], default="balanced")
    parser.add_argument("--max-options-per-block", type=int, default=18)
    parser.add_argument("--max-conflict-pairs", type=int, default=500_000)
    parser.add_argument("--time-limit", type=float, default=1.0)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--greedy-schedule-cp-limit", type=float, default=3.0)
    parser.add_argument("--neighborhood-cp-limit", type=float, default=5.0)
    parser.add_argument("--neighborhood-blocks", type=int, default=8)
    parser.add_argument("--neighborhood-objective", choices=["tardiness", "objective", "both"], default="tardiness")
    parser.add_argument("--neighborhood-selection", choices=["tardy", "blockers", "both"], default="tardy")
    parser.add_argument("--neighborhood-rounds", type=int, default=1)
    args = parser.parse_args()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.save_solutions:
        args.solution_dir.mkdir(parents=True, exist_ok=True)

    with args.output.open("w", encoding="utf-8") as out:
        for instance in args.instances:
            instance_path = args.train_dir / f"{instance}.json"
            with instance_path.open(encoding="utf-8") as handle:
                prob_info = json.load(handle)
            original_blocks = len(prob_info.get("blocks", []))
            if args.max_blocks is not None and original_blocks > args.max_blocks:
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
            if solution is not None and args.save_solutions:
                solution_path = args.solution_dir / f"{result.get('instance', instance)}.solution.json"
                with solution_path.open("w", encoding="utf-8") as handle:
                    json.dump(solution, handle)
                result["solution_path"] = str(solution_path)

            result["source_instance"] = instance
            result["source_blocks"] = original_blocks
            result["max_blocks"] = args.max_blocks
            out.write(json.dumps(_compact_result(result), ensure_ascii=False) + "\n")
            out.flush()
            print(
                f"{result.get('instance')}: feasible={result.get('neighborhood_cp_official_feasible')} "
                f"obj1={result.get('neighborhood_cp_official_obj1')} "
                f"objective={result.get('neighborhood_cp_official_objective')}"
            )


def _compact_result(result: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "source_instance",
        "source_blocks",
        "max_blocks",
        "instance",
        "n_blocks",
        "n_bays",
        "mode",
        "lane_step",
        "x_anchor_count",
        "edge_cap_strategy",
        "max_options_per_block",
        "option_count_before_cap",
        "option_count",
        "conflict_pairs",
        "conflict_truncated",
        "serial_official_obj1",
        "greedy_strategy",
        "greedy_score_mode",
        "greedy_official_objective",
        "greedy_official_obj1",
        "schedule_cp_status",
        "schedule_cp_official_objective",
        "schedule_cp_official_obj1",
        "neighborhood_cp_status",
        "neighborhood_cp_objective_mode",
        "neighborhood_cp_selection_mode",
        "neighborhood_cp_rounds",
        "neighborhood_cp_official_feasible",
        "neighborhood_cp_official_objective",
        "neighborhood_cp_official_obj1",
        "neighborhood_cp_official_obj2",
        "neighborhood_cp_official_obj3",
        "solution_source",
        "solution_path",
        "elapsed",
    ]
    return {key: result.get(key) for key in keys if key in result}


if __name__ == "__main__":
    main()
