"""Summarize conservative surrogate batch JSONL results.

This is an analysis helper, not submitted solver logic.  It combines selected
`surrogate_batch.py` JSONL outputs into per-instance CSV and Markdown evidence
for the conservative footprint surrogate experiments.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", nargs="+", type=Path, required=True)
    parser.add_argument("--csv-output", type=Path, required=True)
    parser.add_argument("--md-output", type=Path, required=True)
    parser.add_argument("--dedupe", choices=["none", "best-objective"], default="none")
    parser.add_argument("--expect-visible-40", action="store_true")
    args = parser.parse_args()

    rows = _load_rows(args.inputs)
    loaded_count = len(rows)
    if args.dedupe == "best-objective":
        rows = _dedupe_best_objective(rows)
    if args.expect_visible_40:
        _validate_visible_40(rows)

    args.csv_output.parent.mkdir(parents=True, exist_ok=True)
    args.md_output.parent.mkdir(parents=True, exist_ok=True)
    _write_csv(args.csv_output, rows)
    _write_markdown(
        args.md_output,
        rows,
        args.inputs,
        expect_visible_40=args.expect_visible_40,
        dedupe=args.dedupe,
        loaded_count=loaded_count,
    )

    print(f"loaded_rows={loaded_count}")
    print(f"rows={len(rows)}")
    print(f"feasible={sum(1 for row in rows if _is_feasible(row))}")
    print(f"truncated={sum(1 for row in rows if row.get('conflict_truncated'))}")
    print(f"csv={args.csv_output}")
    print(f"markdown={args.md_output}")


def _load_rows(inputs: list[Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for input_path in inputs:
        with input_path.open(encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, start=1):
                stripped = line.strip()
                if not stripped:
                    continue
                row = json.loads(stripped)
                row["_source_file"] = str(input_path)
                row["_source_line"] = line_no
                rows.append(row)
    rows.sort(key=lambda row: _instance_sort_key(str(row.get("instance", ""))))
    return rows


def _dedupe_best_objective(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    best_by_instance: dict[str, dict[str, Any]] = {}
    for row in rows:
        instance = str(row.get("instance"))
        current = best_by_instance.get(instance)
        if current is None or _official_rank(row) < _official_rank(current):
            best_by_instance[instance] = row
    return sorted(best_by_instance.values(), key=lambda row: _instance_sort_key(str(row.get("instance", ""))))


def _validate_visible_40(rows: list[dict[str, Any]]) -> None:
    counts = Counter(str(row.get("instance")) for row in rows)
    expected = {f"prob_{idx}" for idx in range(1, 41)}
    observed = set(counts)
    missing = sorted(expected - observed, key=_instance_sort_key)
    extra = sorted(observed - expected, key=_instance_sort_key)
    duplicates = sorted((name for name, count in counts.items() if count != 1), key=_instance_sort_key)
    if missing or extra or duplicates:
        details = {
            "missing": missing,
            "extra": extra,
            "duplicates": {name: counts[name] for name in duplicates},
        }
        raise SystemExit(f"visible-40 coverage check failed: {details}")


def _write_csv(output: Path, rows: list[dict[str, Any]]) -> None:
    fields = [
        "instance",
        "n_blocks",
        "n_bays",
        "x_anchor_count",
        "max_options_per_block",
        "conflict_pairs",
        "conflict_truncated",
        "feasible",
        "official_obj1",
        "official_objective",
        "official_obj2",
        "official_obj3",
        "greedy_strategy",
        "greedy_score_mode",
        "neighborhood_selection",
        "neighborhood_rounds",
        "solution_source",
        "elapsed",
        "source_file",
    ]
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow(_csv_row(row))


def _write_markdown(
    output: Path,
    rows: list[dict[str, Any]],
    inputs: list[Path],
    *,
    expect_visible_40: bool,
    dedupe: str,
    loaded_count: int,
) -> None:
    feasible_count = sum(1 for row in rows if _is_feasible(row))
    truncated_count = sum(1 for row in rows if row.get("conflict_truncated"))
    due_spread_count = sum(1 for row in rows if str(row.get("greedy_score_mode", "")).endswith("_due_spread"))
    obj1_values = [_official_obj1(row) for row in rows]
    objective_values = [_official_objective(row) for row in rows]
    elapsed_values = [_float(row.get("elapsed")) for row in rows]
    conflict_values = [_int(row.get("conflict_pairs")) for row in rows]

    lines = [
        "# Conservative Surrogate Visible Summary",
        "",
        "This file is generated by `scripts/summarize_surrogate_results.py`.",
        "It summarizes analysis-only conservative footprint surrogate runs; it is not submitted solver logic.",
        "",
        "## Inputs",
        "",
    ]
    lines.extend(f"- `{path}`" for path in inputs)
    lines.extend(
        [
            "",
            "## Coverage",
            "",
            f"- Loaded rows: {loaded_count}",
            f"- Dedupe mode: {dedupe}",
            f"- Rows: {len(rows)}",
            f"- Expected visible 40: {'yes' if expect_visible_40 else 'not checked'}",
            f"- Official feasible rows: {feasible_count}/{len(rows)}",
            f"- Conflict graph truncated rows: {truncated_count}/{len(rows)}",
            f"- Due-spread selected rows: {due_spread_count}/{len(rows)}",
            f"- Obj1 avg/min/max: {_fmt(mean(obj1_values))} / {_fmt(min(obj1_values))} / {_fmt(max(obj1_values))}",
            f"- Objective avg/min/max: {_fmt(mean(objective_values))} / {_fmt(min(objective_values))} / {_fmt(max(objective_values))}",
            f"- Elapsed avg/max seconds: {_fmt(mean(elapsed_values), 2)} / {_fmt(max(elapsed_values), 2)}",
            f"- Conflict pairs avg/max: {_fmt(mean(conflict_values))} / {_fmt(max(conflict_values))}",
            "",
            "## By Block Count",
            "",
            "| Blocks | Runs | Feasible | Truncated | Avg obj1 | Min obj1 | Max obj1 | Avg elapsed s | Max conflicts |",
            "| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for n_blocks, group in _groups_by(rows, "n_blocks"):
        group_obj1 = [_official_obj1(row) for row in group]
        group_elapsed = [_float(row.get("elapsed")) for row in group]
        group_conflicts = [_int(row.get("conflict_pairs")) for row in group]
        lines.append(
            f"| {n_blocks} | {len(group)} | {sum(1 for row in group if _is_feasible(row))} | "
            f"{sum(1 for row in group if row.get('conflict_truncated'))} | "
            f"{_fmt(mean(group_obj1))} | {_fmt(min(group_obj1))} | {_fmt(max(group_obj1))} | "
            f"{_fmt(mean(group_elapsed), 2)} | {_fmt(max(group_conflicts))} |"
        )

    lines.extend(
        [
            "",
            "## Per Instance",
            "",
            "| Instance | Blocks | Bays | Score | Selection | Rounds | Conflicts | Truncated | Feasible | Obj1 | Objective | Source | Elapsed s |",
            "| --- | ---: | ---: | --- | --- | ---: | ---: | --- | --- | ---: | ---: | --- | ---: |",
        ]
    )
    for row in rows:
        lines.append(
            f"| {row.get('instance')} | {row.get('n_blocks')} | {row.get('n_bays')} | "
            f"{row.get('greedy_score_mode')} | {row.get('neighborhood_cp_selection_mode', '')} | "
            f"{row.get('neighborhood_cp_rounds', '')} | "
            f"{_fmt(_int(row.get('conflict_pairs')))} | "
            f"{str(bool(row.get('conflict_truncated'))).lower()} | {str(_is_feasible(row)).lower()} | "
            f"{_fmt(_official_obj1(row))} | {_fmt(_official_objective(row))} | "
            f"{row.get('solution_source')} | {_fmt(_float(row.get('elapsed')), 2)} |"
        )

    output.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _csv_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "instance": row.get("instance"),
        "n_blocks": row.get("n_blocks"),
        "n_bays": row.get("n_bays"),
        "x_anchor_count": row.get("x_anchor_count"),
        "max_options_per_block": row.get("max_options_per_block"),
        "conflict_pairs": row.get("conflict_pairs"),
        "conflict_truncated": row.get("conflict_truncated"),
        "feasible": _is_feasible(row),
        "official_obj1": _official_obj1(row),
        "official_objective": _official_objective(row),
        "official_obj2": row.get("neighborhood_cp_official_obj2"),
        "official_obj3": row.get("neighborhood_cp_official_obj3"),
        "greedy_strategy": row.get("greedy_strategy"),
        "greedy_score_mode": row.get("greedy_score_mode"),
        "neighborhood_selection": row.get("neighborhood_cp_selection_mode"),
        "neighborhood_rounds": row.get("neighborhood_cp_rounds"),
        "solution_source": row.get("solution_source"),
        "elapsed": row.get("elapsed"),
        "source_file": row.get("_source_file"),
    }


def _groups_by(rows: list[dict[str, Any]], key: str) -> list[tuple[Any, list[dict[str, Any]]]]:
    groups: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[row.get(key)].append(row)
    return sorted(groups.items(), key=lambda item: item[0])


def _instance_sort_key(name: str) -> tuple[int, str]:
    prefix, _, suffix = name.partition("_")
    if prefix == "prob" and suffix.isdigit():
        return (int(suffix), name)
    return (10_000, name)


def _is_feasible(row: dict[str, Any]) -> bool:
    if "neighborhood_cp_official_feasible" in row:
        return bool(row["neighborhood_cp_official_feasible"])
    return _official_objective(row) < float("inf")


def _official_obj1(row: dict[str, Any]) -> float:
    return _float(row.get("neighborhood_cp_official_obj1", row.get("greedy_official_obj1")))


def _official_objective(row: dict[str, Any]) -> float:
    return _float(row.get("neighborhood_cp_official_objective", row.get("greedy_official_objective")))


def _float(value: Any) -> float:
    if value is None:
        return 0.0
    return float(value)


def _int(value: Any) -> int:
    if value is None:
        return 0
    return int(value)


def _fmt(value: float | int, digits: int = 0) -> str:
    if digits:
        return f"{float(value):,.{digits}f}"
    return f"{float(value):,.0f}"


def _official_rank(row: dict[str, Any]) -> tuple[float, float, float, str, int]:
    return (
        _official_objective(row),
        _official_obj1(row),
        _float(row.get("neighborhood_cp_official_obj2")),
        str(row.get("_source_file", "")),
        _int(row.get("_source_line")),
    )


if __name__ == "__main__":
    main()
