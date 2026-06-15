"""Hierarchical bay-assignment seed builders.

The master decision is the bay assignment. Placement is deliberately simple:
spread each bay's assigned blocks across the bay and keep the per-bay schedule
serial so that the seed remains robustly feasible. Later ALNS passes can use
the seed as a basin and repair local conflicts more aggressively.
"""

from __future__ import annotations

import math
import time

from ..state import Placement, orientation_bbox
from .edd_feedback import evaluate_edd_schedule_feedback, merge_conflict_penalties
from .scheduling_mip import (
    reschedule_fixed_placements,
    same_bay_pair_needs_time_separation,
)


def build_edd_master_seed(prob_info: dict, deadline: float, *, max_rounds: int | None = None) -> dict | None:
    """Build a seed with Master bay assignment and EDD conflict feedback."""

    if time.monotonic() >= deadline - 0.5:
        return None

    started_at = time.monotonic()
    remaining = max(0.0, deadline - started_at)
    if max_rounds is None:
        max_rounds = 3 if remaining >= 24.0 else 2

    penalties: list[tuple[int, int, int, float]] = []
    best_solution = None
    best_score = math.inf
    best_proxy = None

    print(
        "[EDDMasterSeed] start "
        f"budget={remaining:.2f}s blocks={len(prob_info.get('blocks', []))} "
        f"bays={len(prob_info.get('bays', []))} rounds={max_rounds}",
        flush=True,
    )
    for round_idx in range(max_rounds):
        if time.monotonic() >= deadline - 1.0:
            break
        round_remaining = deadline - time.monotonic()
        mip_budget = max(1.0, min(round_remaining * 0.55, round_remaining - 0.6))
        assignments = _solve_bay_assignment_mip(
            prob_info,
            min(deadline, time.monotonic() + mip_budget),
            penalties or None,
        )
        if not assignments:
            break

        base_solution = _solution_from_bay_assignment(
            prob_info,
            assignments,
            deadline,
            use_mip_schedule=False,
        )
        if base_solution is None:
            break

        feedback = evaluate_edd_schedule_feedback(
            prob_info,
            base_solution,
            min(deadline, time.monotonic() + max(1.0, (deadline - time.monotonic()) * 0.35)),
        )
        candidate = feedback.solution
        score = _seed_solution_score(prob_info, candidate)
        if score < best_score:
            best_solution = candidate
            best_score = score
            best_proxy = feedback.proxy_result

        print(
            "[EDDMasterSeed] round "
            f"{round_idx + 1}/{max_rounds} score={score:.0f} "
            f"proxy_obj1={feedback.proxy_result.get('obj1')} "
            f"proxy_obj2={feedback.proxy_result.get('obj2')} "
            f"proxy_obj3={feedback.proxy_result.get('obj3')} "
            f"signals={len(feedback.conflict_signals)} "
            f"penalties={len(feedback.conflict_penalties)}",
            flush=True,
        )
        if not feedback.conflict_penalties:
            break
        next_penalties = merge_conflict_penalties(penalties, feedback.conflict_penalties, limit=128)
        if next_penalties == penalties:
            break
        penalties = next_penalties

    if best_solution is not None:
        print(
            "[EDDMasterSeed] done "
            f"elapsed={time.monotonic() - started_at:.2f}s best_score={best_score:.0f} "
            f"best_proxy={best_proxy}",
            flush=True,
        )
    return best_solution


def build_hierarchical_seed(prob_info: dict, deadline: float) -> dict | None:
    """Build a feasible-oriented seed from bay assignment and uniform placement."""

    if time.monotonic() >= deadline - 0.5:
        return None

    started_at = time.monotonic()
    print(
        "[HierarchicalSeed] start "
        f"budget={deadline - started_at:.2f}s blocks={len(prob_info.get('blocks', []))} "
        f"bays={len(prob_info.get('bays', []))}",
        flush=True,
    )
    mip_deadline = min(deadline, time.monotonic() + max(1.0, (deadline - time.monotonic()) * 0.45))
    assignments = _solve_bay_assignment_mip(prob_info, mip_deadline)
    if not assignments:
        print(
            f"[HierarchicalSeed] no bay assignment elapsed={time.monotonic() - started_at:.2f}s",
            flush=True,
        )
        return None

    solution = _solution_from_bay_assignment(prob_info, assignments, deadline)
    if solution is None:
        print(
            f"[HierarchicalSeed] no scheduled seed elapsed={time.monotonic() - started_at:.2f}s",
            flush=True,
        )
        return None
    edd_feedback = evaluate_edd_schedule_feedback(prob_info, solution, deadline)
    solution = edd_feedback.solution
    penalties = merge_conflict_penalties(
        edd_feedback.conflict_penalties,
        _soft_conflict_penalties(prob_info, solution),
        limit=96,
    )
    remaining = deadline - time.monotonic()
    print(
        "[HierarchicalSeed] base seed "
        f"elapsed={time.monotonic() - started_at:.2f}s remaining={remaining:.2f}s "
        f"edd_signals={len(edd_feedback.conflict_signals)} feedback_penalties={len(penalties)}",
        flush=True,
    )
    if penalties and remaining > 2.0:
        feedback_deadline = min(deadline, time.monotonic() + max(1.0, (deadline - time.monotonic()) * 0.4))
        print(
            "[HierarchicalSeed] feedback run "
            f"budget={feedback_deadline - time.monotonic():.2f}s soft_penalties={len(penalties)}",
            flush=True,
        )
        feedback_assignments = _solve_bay_assignment_mip(prob_info, feedback_deadline, penalties)
        if feedback_assignments:
            feedback_solution = _solution_from_bay_assignment(prob_info, feedback_assignments, deadline)
            if feedback_solution is not None:
                base_score = _seed_solution_score(prob_info, solution)
                feedback_score = _seed_solution_score(prob_info, feedback_solution)
                print(
                    "[HierarchicalSeed] feedback score "
                    f"base={base_score:.0f} feedback={feedback_score:.0f}",
                    flush=True,
                )
            if feedback_solution is not None and feedback_score < base_score:
                print(
                    f"[HierarchicalSeed] feedback accepted elapsed={time.monotonic() - started_at:.2f}s",
                    flush=True,
                )
                return feedback_solution
        print(
            f"[HierarchicalSeed] feedback rejected elapsed={time.monotonic() - started_at:.2f}s",
            flush=True,
        )
    elif penalties:
        print(
            f"[HierarchicalSeed] feedback skipped: remaining={remaining:.2f}s",
            flush=True,
        )
    return solution


def _solve_bay_assignment_mip(
    prob_info: dict,
    deadline: float,
    conflict_penalties: list[tuple[int, int, int, float]] | None = None,
) -> list[int] | None:
    """Use Gurobi, when available, for obj2/obj3-focused bay assignment."""

    if time.monotonic() >= deadline - 0.2:
        return None
    try:
        import gurobipy as gp
    except Exception as exc:
        print(f"[HierarchicalSeed] Gurobi import failed: {exc}", flush=True)
        return None

    blocks = prob_info["blocks"]
    bays = prob_info["bays"]
    n_blocks = len(blocks)
    n_bays = len(bays)
    if n_blocks == 0 or n_bays == 0:
        return None

    try:
        model = gp.Model("ogc_bay_assignment")
        model.Params.OutputFlag = 1
        model.Params.TimeLimit = max(1.0, deadline - time.monotonic())
        model.Params.MIPGap = 0.15
        model.Params.MIPFocus = 1

        z = model.addVars(n_blocks, n_bays, vtype=gp.GRB.BINARY, name="z")
        for block_id in range(n_blocks):
            model.addConstr(gp.quicksum(z[block_id, bay_id] for bay_id in range(n_bays)) == 1)
            for bay_id in range(n_bays):
                if not _has_fit_position(blocks[block_id], bays[bay_id]):
                    z[block_id, bay_id].UB = 0

        bay_areas = [float(bay["width"]) * float(bay["height"]) for bay in bays]
        avg_area = sum(bay_areas) / max(1, n_bays)
        bay_weights = [avg_area / area if area > 0 else 1.0 for area in bay_areas]
        normalized_loads = []
        for bay_id in range(n_bays):
            load = gp.quicksum(
                float(blocks[block_id]["workload"]) * z[block_id, bay_id]
                for block_id in range(n_blocks)
            )
            normalized_loads.append(load * bay_weights[bay_id])

        max_load = model.addVar(lb=0.0, name="max_load")
        min_load = model.addVar(lb=0.0, name="min_load")
        for load in normalized_loads:
            model.addConstr(max_load >= load)
            model.addConstr(min_load <= load)

        weights = prob_info.get("weights", {})
        w2 = float(weights.get("w2", 1.0))
        w3 = float(weights.get("w3", 1.0))
        preference_cost = gp.quicksum(
            float(max(blocks[block_id]["bay_preferences"]) - blocks[block_id]["bay_preferences"][bay_id])
            * z[block_id, bay_id]
            for block_id in range(n_blocks)
            for bay_id in range(n_bays)
        )
        conflict_cost = 0.0
        if conflict_penalties:
            conflict_terms = []
            for idx, (left_id, right_id, bay_id, penalty) in enumerate(conflict_penalties):
                if not (0 <= left_id < n_blocks and 0 <= right_id < n_blocks and 0 <= bay_id < n_bays):
                    continue
                same_bay = model.addVar(vtype=gp.GRB.BINARY, name=f"soft_conflict_{idx}")
                model.addConstr(same_bay >= z[left_id, bay_id] + z[right_id, bay_id] - 1)
                conflict_terms.append(float(penalty) * same_bay)
            if conflict_terms:
                conflict_cost = gp.quicksum(conflict_terms)
        model.setObjective(w2 * (max_load - min_load) + w3 * preference_cost + conflict_cost, gp.GRB.MINIMIZE)
        model.optimize()
        if model.SolCount <= 0:
            return None
        return [
            max(range(n_bays), key=lambda bay_id: float(z[block_id, bay_id].X))
            for block_id in range(n_blocks)
        ]
    except Exception as exc:
        print(f"[HierarchicalSeed] Gurobi bay-assignment failed: {exc}", flush=True)
        return None


def _solution_from_bay_assignment(
    prob_info: dict,
    assignments: list[int],
    deadline: float,
    *,
    use_mip_schedule: bool = True,
) -> dict | None:
    placements = _uniform_serial_placements(prob_info, assignments, deadline)
    if len(placements) != len(prob_info.get("blocks", [])):
        return None
    solution = {"operations": _build_operations(placements)}
    if not use_mip_schedule:
        return solution
    scheduled = reschedule_fixed_placements(prob_info, solution, deadline)
    return scheduled or solution


def _uniform_serial_placements(prob_info: dict, assignments: list[int], deadline: float) -> list[Placement]:
    blocks = prob_info["blocks"]
    bays = prob_info["bays"]
    by_bay: list[list[int]] = [[] for _ in bays]
    for block_id, bay_id in enumerate(assignments):
        if 0 <= int(bay_id) < len(bays):
            by_bay[int(bay_id)].append(block_id)

    placements: list[Placement] = []
    for bay_id, block_ids in enumerate(by_bay):
        if time.monotonic() >= deadline - 0.1:
            return placements
        ordered = sorted(
            block_ids,
            key=lambda block_id: (
                int(blocks[block_id]["due_date"]),
                int(blocks[block_id]["release_time"]),
                int(blocks[block_id]["processing_time"]),
                block_id,
            ),
        )
        positions = _uniform_position_plan(prob_info, bay_id, ordered)
        available = 0
        for block_id in ordered:
            block = blocks[block_id]
            position = positions.get(block_id) or _best_fit_position(block, bays[bay_id], 0, 0)
            if position is None:
                return placements
            orient_idx, x, y = position
            entry_time = max(int(block["release_time"]), available)
            exit_time = entry_time + int(block["processing_time"])
            placements.append(
                Placement(
                    block_id=block_id,
                    bay_id=bay_id,
                    x=int(x),
                    y=int(y),
                    orient_idx=int(orient_idx),
                    entry_time=int(entry_time),
                    exit_time=int(exit_time),
                )
            )
            available = exit_time
    return placements


def _uniform_position_plan(prob_info: dict, bay_id: int, block_ids: list[int]) -> dict[int, tuple[int, int, int]]:
    blocks = prob_info["blocks"]
    bay = prob_info["bays"][bay_id]
    if not block_ids:
        return {}

    aspect = max(1.0, float(bay["width"]) / max(1.0, float(bay["height"])))
    cols = max(1, math.ceil(math.sqrt(len(block_ids) * aspect)))
    rows = max(1, math.ceil(len(block_ids) / cols))
    cell_w = max(1.0, float(bay["width"]) / cols)
    cell_h = max(1.0, float(bay["height"]) / rows)
    result: dict[int, tuple[int, int, int]] = {}

    for idx, block_id in enumerate(block_ids):
        col = idx % cols
        row = idx // cols
        center_x = (col + 0.5) * cell_w
        center_y = (row + 0.5) * cell_h
        position = _best_fit_position(blocks[block_id], bay, center_x, center_y)
        if position is not None:
            result[block_id] = position
    result.update(_shelf_protected_position_plan(prob_info, bay_id, block_ids))
    return result


def _shelf_protected_position_plan(prob_info: dict, bay_id: int, block_ids: list[int]) -> dict[int, tuple[int, int, int]]:
    """Place the earliest-due prefix without AABB overlap when the bay has room."""

    from utils import Bay, Block

    blocks = prob_info["blocks"]
    bay_data = prob_info["bays"][bay_id]
    bay = Bay.from_dict(bay_data, bay_id)
    result: dict[int, tuple[int, int, int]] = {}
    cursor_x = 0.0
    row_y = 0.0
    row_height = 0.0
    max_prefix = min(len(block_ids), max(12, int(math.sqrt(len(block_ids)) * 4)))

    for block_id in block_ids[:max_prefix]:
        placed = _shelf_try_place_block(blocks[block_id], bay, bay_data, cursor_x, row_y)
        if placed is None:
            if row_height <= 0:
                break
            row_y += row_height
            cursor_x = 0.0
            row_height = 0.0
            placed = _shelf_try_place_block(blocks[block_id], bay, bay_data, cursor_x, row_y)
        if placed is None:
            break

        orient_idx, x, y, width, height = placed
        block = Block(block_id, blocks[block_id], x=int(x), y=int(y), orient_idx=int(orient_idx))
        if not bay.contains_block(block):
            break
        result[block_id] = (int(orient_idx), int(x), int(y))
        bbox = block.bounding_rect()
        cursor_x = float(bbox[2])
        row_height = max(row_height, float(bbox[3]) - row_y)
    return result


def _shelf_try_place_block(block_data: dict, bay, bay_data: dict, cursor_x: float, row_y: float):
    candidates = []
    for orient_idx in range(len(block_data.get("shape", []))):
        bbox = orientation_bbox(block_data, orient_idx)
        width = bbox[2] - bbox[0]
        height = bbox[3] - bbox[1]
        if width > float(bay_data["width"]) + 1e-6 or height > float(bay_data["height"]) + 1e-6:
            continue
        x = int(math.ceil(cursor_x - bbox[0]))
        y = int(math.ceil(row_y - bbox[1]))
        right = x + bbox[2]
        top = y + bbox[3]
        if right > float(bay_data["width"]) + 1e-6 or top > float(bay_data["height"]) + 1e-6:
            continue
        candidates.append((height, width, orient_idx, x, y))
    if not candidates:
        return None
    height, width, orient_idx, x, y = min(candidates)
    return orient_idx, x, y, width, height


def _has_fit_position(block_data: dict, bay_data: dict) -> bool:
    return _best_fit_position(block_data, bay_data, 0, 0) is not None


def _best_fit_position(block_data: dict, bay_data: dict, center_x: float, center_y: float) -> tuple[int, int, int] | None:
    from utils import Bay, Block

    bay = Bay.from_dict(bay_data, 0)
    best = None
    for orient_idx in range(len(block_data.get("shape", []))):
        bbox = orientation_bbox(block_data, orient_idx)
        width = bbox[2] - bbox[0]
        height = bbox[3] - bbox[1]
        x = int(round(center_x - width / 2.0 - bbox[0]))
        y = int(round(center_y - height / 2.0 - bbox[1]))
        x = max(0, min(x, int(math.floor(float(bay_data["width"]) - width))))
        y = max(0, min(y, int(math.floor(float(bay_data["height"]) - height))))
        candidate = Block(-1, block_data, x=int(x), y=int(y), orient_idx=int(orient_idx))
        if not bay.contains_block(candidate):
            repaired = _repair_inside_bay(bay, block_data, orient_idx, x, y)
            if repaired is None:
                continue
            x, y = repaired
            candidate = Block(-1, block_data, x=int(x), y=int(y), orient_idx=int(orient_idx))
        if not bay.contains_block(candidate):
            continue
        key = (width * height, max(width, height), orient_idx)
        if best is None or key < best[0]:
            best = (key, orient_idx, x, y)
    if best is None:
        return None
    return (int(best[1]), int(best[2]), int(best[3]))


def _repair_inside_bay(bay, block_data: dict, orient_idx: int, x: int, y: int) -> tuple[int, int] | None:
    from utils import Block

    probes = [(0, 0)]
    for radius in (1, 2, 3, 5, 8, 13, 21, 34):
        probes.extend(
            [
                (-radius, 0),
                (radius, 0),
                (0, -radius),
                (0, radius),
                (-radius, -radius),
                (-radius, radius),
                (radius, -radius),
                (radius, radius),
            ]
        )
    probes.append((-x, -y))
    for dx, dy in probes:
        candidate_x = max(0, int(x + dx))
        candidate_y = max(0, int(y + dy))
        block = Block(-1, block_data, x=candidate_x, y=candidate_y, orient_idx=orient_idx)
        if bay.contains_block(block):
            return candidate_x, candidate_y
    return None


def _build_operations(placements: list[Placement]) -> dict:
    events: dict[int, list[dict]] = {}
    for placement in placements:
        events.setdefault(int(placement.exit_time), []).append(
            {"type": "EXIT", "block_id": int(placement.block_id), "bay_id": int(placement.bay_id)}
        )
        events.setdefault(int(placement.entry_time), []).append(
            {
                "type": "ENTRY",
                "block_id": int(placement.block_id),
                "bay_id": int(placement.bay_id),
                "x": int(placement.x),
                "y": int(placement.y),
                "orient_idx": int(placement.orient_idx),
            }
        )

    return {
        str(time_idx): sorted(events[time_idx], key=lambda op: (0 if op["type"] == "EXIT" else 1, op["block_id"]))
        for time_idx in sorted(events)
    }


def _soft_conflict_penalties(prob_info: dict, solution: dict, limit: int = 48) -> list[tuple[int, int, int, float]]:
    assignments = _assignments_from_solution(solution)
    blocks = prob_info["blocks"]
    w1 = float(prob_info.get("weights", {}).get("w1", 1.0))
    penalties: dict[tuple[int, int, int], float] = {}

    tardy_ids = sorted(
        (
            block_id
            for block_id, row in assignments.items()
            if int(row["exit_time"]) > int(blocks[block_id]["due_date"])
        ),
        key=lambda block_id: (
            int(assignments[block_id]["exit_time"]) - int(blocks[block_id]["due_date"]),
            -int(blocks[block_id]["due_date"]),
        ),
        reverse=True,
    )[:24]

    for victim_id in tardy_ids:
        victim = assignments[victim_id]
        victim_due = int(blocks[victim_id]["due_date"])
        victim_tardy = int(victim["exit_time"]) - victim_due
        bay_id = int(victim["bay_id"])
        for other_id, other in assignments.items():
            if other_id == victim_id or int(other["bay_id"]) != bay_id:
                continue
            if int(blocks[other_id]["due_date"]) <= victim_due:
                continue
            if not same_bay_pair_needs_time_separation(prob_info, assignments, victim_id, other_id):
                continue
            left_id, right_id = sorted((int(victim_id), int(other_id)))
            key = (left_id, right_id, bay_id)
            expected_units = min(3.0, max(1.0, float(victim_tardy)))
            penalties[key] = min(w1 * 6.0, penalties.get(key, 0.0) + w1 * expected_units)

    ranked = sorted(
        ((penalty, left_id, right_id, bay_id) for (left_id, right_id, bay_id), penalty in penalties.items()),
        reverse=True,
    )
    return [(left_id, right_id, bay_id, penalty) for penalty, left_id, right_id, bay_id in ranked[:limit]]


def _assignments_from_solution(solution: dict) -> dict[int, dict]:
    assignments: dict[int, dict] = {}
    entry_seq = 0
    for time_key, operations in sorted(solution.get("operations", {}).items(), key=lambda item: int(item[0])):
        time_idx = int(time_key)
        for op in operations:
            block_id = int(op["block_id"])
            assignments.setdefault(block_id, {"block_id": block_id})
            if op["type"] == "ENTRY":
                assignments[block_id].update(
                    {
                        "bay_id": int(op["bay_id"]),
                        "x": int(op.get("x", 0)),
                        "y": int(op.get("y", 0)),
                        "orient_idx": int(op.get("orient_idx", 0)),
                        "entry_time": time_idx,
                        "_seq": entry_seq,
                    }
                )
                entry_seq += 1
            elif op["type"] == "EXIT":
                assignments[block_id].update({"bay_id": int(op["bay_id"]), "exit_time": time_idx})
    return {bid: data for bid, data in assignments.items() if "entry_time" in data and "exit_time" in data}


def _proxy_objective(prob_info: dict, solution: dict) -> float:
    assignments = _assignments_from_solution(solution)
    blocks = prob_info["blocks"]
    bays = prob_info["bays"]
    weights = prob_info.get("weights", {})
    w1 = float(weights.get("w1", 1.0))
    w2 = float(weights.get("w2", 1.0))
    w3 = float(weights.get("w3", 1.0))
    obj1 = 0.0
    obj3 = 0.0
    bay_loads = [0.0 for _ in bays]
    for block_id, row in assignments.items():
        bay_id = int(row["bay_id"])
        block = blocks[block_id]
        obj1 += max(0.0, int(row["exit_time"]) - int(block["due_date"]))
        bay_loads[bay_id] += float(block["workload"])
        preferences = block["bay_preferences"]
        obj3 += max(preferences) - preferences[bay_id]
    bay_areas = [float(bay["width"]) * float(bay["height"]) for bay in bays]
    avg_area = sum(bay_areas) / max(1, len(bays))
    bay_weights = [avg_area / area if area > 0 else 1.0 for area in bay_areas]
    obj2 = 0.0
    for left in range(len(bays)):
        for right in range(len(bays)):
            if left != right:
                obj2 = max(obj2, abs(bay_weights[left] * bay_loads[left] - bay_weights[right] * bay_loads[right]))
    return w1 * obj1 + w2 * int(obj2) + w3 * obj3


def _seed_solution_score(prob_info: dict, solution: dict) -> float:
    try:
        from utils import check_feasibility

        result = check_feasibility(prob_info, solution)
        if result.get("feasible") and result.get("objective") is not None:
            return float(result["objective"])
    except Exception:
        pass
    return _proxy_objective(prob_info, solution)
