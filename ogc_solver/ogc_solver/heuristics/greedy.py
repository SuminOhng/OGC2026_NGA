"""Conservative constructive heuristics."""

from __future__ import annotations

from ..scoring import preference_penalty, tardiness
from ..state import Placement, iter_feasible_orientations


def sequential_empty_bay(prob_info: dict, deadline: float | None = None) -> dict:
    """Assign each block to a non-overlapping bay interval.

    Keeping each bay empty except for one block at a time makes the initial
    solution simple and robust: entry, exit, and spatial collision constraints
    are avoided structurally. Stronger search modules can later replace or
    improve this initial solution.
    """

    bays = prob_info["bays"]
    blocks = prob_info["blocks"]
    bay_available = [0 for _ in bays]
    placements: list[Placement] = []

    block_order = sorted(
        range(len(blocks)),
        key=lambda block_id: (
            int(blocks[block_id]["due_date"]),
            int(blocks[block_id]["release_time"]),
            int(blocks[block_id]["processing_time"]),
            block_id,
        ),
    )

    for block_id in block_order:
        block_data = blocks[block_id]
        placement = _choose_placement(block_id, block_data, bays, bay_available)
        placements.append(placement)
        bay_available[placement.bay_id] = placement.exit_time

    return {"operations": _build_operations(placements)}


def _choose_placement(
    block_id: int,
    block_data: dict,
    bays: list[dict],
    bay_available: list[int],
) -> Placement:
    release_time = int(block_data["release_time"])
    processing_time = int(block_data["processing_time"])

    best_key = None
    best_value = None

    for bay_id, bay_data in enumerate(bays):
        for orient_idx, x, y in iter_feasible_orientations(block_data, bay_data):
            entry_time = max(release_time, int(bay_available[bay_id]))
            exit_time = entry_time + processing_time
            key = (
                tardiness(block_data, exit_time),
                preference_penalty(block_data, bay_id),
                exit_time,
                entry_time,
                bay_id,
                orient_idx,
            )
            if best_key is None or key < best_key:
                best_key = key
                best_value = (bay_id, x, y, orient_idx, entry_time, exit_time)

    if best_value is None:
        best_value = _fallback_placement(block_id, block_data, bays, bay_available)

    bay_id, x, y, orient_idx, entry_time, exit_time = best_value
    return Placement(
        block_id=block_id,
        bay_id=bay_id,
        x=int(x),
        y=int(y),
        orient_idx=int(orient_idx),
        entry_time=int(entry_time),
        exit_time=int(exit_time),
    )


def _fallback_placement(
    block_id: int,
    block_data: dict,
    bays: list[dict],
    bay_available: list[int],
) -> tuple[int, int, int, int, int, int]:
    """Return a deterministic assignment if no orientation appears to fit."""

    release_time = int(block_data["release_time"])
    processing_time = int(block_data["processing_time"])
    bay_id = min(range(len(bays)), key=lambda idx: (bay_available[idx], idx))
    entry_time = max(release_time, int(bay_available[bay_id]))
    exit_time = entry_time + processing_time
    return (bay_id, 0, 0, 0, entry_time, exit_time)


def _build_operations(placements: list[Placement]) -> dict:
    events: dict[int, list[dict]] = {}

    for placement in placements:
        events.setdefault(placement.exit_time, []).append(
            {
                "type": "EXIT",
                "block_id": placement.block_id,
                "bay_id": placement.bay_id,
            }
        )
        events.setdefault(placement.entry_time, []).append(
            {
                "type": "ENTRY",
                "block_id": placement.block_id,
                "bay_id": placement.bay_id,
                "x": placement.x,
                "y": placement.y,
                "orient_idx": placement.orient_idx,
            }
        )

    operations = {}
    for time_idx in sorted(events):
        ordered_events = sorted(
            events[time_idx],
            key=lambda op: (0 if op["type"] == "EXIT" else 1, op["block_id"]),
        )
        operations[str(int(time_idx))] = ordered_events

    return operations
