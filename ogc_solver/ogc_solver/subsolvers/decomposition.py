"""Small subsolver policies for the ALNS master loop.

The master ALNS still owns acceptance and official feasibility validation.
These helpers only provide focused subproblem decisions so that bay/placement
and schedule ideas can evolve independently.
"""

from __future__ import annotations


def should_run_schedule_polish(total_budget: float) -> bool:
    """Return True when there is enough time for schedule-only polishing."""

    return total_budget >= 20.0


def early_exit_protection_removal(
    prob_info: dict,
    assignments: dict[int, dict],
    count: int,
) -> list[int]:
    """Choose blocks that protect an early-due block's exit opportunity.

    This placement-focused destroy operator targets a tardy or tight early-due
    block and removes one or more later-due blocks in the same bay that overlap
    its occupancy interval. The intended repair then gets a chance to place the
    early block where its exit path is less likely to be buried.
    """

    if count <= 0:
        return []

    blocks = prob_info["blocks"]
    target_ids = sorted(
        assignments,
        key=lambda bid: (
            int(blocks[bid]["due_date"]),
            -max(0, int(assignments[bid]["exit_time"]) - int(blocks[bid]["due_date"])),
            int(assignments[bid]["exit_time"]),
            bid,
        ),
    )

    selected: list[int] = []
    seen: set[int] = set()
    for target_id in target_ids:
        target = assignments[target_id]
        target_due = int(blocks[target_id]["due_date"])
        target_tardiness = max(0, int(target["exit_time"]) - target_due)
        if target_tardiness <= 0:
            continue

        blockers = _geometry_exit_blockers(prob_info, assignments, target_id)
        if len(blockers) < count - 1:
            for blocker_id in _rank_later_due_blockers(prob_info, assignments, target_id):
                if blocker_id not in blockers:
                    blockers.append(blocker_id)
        if not blockers:
            continue

        selected.append(target_id)
        seen.add(target_id)
        if len(selected) >= count:
            return selected

        for blocker_id in blockers:
            if blocker_id in seen:
                continue
            selected.append(blocker_id)
            seen.add(blocker_id)
            if len(selected) >= count:
                return selected

        if selected:
            break

    if len(selected) < count:
        for block_id in _worst_tardy_ids(prob_info, assignments):
            if block_id not in seen:
                selected.append(block_id)
                seen.add(block_id)
            if len(selected) >= count:
                break
    return selected


def _geometry_exit_blockers(prob_info: dict, assignments: dict[int, dict], target_id: int) -> list[int]:
    from utils import Bay, Block, check_exit

    blocks = prob_info["blocks"]
    target = assignments[target_id]
    target_data = blocks[target_id]
    target_due = int(target_data["due_date"])
    target_entry = int(target["entry_time"])
    target_exit = int(target["exit_time"])
    processing_time = int(target_data["processing_time"])

    candidate_exit = min(target_exit - 1, max(target_entry + processing_time, target_due))
    if candidate_exit <= target_entry or candidate_exit >= target_exit:
        return []

    bay_id = int(target["bay_id"])
    bay = Bay.from_dict(prob_info["bays"][bay_id], bay_id)
    target_block = Block(
        target_id,
        target_data,
        x=int(target["x"]),
        y=int(target["y"]),
        orient_idx=int(target["orient_idx"]),
    )

    present = [target_block]
    for other_id, other in assignments.items():
        if other_id == target_id or int(other["bay_id"]) != bay_id:
            continue
        if int(other["entry_time"]) < candidate_exit < int(other["exit_time"]):
            present.append(
                Block(
                    other_id,
                    blocks[other_id],
                    x=int(other["x"]),
                    y=int(other["y"]),
                    orient_idx=int(other["orient_idx"]),
                )
            )

    blockers = []
    seen = set()
    for obstruction in check_exit(bay, present, target_block, fast=False):
        blocker_id = int(obstruction.existing_block.block_id)
        if blocker_id == target_id or blocker_id in seen:
            continue
        if int(blocks[blocker_id]["due_date"]) < target_due:
            continue
        seen.add(blocker_id)
        blockers.append(blocker_id)
    return blockers


def _rank_later_due_blockers(prob_info: dict, assignments: dict[int, dict], target_id: int) -> list[int]:
    blocks = prob_info["blocks"]
    target = assignments[target_id]
    target_due = int(blocks[target_id]["due_date"])
    target_entry = int(target["entry_time"])
    target_exit = int(target["exit_time"])
    ranked = []

    for other_id, other in assignments.items():
        if other_id == target_id or other["bay_id"] != target["bay_id"]:
            continue

        other_entry = int(other["entry_time"])
        other_exit = int(other["exit_time"])
        overlap = min(target_exit, other_exit) - max(target_entry, other_entry)
        spans_target_exit = other_entry < target_exit < other_exit
        if overlap <= 0 and not spans_target_exit:
            continue

        due_gap = int(blocks[other_id]["due_date"]) - target_due
        if due_gap < 0:
            continue
        ranked.append(
            (
                not spans_target_exit,
                -max(0, due_gap),
                -max(0, overlap),
                int(other["exit_time"]),
                other_id,
            )
        )

    return [item[-1] for item in sorted(ranked)]


def _worst_tardy_ids(prob_info: dict, assignments: dict[int, dict]) -> list[int]:
    blocks = prob_info["blocks"]
    return sorted(
        assignments,
        key=lambda bid: (
            max(0, int(assignments[bid]["exit_time"]) - int(blocks[bid]["due_date"])),
            int(assignments[bid]["exit_time"]),
            -int(blocks[bid]["due_date"]),
        ),
        reverse=True,
    )
