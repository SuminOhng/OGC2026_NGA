"""Compare two batch_eval JSONL result files by instance.

This is a local analysis helper.  It is useful for CP-polish gate A/B tests:

    python scripts/compare_batch_results.py baseline.jsonl candidate.jsonl

Lower objective and lower obj1 are better, so positive ``*_improvement``
values mean the candidate improved over the baseline.
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--output", type=Path, default=None, help="Optional .jsonl or .csv comparison output")
    args = parser.parse_args()

    baseline = _read_result_rows(args.baseline)
    candidate = _read_result_rows(args.candidate)
    rows = compare_results(baseline, candidate)
    for row in rows:
        print(json.dumps(row))
    summary = summarize(rows)
    print(json.dumps(summary, indent=2))
    if args.output is not None:
        _write_rows(args.output, rows)


def compare_results(
    baseline: dict[str, dict[str, Any]],
    candidate: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    rows = []
    for instance in sorted(set(baseline) | set(candidate), key=_natural_instance_key):
        base = baseline.get(instance)
        cand = candidate.get(instance)
        row: dict[str, Any] = {"instance": instance}
        if base is None:
            row["status"] = "candidate_only"
            rows.append(row)
            continue
        if cand is None:
            row["status"] = "baseline_only"
            rows.append(row)
            continue
        row["status"] = "matched"
        for field in ("feasible", "objective", "obj1", "obj2", "obj3", "lower_bound"):
            row[f"baseline_{field}"] = base.get(field)
            row[f"candidate_{field}"] = cand.get(field)
        row["objective_improvement"] = _delta(base.get("objective"), cand.get("objective"))
        row["obj1_improvement"] = _delta(base.get("obj1"), cand.get("obj1"))
        row["objective_improvement_pct"] = _pct(row["objective_improvement"], base.get("objective"))
        row["obj1_improvement_pct"] = _pct(row["obj1_improvement"], base.get("obj1"))
        row["candidate_better_objective"] = _is_positive(row["objective_improvement"])
        row["candidate_better_obj1"] = _is_positive(row["obj1_improvement"])
        rows.append(row)
    return rows


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    matched = [row for row in rows if row.get("status") == "matched"]
    objective_improvement = sum(float(row.get("objective_improvement") or 0.0) for row in matched)
    obj1_improvement = sum(float(row.get("obj1_improvement") or 0.0) for row in matched)
    base_objective = sum(float(row.get("baseline_objective") or 0.0) for row in matched)
    base_obj1 = sum(float(row.get("baseline_obj1") or 0.0) for row in matched)
    return {
        "matched_instances": len(matched),
        "candidate_better_objective": sum(1 for row in matched if row.get("candidate_better_objective")),
        "candidate_better_obj1": sum(1 for row in matched if row.get("candidate_better_obj1")),
        "sum_objective_improvement": objective_improvement,
        "sum_obj1_improvement": obj1_improvement,
        "sum_objective_improvement_pct": _pct(objective_improvement, base_objective),
        "sum_obj1_improvement_pct": _pct(obj1_improvement, base_obj1),
    }


def _read_result_rows(path: Path) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            instance = record.get("instance")
            if not instance:
                continue
            rows[str(instance)] = record
    return rows


def _write_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() == ".csv":
        fieldnames = sorted({key for row in rows for key in row})
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        return
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def _delta(baseline: Any, candidate: Any) -> float | None:
    if baseline is None or candidate is None:
        return None
    return float(baseline) - float(candidate)


def _pct(delta: Any, baseline: Any) -> float | None:
    if delta is None or baseline in (None, 0):
        return None
    return float(delta) / float(baseline)


def _is_positive(value: Any) -> bool:
    return value is not None and float(value) > 1e-9


def _natural_instance_key(name: str) -> tuple[str, int | str]:
    prefix, _, suffix = name.rpartition("_")
    return (prefix, int(suffix) if suffix.isdigit() else suffix)


if __name__ == "__main__":
    main()
