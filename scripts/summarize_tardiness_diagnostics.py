"""Summarize tardiness diagnostic JSON files and classify repair triggers."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("diagnostics", type=Path, nargs="+")
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--obj1-threshold", type=float, default=500.0)
    parser.add_argument("--ideal-share-threshold", type=float, default=0.60)
    parser.add_argument("--ideal-blocked-blocks-threshold", type=int, default=20)
    parser.add_argument("--cheap-ideal-share-threshold", type=float, default=0.60)
    parser.add_argument("--cheap-ideal-blocked-blocks-threshold", type=int, default=20)
    parser.add_argument("--cheap-obj1-threshold", type=float, default=500.0)
    parser.add_argument("--cheap-tardy-blocks-threshold", type=int, default=50)
    args = parser.parse_args()

    rows = []
    for path in args.diagnostics:
        with path.open(encoding="utf-8") as handle:
            diagnosis = json.load(handle)
        rows.append(
            summarize(
                diagnosis,
                source=path,
                obj1_threshold=args.obj1_threshold,
                ideal_share_threshold=args.ideal_share_threshold,
                ideal_blocked_blocks_threshold=args.ideal_blocked_blocks_threshold,
                cheap_ideal_share_threshold=args.cheap_ideal_share_threshold,
                cheap_ideal_blocked_blocks_threshold=args.cheap_ideal_blocked_blocks_threshold,
                cheap_obj1_threshold=args.cheap_obj1_threshold,
                cheap_tardy_blocks_threshold=args.cheap_tardy_blocks_threshold,
            )
        )

    payload = {"rows": rows}
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
    print(json.dumps(payload, indent=2))


def summarize(
    diagnosis: dict[str, Any],
    *,
    source: Path,
    obj1_threshold: float,
    ideal_share_threshold: float,
    ideal_blocked_blocks_threshold: int,
    cheap_ideal_share_threshold: float,
    cheap_ideal_blocked_blocks_threshold: int,
    cheap_obj1_threshold: float,
    cheap_tardy_blocks_threshold: int,
) -> dict[str, Any]:
    obj1 = float(diagnosis.get("obj1") or 0.0)
    objective = float(diagnosis.get("objective") or 0.0)
    ideal_share = _safe_float(diagnosis.get("ideal_blocked_tardiness_share"))
    cheap_ideal_share = _safe_float(diagnosis.get("cheap_ideal_blocked_tardiness_share"))
    entry_share = _safe_float(diagnosis.get("entry_delay_tardiness_share"))
    ideal_blocks = int(diagnosis.get("ideal_blocked_tardy_blocks") or 0)
    cheap_ideal_blocks = int(diagnosis.get("cheap_ideal_blocked_tardy_blocks") or 0)
    tardy_blocks = int(diagnosis.get("tardy_blocks") or 0)
    trigger = (
        obj1 >= obj1_threshold
        and ideal_share >= ideal_share_threshold
        and ideal_blocks >= ideal_blocked_blocks_threshold
    )
    cheap_ideal_trigger = (
        obj1 >= obj1_threshold
        and cheap_ideal_share >= cheap_ideal_share_threshold
        and cheap_ideal_blocks >= cheap_ideal_blocked_blocks_threshold
    )
    cheap_trigger = obj1 >= cheap_obj1_threshold and tardy_blocks >= cheap_tardy_blocks_threshold
    return {
        "source": str(source),
        "instance": diagnosis.get("instance") or source.stem,
        "objective": objective,
        "obj1": obj1,
        "tardy_blocks": tardy_blocks,
        "ideal_blocked_tardy_blocks": ideal_blocks,
        "ideal_blocked_tardiness_share": ideal_share,
        "cheap_ideal_blocked_tardy_blocks": cheap_ideal_blocks,
        "cheap_ideal_blocked_tardiness_share": cheap_ideal_share,
        "entry_delay_tardiness_share": entry_share,
        "trigger_tail_obj1_repair": trigger,
        "cheap_ideal_trigger_tail_obj1_repair": cheap_ideal_trigger,
        "cheap_trigger_tail_obj1_repair": cheap_trigger,
        "top_ideal_blockers": diagnosis.get("top_ideal_blockers", [])[:5],
        "top_cheap_ideal_blockers": diagnosis.get("top_cheap_ideal_blockers", [])[:5],
    }


def _safe_float(value: Any) -> float:
    if value is None:
        return 0.0
    return float(value)


if __name__ == "__main__":
    main()
