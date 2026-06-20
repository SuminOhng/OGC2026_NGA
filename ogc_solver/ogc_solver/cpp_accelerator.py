"""C++ solver bridge.

Submitted search is performed by the bundled native executable.  Python only
serializes the instance, launches the executable, parses candidate solutions,
and lets the official checker rank feasible C++ candidates.
"""

from __future__ import annotations

import math
import os
import subprocess
import stat
from pathlib import Path

from .state import orientation_bbox, resolve_layers


def solve_with_cpp_accelerator(prob_info: dict, timeout_seconds: float) -> dict | None:
    """Return a candidate solution from the native accelerator, if available."""

    candidates = solve_cpp_accelerator_candidates(prob_info, timeout_seconds)
    return candidates[0] if candidates else None


def solve_cpp_accelerator_candidates(prob_info: dict, timeout_seconds: float) -> list[dict]:
    """Return candidate solutions from the native accelerator, if available."""

    executable = _find_executable()
    if executable is None or timeout_seconds <= 0.05:
        return []

    geometry_mode = os.environ.get("OGC_CPP_GEOMETRY_MODE", "").strip().lower()
    if geometry_mode in {"surrogate", "bbox", "box"}:
        return _run_cpp_solver(
            executable,
            _build_payload(prob_info, timeout_seconds, include_polygons=False),
            timeout_seconds,
            len(prob_info.get("blocks", [])),
        )
    if geometry_mode in {"exact", "polygon", "polygons"}:
        return _run_cpp_solver(
            executable,
            _build_payload(prob_info, timeout_seconds, include_polygons=True),
            timeout_seconds,
            len(prob_info.get("blocks", [])),
        )

    require_polygons = os.environ.get("OGC_CPP_REQUIRE_POLYGONS", "").strip()
    n_blocks = len(prob_info.get("blocks", []))
    seed_polish_seconds = 0.0
    if not require_polygons and n_blocks >= 150 and timeout_seconds >= 120.0:
        seed_polish_seconds = min(54.0, max(18.0, float(timeout_seconds) * 0.24))
    elif not require_polygons and n_blocks >= 150 and timeout_seconds >= 30.0:
        seed_polish_seconds = min(18.0, max(0.0, float(timeout_seconds) * 0.32))
    initial_seconds = max(0.05, float(timeout_seconds) - seed_polish_seconds)
    if require_polygons:
        polygon_seconds = float(timeout_seconds)
    elif n_blocks >= 250 and timeout_seconds >= 45.0:
        polygon_seconds = min(initial_seconds - 10.0, max(3.0, initial_seconds * 0.72))
    else:
        polygon_seconds = min(3.0, max(0.0, initial_seconds * 0.10))
    candidates = []
    if polygon_seconds > 0.2:
        candidates = _run_cpp_solver(
            executable,
            _build_payload(prob_info, polygon_seconds, include_polygons=True),
            polygon_seconds,
            len(prob_info.get("blocks", [])),
        )
    if require_polygons:
        return candidates

    fallback_seconds = max(0.05, initial_seconds - polygon_seconds)
    candidates.extend(
        _run_cpp_solver(
            executable,
            _build_payload(prob_info, fallback_seconds, include_polygons=False),
            fallback_seconds,
            len(prob_info.get("blocks", [])),
        )
    )
    if seed_polish_seconds > 0.2 and candidates:
        if seed_polish_seconds >= 40.0:
            seed_rounds = 5
        elif seed_polish_seconds >= 24.0:
            seed_rounds = 4
        elif seed_polish_seconds >= 12.0:
            seed_rounds = 3
        elif seed_polish_seconds >= 6.0:
            seed_rounds = 2
        else:
            seed_rounds = 1
        round_seconds = max(0.5, seed_polish_seconds / seed_rounds)
        seed_pool = list(candidates)
        for _round in range(seed_rounds):
            seed_candidates = sorted(
                seed_pool,
                key=lambda candidate: _proxy_solution_rank(prob_info, candidate),
            )[:24]
            new_candidates = _run_cpp_solver(
                executable,
                _build_payload(
                    prob_info,
                    round_seconds,
                    include_polygons=True,
                    seed_solutions=seed_candidates,
                ),
                round_seconds,
                len(prob_info.get("blocks", [])),
            )
            if not new_candidates:
                break
            candidates.extend(new_candidates)
            seed_pool = new_candidates + seed_pool
    return candidates


def polish_cpp_candidates_with_seeds(
    prob_info: dict,
    seed_solutions: list[dict],
    timeout_seconds: float,
) -> list[dict]:
    executable = _find_executable()
    if executable is None or timeout_seconds <= 0.05 or not seed_solutions:
        return []
    return _run_cpp_solver(
        executable,
        _build_payload(
            prob_info,
            timeout_seconds,
            include_polygons=True,
            seed_solutions=seed_solutions,
        ),
        timeout_seconds,
        len(prob_info.get("blocks", [])),
    )


def _run_cpp_solver(executable: Path, payload: str, timeout_seconds: float, expected_blocks: int) -> list[dict]:
    try:
        completed = subprocess.run(
            [str(executable)],
            input=payload,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=max(0.2, float(timeout_seconds) + 0.5),
            cwd=str(executable.parent),
            check=False,
        )
    except Exception:
        return []

    if completed.returncode != 0:
        return []
    return _parse_solutions(completed.stdout, expected_blocks)


def _find_executable() -> Path | None:
    override = os.environ.get("OGC_CPP_ACCEL_PATH", "").strip()
    if override:
        path = Path(override)
        if path.is_file():
            _ensure_executable(path)
            return path

    if os.environ.get("OGC_DISABLE_CPP_ACCEL", "").strip():
        return None

    base = Path(__file__).resolve().parent / "cpp_accel"
    if os.name == "nt":
        candidates = [
            base / "ogc_fast_solver.exe",
            base / "ogc_fast_solver",
        ]
    else:
        candidates = [
            base / "ogc_fast_solver",
            base / "ogc_fast_solver.exe",
        ]
    for path in candidates:
        if path.is_file():
            _ensure_executable(path)
            return path
    return None


def _ensure_executable(path: Path) -> None:
    try:
        mode = path.stat().st_mode
        path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except Exception:
        pass


def _build_payload(
    prob_info: dict,
    timeout_seconds: float,
    *,
    include_polygons: bool,
    seed_solutions: list[dict] | None = None,
) -> str:
    bays = prob_info.get("bays", [])
    blocks = prob_info.get("blocks", [])
    weights = prob_info.get("weights", {})
    include_polygons = include_polygons and os.environ.get("OGC_CPP_NO_POLYGONS", "").strip() == ""
    lines = [
        "OGC_FAST_SOLVER_V1",
        f"{max(1, int(timeout_seconds * 1000))} {len(bays)} {len(blocks)}",
        f"{float(weights.get('w1', 1.0)):.12g} {float(weights.get('w2', 1.0)):.12g} {float(weights.get('w3', 1.0)):.12g}",
    ]
    for bay in bays:
        lines.append(f"{float(bay['width']):.12g} {float(bay['height']):.12g}")

    for block in blocks:
        values = [
            str(int(block["release_time"])),
            str(int(block["due_date"])),
            str(int(block["processing_time"])),
            f"{float(block['workload']):.12g}",
        ]
        values.extend(str(int(pref)) for pref in block.get("bay_preferences", []))
        shapes = block.get("shape", [])
        values.append(str(len(shapes)))
        for orient_idx in range(len(shapes)):
            bbox = orientation_bbox(block, orient_idx)
            values.extend(f"{float(value):.12g}" for value in bbox)
            if include_polygons:
                layers = resolve_layers(shapes[orient_idx].get("layers", []))
                layer_boxes = [_layer_bbox(layer) for layer in layers]
                values.append(str(len(layer_boxes)))
                for layer_box, layer in zip(layer_boxes, layers):
                    values.extend(f"{float(value):.12g}" for value in layer_box)
                    values.append(str(len(layer)))
                    for vertex in layer:
                        values.append(f"{float(vertex[0]):.12g}")
                        values.append(f"{float(vertex[1]):.12g}")
            else:
                values.append("0")
        lines.append(" ".join(values))
    seeds = [_solution_assignments(solution, len(blocks)) for solution in (seed_solutions or [])]
    seeds = [seed for seed in seeds if seed is not None]
    if seeds:
        lines.append(f"SEEDS {len(seeds)}")
        for seed in seeds:
            for assignment in seed:
                lines.append(
                    "{} {} {} {} {} {} {}".format(
                        int(assignment["block_id"]),
                        int(assignment["bay_id"]),
                        int(assignment["x"]),
                        int(assignment["y"]),
                        int(assignment["orient_idx"]),
                        int(assignment["entry_time"]),
                        int(assignment["exit_time"]),
                    )
                )
    return "\n".join(lines) + "\n"


def _solution_assignments(solution: dict, expected_blocks: int) -> list[dict] | None:
    operations = solution.get("operations", {})
    assignments: dict[int, dict] = {}
    try:
        for time_key, ops in operations.items():
            time_value = int(float(time_key))
            for op in ops:
                block_id = int(op["block_id"])
                row = assignments.setdefault(block_id, {"block_id": block_id})
                op_type = op.get("type")
                if op_type == "ENTRY":
                    row["bay_id"] = int(op["bay_id"])
                    row["x"] = int(op["x"])
                    row["y"] = int(op["y"])
                    row["orient_idx"] = int(op["orient_idx"])
                    row["entry_time"] = time_value
                elif op_type == "EXIT":
                    row["exit_time"] = time_value
    except Exception:
        return None
    if len(assignments) != expected_blocks:
        return None
    ordered = []
    for block_id in range(expected_blocks):
        row = assignments.get(block_id)
        if row is None:
            return None
        required = ("bay_id", "x", "y", "orient_idx", "entry_time", "exit_time")
        if any(key not in row for key in required):
            return None
        ordered.append(row)
    return ordered


def _proxy_solution_rank(prob_info: dict, solution: dict) -> tuple[float, float]:
    assignments = _solution_assignments(solution, len(prob_info.get("blocks", [])))
    if assignments is None:
        return (math.inf, math.inf)
    blocks = prob_info.get("blocks", [])
    weights = prob_info.get("weights", {})
    w1 = float(weights.get("w1", 1.0))
    w2 = float(weights.get("w2", 1.0))
    w3 = float(weights.get("w3", 1.0))
    obj1 = 0.0
    obj3 = 0.0
    loads = [0.0 for _ in prob_info.get("bays", [])]
    for row in assignments:
        block = blocks[int(row["block_id"])]
        obj1 += max(0, int(row["exit_time"]) - int(block["due_date"]))
        bay_id = int(row["bay_id"])
        if 0 <= bay_id < len(loads):
            loads[bay_id] += float(block["workload"])
        preferences = block.get("bay_preferences", [])
        if preferences:
            obj3 += max(preferences) - preferences[bay_id]
    bay_areas = [
        max(1e-9, float(bay["width"]) * float(bay["height"]))
        for bay in prob_info.get("bays", [])
    ]
    if bay_areas:
        average_area = sum(bay_areas) / len(bay_areas)
        bay_weights = [average_area / area for area in bay_areas]
        obj2 = 0.0
        for i, load_i in enumerate(loads):
            for j, load_j in enumerate(loads):
                if i != j:
                    obj2 = max(obj2, abs(bay_weights[i] * load_i - bay_weights[j] * load_j))
    else:
        obj2 = 0.0
    objective = w1 * obj1 + w2 * math.floor(obj2) + w3 * obj3
    return (obj1, objective)


def _layer_bbox(layer: list) -> tuple[float, float, float, float]:
    if not layer:
        return (0.0, 0.0, 0.0, 0.0)
    xs = [float(vertex[0]) for vertex in layer]
    ys = [float(vertex[1]) for vertex in layer]
    return (min(xs), min(ys), max(xs), max(ys))


def _parse_solutions(stdout: str, expected_blocks: int) -> list[dict]:
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    if not lines:
        return []
    header = lines[0].split()
    if len(header) == 2 and header[0] == "OK":
        try:
            count = int(header[1])
        except ValueError:
            return []
        if count != expected_blocks:
            return []
        return _parse_solution_variants(lines[1 : count + 1], expected_blocks)
    if len(header) == 3 and header[0] == "OK_MULTI":
        try:
            solution_count = int(header[1])
            block_count = int(header[2])
        except ValueError:
            return []
        if solution_count <= 0 or block_count != expected_blocks:
            return []
        solutions = []
        offset = 1
        for _ in range(solution_count):
            chunk = lines[offset : offset + expected_blocks]
            offset += expected_blocks
            solutions.extend(_parse_solution_variants(chunk, expected_blocks))
        return _dedupe_solution_variants(solutions)
    return []


def _parse_solution_variants(lines: list[str], expected_blocks: int) -> list[dict]:
    assignments = _parse_assignments(lines, expected_blocks)
    if assignments is None:
        return []
    exit_orders = [False, True] if _has_same_time_exits(assignments) else [False]
    entry_orders = [False, True] if _has_same_time_entries(assignments) else [False]
    variants = []
    for reverse_exits in exit_orders:
        for reverse_entries in entry_orders:
            variants.append(
                {
                    "operations": _build_operations(
                        assignments,
                        reverse_same_time_exits=reverse_exits,
                        reverse_same_time_entries=reverse_entries,
                    )
                }
            )
    return _dedupe_solution_variants(variants)


def _parse_assignments(lines: list[str], expected_blocks: int) -> list[dict] | None:
    if len(lines) != expected_blocks:
        return None
    assignments = []
    seen = set()
    for line in lines:
        parts = line.split()
        if len(parts) != 7:
            return None
        try:
            block_id, bay_id, x, y, orient_idx, entry_time, exit_time = (int(float(part)) for part in parts)
        except ValueError:
            return None
        if block_id in seen:
            return None
        if not all(math.isfinite(value) for value in (block_id, bay_id, x, y, orient_idx, entry_time, exit_time)):
            return None
        seen.add(block_id)
        assignments.append(
            {
                "block_id": block_id,
                "bay_id": bay_id,
                "x": x,
                "y": y,
                "orient_idx": orient_idx,
                "entry_time": entry_time,
                "exit_time": exit_time,
            }
        )
    if len(seen) != expected_blocks:
        return None
    return assignments


def _has_same_time_exits(assignments: list[dict]) -> bool:
    seen: set[tuple[int, int]] = set()
    for assignment in assignments:
        key = (int(assignment["bay_id"]), int(assignment["exit_time"]))
        if key in seen:
            return True
        seen.add(key)
    return False


def _has_same_time_entries(assignments: list[dict]) -> bool:
    seen: set[tuple[int, int]] = set()
    for assignment in assignments:
        key = (int(assignment["bay_id"]), int(assignment["entry_time"]))
        if key in seen:
            return True
        seen.add(key)
    return False


def _dedupe_solution_variants(solutions: list[dict]) -> list[dict]:
    unique = []
    seen = set()
    for solution in solutions:
        key = repr(solution.get("operations", {}))
        if key in seen:
            continue
        seen.add(key)
        unique.append(solution)
    return unique


def _build_operations(
    assignments: list[dict],
    *,
    reverse_same_time_exits: bool = False,
    reverse_same_time_entries: bool = False,
) -> dict:
    buckets: dict[int, list[tuple[int, int, dict]]] = {}
    for assignment in assignments:
        block_id = int(assignment["block_id"])
        bay_id = int(assignment["bay_id"])
        entry_time = int(assignment["entry_time"])
        exit_time = int(assignment["exit_time"])
        exit_order_block_id = -block_id if reverse_same_time_exits else block_id
        entry_order_block_id = -block_id if reverse_same_time_entries else block_id
        buckets.setdefault(exit_time, []).append(
            (0, exit_order_block_id, {"type": "EXIT", "block_id": block_id, "bay_id": bay_id})
        )
        buckets.setdefault(entry_time, []).append(
            (
                1,
                entry_order_block_id,
                {
                    "type": "ENTRY",
                    "block_id": block_id,
                    "bay_id": bay_id,
                    "x": int(assignment["x"]),
                    "y": int(assignment["y"]),
                    "orient_idx": int(assignment["orient_idx"]),
                },
            )
        )
    return {str(time_idx): [item[2] for item in sorted(items)] for time_idx, items in sorted(buckets.items())}
