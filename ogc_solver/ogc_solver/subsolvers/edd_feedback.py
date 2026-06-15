"""EDD schedule feedback for bay-assignment decomposition.

This module is intentionally standalone: it does not call the master solver or
ALNS engine.  It takes fixed bay/placement decisions from a solution, rebuilds a
per-bay EDD schedule with ``exit = entry + processing_time``, and reports local
geometry conflicts as pair signals that a master assignment model can penalize
or cut in a later iteration.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Iterable, Mapping


ConflictSignal = dict[str, Any]
Assignment = dict[str, int]


@dataclass(frozen=True)
class EDDFeedbackResult:
    """Attribute-style result used by hierarchical seed builders."""

    solution: dict[str, Any]
    assignments: dict[int, Assignment]
    conflict_signals: list[ConflictSignal]
    conflict_penalties: list[tuple[int, int, int, float]]
    conflict_cuts: list[dict[str, Any]]
    proxy_result: dict[str, Any]
    geometry_available: bool
    utils_error: str | None = None


@dataclass(frozen=True)
class _Geometry:
    Bay: Any | None = None
    Block: Any | None = None
    check_entry: Any | None = None
    check_exit: Any | None = None
    check_collisions: Any | None = None
    error: str | None = None

    @property
    def available(self) -> bool:
        return (
            self.Bay is not None
            and self.Block is not None
            and self.check_entry is not None
            and self.check_exit is not None
            and self.check_collisions is not None
        )


@dataclass(frozen=True)
class _FallbackBay:
    width: int
    height: int
    id: int = 0

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], idx: int = 0) -> "_FallbackBay":
        return cls(width=int(data.get("width", 0)), height=int(data.get("height", 0)), id=int(idx))

    def contains_block(self, block: "_FallbackBlock") -> bool:
        min_x, min_y, max_x, max_y = block.bounding_rect()
        return min_x >= 0 and min_y >= 0 and max_x <= self.width and max_y <= self.height


@dataclass(frozen=True)
class _FallbackBlock:
    block_id: int
    block_data: Mapping[str, Any]
    x: int = 0
    y: int = 0
    orient_idx: int = 0

    def layers_at_pos(self) -> list[list[list[float]]]:
        layers = _orientation_layers(self.block_data, self.orient_idx)
        if not layers:
            return []
        ref_x, ref_y = layers[0][0] if layers[0] else (0.0, 0.0)
        dx = float(self.x) - float(ref_x)
        dy = float(self.y) - float(ref_y)
        return [
            [[float(vertex[0]) + dx, float(vertex[1]) + dy] for vertex in layer]
            for layer in layers
        ]

    def bounding_rect(self) -> tuple[float, float, float, float]:
        layers = self.layers_at_pos()
        vertices = [vertex for layer in layers for vertex in layer]
        if not vertices:
            return (float(self.x), float(self.y), float(self.x) + 1.0, float(self.y) + 1.0)
        xs = [float(vertex[0]) for vertex in vertices]
        ys = [float(vertex[1]) for vertex in vertices]
        return (min(xs), min(ys), max(xs), max(ys))


@dataclass(frozen=True)
class _FallbackObstruction:
    existing_block: Any
    area: float = 1.0
    new_layer: int = 0
    exist_layer: int = 0

    @property
    def is_sweep(self) -> bool:
        return self.exist_layer > self.new_layer


@dataclass(frozen=True)
class _FallbackCollision:
    block_a: Any
    block_b: Any
    area: float = 1.0
    layer_index: int = 0


def parse_solution_operations(solution: Mapping[str, Any]) -> dict[int, Assignment]:
    """Parse operation buckets into one assignment row per complete block."""

    assignments: dict[int, Assignment] = {}
    entry_seq = 0
    operations = solution.get("operations", {}) if isinstance(solution, Mapping) else {}
    if not isinstance(operations, Mapping):
        return {}

    for time_key, bucket in sorted(operations.items(), key=lambda item: _time_sort_key(item[0])):
        time_idx = _safe_int(time_key, 0)
        if not isinstance(bucket, Iterable) or isinstance(bucket, (str, bytes)):
            continue
        for op in bucket:
            if not isinstance(op, Mapping) or "block_id" not in op:
                continue
            block_id = _safe_int(op.get("block_id"), None)
            if block_id is None:
                continue
            row = assignments.setdefault(block_id, {"block_id": int(block_id)})
            op_type = str(op.get("type", "")).upper()
            if op_type == "ENTRY":
                row.update(
                    {
                        "bay_id": _safe_int(op.get("bay_id"), 0),
                        "x": _safe_int(op.get("x"), 0),
                        "y": _safe_int(op.get("y"), 0),
                        "orient_idx": _safe_int(op.get("orient_idx"), 0),
                        "entry_time": int(time_idx),
                        "_seq": int(entry_seq),
                    }
                )
                entry_seq += 1
            elif op_type == "EXIT":
                row["bay_id"] = _safe_int(op.get("bay_id"), row.get("bay_id", 0))
                row["exit_time"] = int(time_idx)

    required = {"bay_id", "x", "y", "orient_idx", "entry_time", "exit_time"}
    return {
        int(block_id): dict(row)
        for block_id, row in assignments.items()
        if required.issubset(row)
    }


def build_operations(assignments: Iterable[Mapping[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Build deterministic operation buckets from assignment rows."""

    buckets: dict[int, list[tuple[int, int, int, dict[str, Any]]]] = {}
    for row in assignments:
        block_id = _safe_int(row.get("block_id"), None)
        bay_id = _safe_int(row.get("bay_id"), None)
        if block_id is None or bay_id is None:
            continue
        entry_time = _safe_int(row.get("entry_time"), 0)
        exit_time = _safe_int(row.get("exit_time"), entry_time)
        x = _safe_int(row.get("x"), 0)
        y = _safe_int(row.get("y"), 0)
        orient_idx = _safe_int(row.get("orient_idx"), 0)
        seq = _safe_int(row.get("_seq"), block_id)

        buckets.setdefault(exit_time, []).append(
            (
                0,
                0,
                block_id,
                {"type": "EXIT", "block_id": block_id, "bay_id": bay_id},
            )
        )
        buckets.setdefault(entry_time, []).append(
            (
                1,
                seq,
                block_id,
                {
                    "type": "ENTRY",
                    "block_id": block_id,
                    "bay_id": bay_id,
                    "x": x,
                    "y": y,
                    "orient_idx": orient_idx,
                },
            )
        )

    return {
        str(int(time_idx)): [item[3] for item in sorted(items)]
        for time_idx, items in sorted(buckets.items())
    }


def evaluate_edd_feedback(
    prob_info: Mapping[str, Any],
    solution: Mapping[str, Any],
    *,
    deadline: float | None = None,
    max_delay_candidates: int = 96,
    conflict_limit: int = 256,
) -> dict[str, Any]:
    """Return an EDD candidate solution plus master-feedback conflict signals.

    The input solution supplies bay, x, y, and orientation decisions.  This
    helper keeps those placements fixed, schedules each bay independently in EDD
    order, and tests local entry, exit, and collision geometry against already
    scheduled blocks in the same bay.
    """

    geometry = _load_geometry()
    assignments = parse_solution_operations(solution)
    blocks = list(prob_info.get("blocks", []))
    bays = list(prob_info.get("bays", []))
    conflicts: list[ConflictSignal] = []
    candidate_assignments: dict[int, Assignment] = {}

    missing_ids = sorted(set(range(len(blocks))) - set(assignments))
    for block_id in missing_ids:
        conflicts.append(
            _conflict_signal(
                victim=block_id,
                blocker=None,
                bay=-1,
                event="missing_assignment",
                severity=1000.0,
            )
        )

    by_bay: list[list[int]] = [[] for _ in bays]
    for block_id, row in sorted(assignments.items()):
        bay_id = int(row.get("bay_id", -1))
        if 0 <= bay_id < len(bays):
            by_bay[bay_id].append(block_id)
        else:
            fixed = _duration_fixed_row(blocks, row)
            candidate_assignments[block_id] = fixed
            conflicts.append(
                _conflict_signal(
                    victim=block_id,
                    blocker=None,
                    bay=bay_id,
                    event="invalid_bay",
                    severity=1000.0,
                )
            )

    for bay_id, block_ids in enumerate(by_bay):
        if _deadline_reached(deadline):
            for block_id in block_ids:
                candidate_assignments.setdefault(block_id, _duration_fixed_row(blocks, assignments[block_id]))
            continue
        scheduled, bay_conflicts = _evaluate_bay_edd(
            prob_info,
            assignments,
            bay_id,
            block_ids,
            geometry,
            deadline=deadline,
            max_delay_candidates=max_delay_candidates,
            conflict_limit=max(0, conflict_limit - len(conflicts)),
        )
        candidate_assignments.update(scheduled)
        conflicts.extend(bay_conflicts)

    deduped_conflicts = _dedupe_conflicts(conflicts, conflict_limit)
    candidate_solution = {"operations": build_operations(candidate_assignments.values())}
    proxy_result = _proxy_result(prob_info, candidate_assignments)
    penalties = conflicts_to_penalties(deduped_conflicts)
    cuts = conflicts_to_cuts(deduped_conflicts)
    return {
        "candidate_solution": candidate_solution,
        "solution": candidate_solution,
        "assignments": dict(sorted(candidate_assignments.items())),
        "conflicts": deduped_conflicts,
        "conflict_signals": deduped_conflicts,
        "penalties": penalties,
        "conflict_penalties": penalties,
        "cuts": cuts,
        "conflict_cuts": cuts,
        "proxy_result": proxy_result,
        "geometry_available": geometry.available,
        "utils_error": geometry.error,
        "complete": len(candidate_assignments) == len(blocks),
    }


def evaluate_edd_schedule_feedback(
    prob_info: Mapping[str, Any],
    solution: Mapping[str, Any],
    deadline: float | None = None,
    *,
    max_delay_candidates: int = 96,
    conflict_limit: int = 256,
) -> EDDFeedbackResult:
    """Compatibility wrapper returning attribute-style EDD feedback."""

    report = evaluate_edd_feedback(
        prob_info,
        solution,
        deadline=deadline,
        max_delay_candidates=max_delay_candidates,
        conflict_limit=conflict_limit,
    )
    return EDDFeedbackResult(
        solution=report["solution"],
        assignments=report["assignments"],
        conflict_signals=report["conflict_signals"],
        conflict_penalties=report["conflict_penalties"],
        conflict_cuts=report["conflict_cuts"],
        proxy_result=report["proxy_result"],
        geometry_available=bool(report["geometry_available"]),
        utils_error=report["utils_error"],
    )


def conflicts_to_penalties(
    conflicts: Iterable[Mapping[str, Any]],
    *,
    base_penalty: float = 1.0,
) -> list[tuple[int, int, int, float]]:
    """Aggregate block-pair conflict signals as ``(left, right, bay, penalty)``."""

    penalties: dict[tuple[int, int, int], float] = {}
    for signal in conflicts:
        victim = _safe_int(signal.get("victim"), None)
        blocker = _safe_int(signal.get("blocker"), None)
        bay_id = _safe_int(signal.get("bay"), None)
        if victim is None or blocker is None or bay_id is None or victim == blocker:
            continue
        left_id, right_id = sorted((victim, blocker))
        key = (left_id, right_id, bay_id)
        penalties[key] = penalties.get(key, 0.0) + float(base_penalty) * _safe_float(signal.get("severity"), 1.0)

    ranked = sorted(
        ((penalty, left_id, right_id, bay_id) for (left_id, right_id, bay_id), penalty in penalties.items()),
        key=lambda item: (-item[0], item[1], item[2], item[3]),
    )
    return [(left_id, right_id, bay_id, float(penalty)) for penalty, left_id, right_id, bay_id in ranked]


def merge_conflict_penalties(
    *penalty_lists: Iterable[tuple[int, int, int, float]],
    limit: int = 128,
) -> list[tuple[int, int, int, float]]:
    """Merge same-bay pair penalties from multiple feedback rounds."""

    merged: dict[tuple[int, int, int], float] = {}
    for penalties in penalty_lists:
        for left_id, right_id, bay_id, penalty in penalties:
            left, right = sorted((_safe_int(left_id, 0), _safe_int(right_id, 0)))
            key = (int(left), int(right), _safe_int(bay_id, 0))
            merged[key] = merged.get(key, 0.0) + float(penalty)

    ranked = sorted(
        ((penalty, left_id, right_id, bay_id) for (left_id, right_id, bay_id), penalty in merged.items()),
        key=lambda item: (-item[0], item[1], item[2], item[3]),
    )
    return [(left_id, right_id, bay_id, float(penalty)) for penalty, left_id, right_id, bay_id in ranked[:limit]]


def conflicts_to_cuts(conflicts: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Return deterministic same-bay cut records derived from pair conflicts."""

    cuts: dict[tuple[int, int, int, str], dict[str, Any]] = {}
    for signal in conflicts:
        victim = _safe_int(signal.get("victim"), None)
        blocker = _safe_int(signal.get("blocker"), None)
        bay_id = _safe_int(signal.get("bay"), None)
        if victim is None or blocker is None or bay_id is None or victim == blocker:
            continue
        left_id, right_id = sorted((victim, blocker))
        event = str(signal.get("event", "conflict"))
        key = (left_id, right_id, bay_id, event)
        severity = _safe_float(signal.get("severity"), 1.0)
        existing = cuts.get(key)
        if existing is None:
            cuts[key] = {
                "left_id": left_id,
                "right_id": right_id,
                "bay_id": bay_id,
                "event": event,
                "severity": float(severity),
            }
        else:
            existing["severity"] = float(existing["severity"]) + float(severity)

    return [
        cuts[key]
        for key in sorted(
            cuts,
            key=lambda item: (-float(cuts[item]["severity"]), item[0], item[1], item[2], item[3]),
        )
    ]


def _evaluate_bay_edd(
    prob_info: Mapping[str, Any],
    assignments: Mapping[int, Mapping[str, Any]],
    bay_id: int,
    block_ids: list[int],
    geometry: _Geometry,
    *,
    deadline: float | None,
    max_delay_candidates: int,
    conflict_limit: int,
) -> tuple[dict[int, Assignment], list[ConflictSignal]]:
    blocks = list(prob_info.get("blocks", []))
    ordered = sorted(
        block_ids,
        key=lambda block_id: (
            _block_int(blocks, block_id, "due_date", 0),
            _block_int(blocks, block_id, "release_time", 0),
            _block_int(blocks, block_id, "processing_time", 0),
            int(assignments[block_id].get("_seq", block_id)),
            block_id,
        ),
    )

    scheduled: dict[int, Assignment] = {}
    conflicts: list[ConflictSignal] = []

    for edd_seq, block_id in enumerate(ordered):
        row = dict(assignments[block_id])
        row["_seq"] = edd_seq
        row["bay_id"] = int(bay_id)
        if _deadline_reached(deadline):
            scheduled[block_id] = _duration_fixed_row(blocks, row)
            continue
        processing_time = max(0, _block_int(blocks, block_id, "processing_time", 0))
        release_time = _block_int(blocks, block_id, "release_time", 0)
        due_time = _block_int(blocks, block_id, "due_date", release_time + processing_time)
        original_entry = int(row.get("entry_time", release_time))

        orientation_conflict = _orientation_conflict(blocks, block_id, row, bay_id)
        if orientation_conflict is not None:
            conflicts.append(orientation_conflict)

        candidate_times = _candidate_entry_times(
            release_time=release_time,
            processing_time=processing_time,
            due_time=due_time,
            original_entry=original_entry,
            scheduled_rows=list(scheduled.values()),
            max_delay_candidates=max_delay_candidates,
        )
        chosen: Assignment | None = None
        first_conflicts: list[ConflictSignal] = []
        first_entry = candidate_times[0] if candidate_times else release_time

        for entry_time in candidate_times:
            if _deadline_reached(deadline):
                break
            trial = dict(row)
            trial["entry_time"] = int(entry_time)
            trial["exit_time"] = int(entry_time) + int(processing_time)
            local_conflicts = _slot_conflicts(prob_info, bay_id, block_id, trial, list(scheduled.values()), geometry)
            if local_conflicts:
                if not first_conflicts:
                    first_conflicts = local_conflicts
                continue
            chosen = trial
            break

        if chosen is None:
            fallback_entry = max(
                release_time,
                max((int(other["exit_time"]) for other in scheduled.values()), default=release_time),
            )
            chosen = dict(row)
            chosen["entry_time"] = int(fallback_entry)
            chosen["exit_time"] = int(fallback_entry) + int(processing_time)
            unresolved = _slot_conflicts(prob_info, bay_id, block_id, chosen, list(scheduled.values()), geometry)
            for signal in unresolved or first_conflicts:
                signal = dict(signal)
                signal["unresolved"] = True
                conflicts.append(signal)
        elif first_conflicts:
            delay = max(0, int(chosen["entry_time"]) - int(first_entry))
            for signal in first_conflicts:
                signal = dict(signal)
                signal["resolved_by_delay"] = int(delay)
                signal["severity"] = float(signal["severity"]) + float(delay)
                conflicts.append(signal)

        scheduled[block_id] = _duration_fixed_row(blocks, chosen)
        if len(conflicts) >= conflict_limit:
            conflicts = conflicts[:conflict_limit]

    return scheduled, conflicts


def _slot_conflicts(
    prob_info: Mapping[str, Any],
    bay_id: int,
    block_id: int,
    trial: Mapping[str, Any],
    scheduled_rows: list[Mapping[str, Any]],
    geometry: _Geometry,
) -> list[ConflictSignal]:
    blocks = list(prob_info.get("blocks", []))
    bays = list(prob_info.get("bays", []))
    if not (0 <= bay_id < len(bays)) or not (0 <= block_id < len(blocks)):
        return []

    bay = _make_bay(geometry, bays[bay_id], bay_id)
    target = _make_block(geometry, block_id, blocks[block_id], trial)
    entry_time = int(trial["entry_time"])
    exit_time = int(trial["exit_time"])
    due_time = _block_int(blocks, block_id, "due_date", exit_time)
    tardiness = max(0, exit_time - due_time)

    conflicts: list[ConflictSignal] = []
    present_at_entry = [
        _make_block(geometry, int(other["block_id"]), blocks[int(other["block_id"])], other)
        for other in scheduled_rows
        if int(other["entry_time"]) <= entry_time < int(other["exit_time"])
    ]
    entry_obstructions = _check_entry(geometry, bay, present_at_entry, target)
    conflicts.extend(
        _signals_from_obstructions(
            obstructions=entry_obstructions,
            victim=block_id,
            bay_id=bay_id,
            event="entry",
            time_idx=entry_time,
            base_severity=1.0 + tardiness,
        )
    )

    for other in scheduled_rows:
        other_id = int(other["block_id"])
        if not _time_overlaps(entry_time, exit_time, int(other["entry_time"]), int(other["exit_time"])):
            continue
        other_block = _make_block(geometry, other_id, blocks[other_id], other)
        collisions = _check_collisions(geometry, bay, [target, other_block])
        conflicts.extend(
            _signals_from_collisions(
                collisions=collisions,
                victim=block_id,
                fallback_blocker=other_id,
                bay_id=bay_id,
                event="collision",
                time_idx=entry_time,
                base_severity=2.0 + tardiness,
            )
        )

    present_at_exit = [
        target,
        *[
            _make_block(geometry, int(other["block_id"]), blocks[int(other["block_id"])], other)
            for other in scheduled_rows
            if int(other["entry_time"]) < exit_time < int(other["exit_time"])
        ],
    ]
    exit_obstructions = _check_exit(geometry, bay, present_at_exit, target)
    conflicts.extend(
        _signals_from_obstructions(
            obstructions=exit_obstructions,
            victim=block_id,
            bay_id=bay_id,
            event="exit",
            time_idx=exit_time,
            base_severity=2.0 + tardiness,
        )
    )

    for protected in scheduled_rows:
        protected_id = int(protected["block_id"])
        protected_exit = int(protected["exit_time"])
        if not (entry_time < protected_exit < exit_time):
            continue
        protected_block = _make_block(geometry, protected_id, blocks[protected_id], protected)
        present = [
            protected_block,
            target,
            *[
                _make_block(geometry, int(other["block_id"]), blocks[int(other["block_id"])], other)
                for other in scheduled_rows
                if int(other["block_id"]) != protected_id
                and int(other["entry_time"]) < protected_exit < int(other["exit_time"])
            ],
        ]
        protected_obstructions = _check_exit(geometry, bay, present, protected_block)
        conflicts.extend(
            _signals_from_obstructions(
                obstructions=protected_obstructions,
                victim=protected_id,
                bay_id=bay_id,
                event="protect_exit",
                time_idx=protected_exit,
                base_severity=3.0 + max(0, protected_exit - _block_int(blocks, protected_id, "due_date", protected_exit)),
                preferred_blocker=block_id,
            )
        )

    return conflicts


def _duration_fixed_row(blocks: list[Any], row: Mapping[str, Any]) -> Assignment:
    block_id = _safe_int(row.get("block_id"), 0)
    entry_time = _safe_int(row.get("entry_time"), 0)
    processing_time = _block_int(blocks, block_id, "processing_time", _safe_int(row.get("exit_time"), entry_time) - entry_time)
    return {
        "block_id": int(block_id),
        "bay_id": _safe_int(row.get("bay_id"), 0),
        "x": _safe_int(row.get("x"), 0),
        "y": _safe_int(row.get("y"), 0),
        "orient_idx": _safe_int(row.get("orient_idx"), 0),
        "entry_time": int(entry_time),
        "exit_time": int(entry_time) + max(0, int(processing_time)),
        "_seq": _safe_int(row.get("_seq"), block_id),
    }


def _candidate_entry_times(
    *,
    release_time: int,
    processing_time: int,
    due_time: int,
    original_entry: int,
    scheduled_rows: list[Mapping[str, Any]],
    max_delay_candidates: int,
) -> list[int]:
    times = {
        int(release_time),
        max(int(release_time), int(due_time) - int(processing_time)),
        max(int(release_time), int(original_entry)),
    }
    if scheduled_rows:
        max_exit = max(int(row["exit_time"]) for row in scheduled_rows)
        times.add(max(int(release_time), int(max_exit)))
    for row in scheduled_rows:
        if int(row["entry_time"]) >= int(release_time):
            times.add(int(row["entry_time"]))
        if int(row["exit_time"]) >= int(release_time):
            times.add(int(row["exit_time"]))

    ordered = sorted(t for t in times if t >= int(release_time))
    if max_delay_candidates <= 0 or len(ordered) <= max_delay_candidates:
        return ordered or [int(release_time)]

    protected = {
        int(release_time),
        max(int(release_time), int(due_time) - int(processing_time)),
        max(int(release_time), int(original_entry)),
        ordered[-1],
    }
    prefix_budget = max(0, int(max_delay_candidates) - len(protected))
    return sorted(set(ordered[:prefix_budget]) | protected)


def _orientation_conflict(
    blocks: list[Any],
    block_id: int,
    row: Mapping[str, Any],
    bay_id: int,
) -> ConflictSignal | None:
    if not (0 <= block_id < len(blocks)):
        return None
    shape = blocks[block_id].get("shape", []) if isinstance(blocks[block_id], Mapping) else []
    orient_idx = _safe_int(row.get("orient_idx"), 0)
    if 0 <= orient_idx < len(shape):
        return None
    return _conflict_signal(
        victim=block_id,
        blocker=None,
        bay=bay_id,
        event="invalid_orientation",
        severity=1000.0,
    )


def _load_geometry() -> _Geometry:
    try:
        from utils import Bay, Block, check_collisions, check_entry, check_exit

        return _Geometry(
            Bay=Bay,
            Block=Block,
            check_entry=check_entry,
            check_exit=check_exit,
            check_collisions=check_collisions,
        )
    except Exception as exc:  # pragma: no cover - depends on runner path.
        return _Geometry(error=f"{type(exc).__name__}: {exc}")


def _make_bay(geometry: _Geometry, bay_data: Mapping[str, Any], bay_id: int):
    if geometry.available:
        try:
            return geometry.Bay.from_dict(bay_data, bay_id)
        except Exception:
            pass
    return _FallbackBay.from_dict(bay_data, bay_id)


def _make_block(geometry: _Geometry, block_id: int, block_data: Mapping[str, Any], row: Mapping[str, Any]):
    x = _safe_int(row.get("x"), 0)
    y = _safe_int(row.get("y"), 0)
    orient_idx = _safe_int(row.get("orient_idx"), 0)
    if geometry.available:
        try:
            return geometry.Block(block_id, block_data, x=x, y=y, orient_idx=orient_idx)
        except Exception:
            pass
    return _FallbackBlock(block_id=block_id, block_data=block_data, x=x, y=y, orient_idx=orient_idx)


def _check_entry(geometry: _Geometry, bay, present: list[Any], target) -> list[Any]:
    if geometry.available:
        try:
            return list(geometry.check_entry(bay, present, target, fast=False))
        except Exception:
            pass
    return _fallback_entry_obstructions(bay, present, target)


def _check_exit(geometry: _Geometry, bay, present: list[Any], target) -> list[Any]:
    if geometry.available:
        try:
            return list(geometry.check_exit(bay, present, target, fast=False))
        except Exception:
            pass
    return _fallback_exit_obstructions(present, target)


def _check_collisions(geometry: _Geometry, bay, blocks: list[Any]) -> list[Any]:
    if geometry.available:
        try:
            return list(geometry.check_collisions(bay, blocks))
        except Exception:
            pass
    return _fallback_collisions(blocks)


def _fallback_entry_obstructions(bay, present: list[Any], target) -> list[_FallbackObstruction]:
    obstructions: list[_FallbackObstruction] = []
    if not bay.contains_block(target):
        obstructions.append(_FallbackObstruction(existing_block=target, area=_bbox_area(target.bounding_rect())))
        return obstructions
    for other in present:
        if _bbox_overlap(target.bounding_rect(), other.bounding_rect()):
            obstructions.append(
                _FallbackObstruction(existing_block=other, area=_bbox_intersection_area(target.bounding_rect(), other.bounding_rect()))
            )
    return obstructions


def _fallback_exit_obstructions(present: list[Any], target) -> list[_FallbackObstruction]:
    obstructions: list[_FallbackObstruction] = []
    target_bbox = target.bounding_rect()
    for other in present:
        if int(getattr(other, "block_id", -1)) == int(getattr(target, "block_id", -2)):
            continue
        if _bbox_overlap(target_bbox, other.bounding_rect()):
            obstructions.append(
                _FallbackObstruction(existing_block=other, area=_bbox_intersection_area(target_bbox, other.bounding_rect()))
            )
    return obstructions


def _fallback_collisions(blocks: list[Any]) -> list[_FallbackCollision]:
    collisions: list[_FallbackCollision] = []
    for left_idx, left in enumerate(blocks):
        left_bbox = left.bounding_rect()
        for right in blocks[left_idx + 1 :]:
            right_bbox = right.bounding_rect()
            if _bbox_overlap(left_bbox, right_bbox):
                collisions.append(
                    _FallbackCollision(
                        block_a=left,
                        block_b=right,
                        area=_bbox_intersection_area(left_bbox, right_bbox),
                    )
                )
    return collisions


def _signals_from_obstructions(
    *,
    obstructions: Iterable[Any],
    victim: int,
    bay_id: int,
    event: str,
    time_idx: int,
    base_severity: float,
    preferred_blocker: int | None = None,
) -> list[ConflictSignal]:
    signals: list[ConflictSignal] = []
    for obstruction in obstructions:
        blocker = preferred_blocker
        existing = getattr(obstruction, "existing_block", None)
        if blocker is None and existing is not None:
            blocker = _safe_int(getattr(existing, "block_id", None), None)
        signal_event = "boundary" if blocker == victim else event
        severity = float(base_severity) + _safe_float(getattr(obstruction, "area", 1.0), 1.0)
        if bool(getattr(obstruction, "is_sweep", False)):
            severity += 1.0
        signals.append(
            _conflict_signal(
                victim=victim,
                blocker=None if blocker == victim else blocker,
                bay=bay_id,
                event=signal_event,
                severity=severity,
                time_idx=time_idx,
            )
        )
    return signals


def _signals_from_collisions(
    *,
    collisions: Iterable[Any],
    victim: int,
    fallback_blocker: int,
    bay_id: int,
    event: str,
    time_idx: int,
    base_severity: float,
) -> list[ConflictSignal]:
    signals: list[ConflictSignal] = []
    for collision in collisions:
        left_id = _safe_int(getattr(getattr(collision, "block_a", None), "block_id", None), None)
        right_id = _safe_int(getattr(getattr(collision, "block_b", None), "block_id", None), None)
        if left_id == victim and right_id is not None:
            blocker = right_id
        elif right_id == victim and left_id is not None:
            blocker = left_id
        else:
            blocker = fallback_blocker
        severity = float(base_severity) + _safe_float(getattr(collision, "area", 1.0), 1.0)
        signals.append(
            _conflict_signal(
                victim=victim,
                blocker=blocker,
                bay=bay_id,
                event=event,
                severity=severity,
                time_idx=time_idx,
            )
        )
    return signals


def _conflict_signal(
    *,
    victim: int,
    blocker: int | None,
    bay: int,
    event: str,
    severity: float,
    time_idx: int | None = None,
) -> ConflictSignal:
    signal: ConflictSignal = {
        "victim": int(victim),
        "blocker": None if blocker is None else int(blocker),
        "bay": int(bay),
        "event": str(event),
        "severity": float(max(0.0, severity)),
    }
    if time_idx is not None:
        signal["time"] = int(time_idx)
    return signal


def _dedupe_conflicts(conflicts: Iterable[Mapping[str, Any]], limit: int) -> list[ConflictSignal]:
    merged: dict[tuple[int, int | None, int, str], ConflictSignal] = {}
    for signal in conflicts:
        victim = _safe_int(signal.get("victim"), None)
        bay_id = _safe_int(signal.get("bay"), None)
        if victim is None or bay_id is None:
            continue
        blocker = _safe_int(signal.get("blocker"), None)
        event = str(signal.get("event", "conflict"))
        key = (victim, blocker, bay_id, event)
        severity = _safe_float(signal.get("severity"), 1.0)
        current = merged.get(key)
        if current is None:
            current = {
                "victim": victim,
                "blocker": blocker,
                "bay": bay_id,
                "event": event,
                "severity": float(severity),
                "count": 1,
            }
            if "time" in signal:
                current["time"] = _safe_int(signal.get("time"), 0)
            if "resolved_by_delay" in signal:
                current["resolved_by_delay"] = _safe_int(signal.get("resolved_by_delay"), 0)
            if signal.get("unresolved"):
                current["unresolved"] = True
            merged[key] = current
        else:
            current["severity"] = float(current["severity"]) + float(severity)
            current["count"] = int(current.get("count", 1)) + 1
            if "time" in signal:
                current["time"] = min(_safe_int(current.get("time"), _safe_int(signal.get("time"), 0)), _safe_int(signal.get("time"), 0))
            if "resolved_by_delay" in signal:
                current["resolved_by_delay"] = max(
                    _safe_int(current.get("resolved_by_delay"), 0),
                    _safe_int(signal.get("resolved_by_delay"), 0),
                )
            if signal.get("unresolved"):
                current["unresolved"] = True

    ordered = sorted(
        merged.values(),
        key=lambda signal: (
            -float(signal["severity"]),
            int(signal["victim"]),
            -1 if signal["blocker"] is None else int(signal["blocker"]),
            int(signal["bay"]),
            str(signal["event"]),
        ),
    )
    return ordered[: max(0, int(limit))]


def _orientation_layers(block_data: Mapping[str, Any], orient_idx: int) -> list[list[list[float]]]:
    shapes = block_data.get("shape", []) if isinstance(block_data, Mapping) else []
    if not shapes:
        return []
    idx = min(max(0, int(orient_idx)), len(shapes) - 1)
    raw_layers = shapes[idx].get("layers", []) if isinstance(shapes[idx], Mapping) else []
    return [list(layer) for layer in raw_layers if layer]


def _proxy_result(prob_info: Mapping[str, Any], assignments: Mapping[int, Mapping[str, Any]]) -> dict[str, Any]:
    blocks = list(prob_info.get("blocks", []))
    bays = list(prob_info.get("bays", []))
    weights = prob_info.get("weights", {}) if isinstance(prob_info.get("weights", {}), Mapping) else {}
    w1 = _safe_float(weights.get("w1"), 1.0)
    w2 = _safe_float(weights.get("w2"), 1.0)
    w3 = _safe_float(weights.get("w3"), 1.0)

    obj1 = 0.0
    obj3 = 0.0
    bay_loads = [0.0 for _ in bays]
    for block_id, row in assignments.items():
        if not (0 <= int(block_id) < len(blocks)):
            continue
        block = blocks[int(block_id)]
        if not isinstance(block, Mapping):
            continue
        bay_id = int(row.get("bay_id", -1))
        exit_time = int(row.get("exit_time", 0))
        obj1 += max(0.0, float(exit_time - _safe_int(block.get("due_date"), exit_time)))
        if 0 <= bay_id < len(bay_loads):
            bay_loads[bay_id] += _safe_float(block.get("workload"), 0.0)
            preferences = block.get("bay_preferences", [])
            if preferences and bay_id < len(preferences):
                obj3 += max(float(value) for value in preferences) - float(preferences[bay_id])

    bay_areas = [
        max(1.0, _safe_float(bay.get("width"), 1.0) * _safe_float(bay.get("height"), 1.0))
        for bay in bays
        if isinstance(bay, Mapping)
    ]
    avg_area = sum(bay_areas) / max(1, len(bay_areas))
    bay_weights = [avg_area / area for area in bay_areas] or [1.0 for _ in bay_loads]
    obj2 = 0.0
    for left in range(len(bay_loads)):
        for right in range(left + 1, len(bay_loads)):
            left_weight = bay_weights[left] if left < len(bay_weights) else 1.0
            right_weight = bay_weights[right] if right < len(bay_weights) else 1.0
            obj2 = max(obj2, abs(left_weight * bay_loads[left] - right_weight * bay_loads[right]))

    objective = w1 * obj1 + w2 * int(obj2) + w3 * obj3
    return {
        "feasible": len(assignments) == len(blocks),
        "objective": float(objective),
        "obj1": int(obj1),
        "obj2": int(obj2),
        "obj3": float(obj3),
    }


def _block_int(blocks: list[Any], block_id: int, key: str, default: int) -> int:
    if 0 <= int(block_id) < len(blocks) and isinstance(blocks[int(block_id)], Mapping):
        return _safe_int(blocks[int(block_id)].get(key), default)
    return int(default)


def _time_overlaps(left_entry: int, left_exit: int, right_entry: int, right_exit: int) -> bool:
    return int(left_entry) < int(right_exit) and int(right_entry) < int(left_exit)


def _bbox_overlap(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> bool:
    return left[0] < right[2] and right[0] < left[2] and left[1] < right[3] and right[1] < left[3]


def _bbox_intersection_area(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> float:
    width = max(0.0, min(left[2], right[2]) - max(left[0], right[0]))
    height = max(0.0, min(left[3], right[3]) - max(left[1], right[1]))
    return width * height


def _bbox_area(bbox: tuple[float, float, float, float]) -> float:
    return max(0.0, bbox[2] - bbox[0]) * max(0.0, bbox[3] - bbox[1])


def _time_sort_key(value: Any) -> tuple[int, Any]:
    try:
        return (0, int(value))
    except Exception:
        return (1, str(value))


def _safe_int(value: Any, default: int | None = 0) -> int | None:
    try:
        return int(value)
    except Exception:
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _deadline_reached(deadline: float | None) -> bool:
    return deadline is not None and time.monotonic() >= float(deadline) - 0.05


__all__ = [
    "Assignment",
    "ConflictSignal",
    "EDDFeedbackResult",
    "build_operations",
    "conflicts_to_cuts",
    "conflicts_to_penalties",
    "evaluate_edd_schedule_feedback",
    "evaluate_edd_feedback",
    "merge_conflict_penalties",
    "parse_solution_operations",
]
