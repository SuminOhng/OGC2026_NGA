"""Fast conservative feasibility oracle for local ALNS candidates.

The official ``utils.check_feasibility`` remains the final authority.  This
module only rejects clearly bad local candidates quickly and computes the proxy
objective for candidates that pass local exact checks.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable

from .state import resolve_layers


def _infeasible_result(stage: int = 5) -> dict:
    return {
        "feasible": False,
        "stage": stage,
        "violations": [],
        "objective": None,
        "obj1": None,
        "obj2": None,
        "obj3": None,
    }


@dataclass
class FastOracleStats:
    calls: int = 0
    feasible_reports: int = 0
    official_verifications: int = 0
    official_mismatches: int = 0
    reject_missing: int = 0
    reject_time: int = 0
    reject_boundary: int = 0
    reject_collision: int = 0
    reject_entry: int = 0
    reject_exit: int = 0
    bbox_pair_skips: int = 0
    layer_pair_skips: int = 0
    exact_collision_calls: int = 0
    collision_cache_hits: int = 0


class FastFeasibilityOracle:
    """Local exact checker with cheap broad-phase filters."""

    def __init__(self, prob_info: dict, *, verify_interval: int = 10):
        self.prob_info = prob_info
        self.blocks = prob_info.get("blocks", [])
        self.bays_data = prob_info.get("bays", [])
        self.verify_interval = max(0, int(verify_interval))
        self.stats = FastOracleStats()
        self.orientation_bounds: dict[tuple[int, int], tuple[float, float, float, float]] = {}
        self.layer_bounds: dict[tuple[int, int], list[tuple[float, float, float, float]]] = {}
        self._collision_cache: dict[tuple, bool] = {}
        self._precompute_bounds()

    def candidate_result(
        self,
        incumbent_assignments: dict[int, dict],
        candidate_assignments: dict[int, dict],
        changed_ids: Iterable[int],
    ) -> dict:
        self.stats.calls += 1
        try:
            local_ok = self.local_feasible(incumbent_assignments, candidate_assignments, set(changed_ids))
        except Exception:
            return _infeasible_result(stage=5)
        if not local_ok:
            return _infeasible_result(stage=5)
        self.stats.feasible_reports += 1
        return self.objective_result(candidate_assignments)

    def should_verify_with_official(self, result: dict) -> bool:
        if self.verify_interval <= 0 or not result.get("feasible"):
            return False
        if self.stats.feasible_reports == 1:
            return True
        return self.stats.feasible_reports % self.verify_interval == 0

    def record_official_verification(self, oracle_result: dict, official_result: dict) -> None:
        self.stats.official_verifications += 1
        if bool(oracle_result.get("feasible")) and not bool(official_result.get("feasible")):
            self.stats.official_mismatches += 1

    def summary(self) -> dict:
        return {
            "oracle_calls": self.stats.calls,
            "oracle_feasible": self.stats.feasible_reports,
            "oracle_official_verifications": self.stats.official_verifications,
            "oracle_mismatches": self.stats.official_mismatches,
            "oracle_reject_missing": self.stats.reject_missing,
            "oracle_reject_time": self.stats.reject_time,
            "oracle_reject_boundary": self.stats.reject_boundary,
            "oracle_reject_collision": self.stats.reject_collision,
            "oracle_reject_entry": self.stats.reject_entry,
            "oracle_reject_exit": self.stats.reject_exit,
            "oracle_bbox_pair_skips": self.stats.bbox_pair_skips,
            "oracle_layer_pair_skips": self.stats.layer_pair_skips,
            "oracle_exact_collision_calls": self.stats.exact_collision_calls,
            "oracle_collision_cache_hits": self.stats.collision_cache_hits,
        }

    def local_feasible(
        self,
        incumbent_assignments: dict[int, dict],
        candidate_assignments: dict[int, dict],
        changed_ids: set[int],
    ) -> bool:
        if len(candidate_assignments) != len(self.blocks):
            self.stats.reject_missing += 1
            return False

        affected_bays = self._affected_bays(incumbent_assignments, candidate_assignments, changed_ids)
        if not affected_bays:
            self.stats.reject_missing += 1
            return False

        try:
            from utils import Bay, Block, check_entry, check_exit
        except Exception:
            self.stats.reject_missing += 1
            return False

        bays = [Bay.from_dict(data, bay_id) for bay_id, data in enumerate(self.bays_data)]
        rows_by_bay: dict[int, list[dict]] = {bay_id: [] for bay_id in affected_bays}
        for block_id, assignment in candidate_assignments.items():
            row = self._build_row(block_id, assignment, bays, Block)
            if row is None:
                return False
            if row["bay_id"] in affected_bays:
                rows_by_bay[row["bay_id"]].append(row)

        for bay_id, rows in rows_by_bay.items():
            bay = bays[bay_id]
            if not self._affected_bay_feasible(bay, rows, check_entry, check_exit):
                return False
        return True

    def objective_result(self, assignments: dict[int, dict]) -> dict:
        weights = self.prob_info.get("weights", {})
        w1 = float(weights.get("w1", 1.0))
        w2 = float(weights.get("w2", 1.0))
        w3 = float(weights.get("w3", 1.0))

        obj1 = 0.0
        obj3 = 0.0
        bay_loads = [0.0 for _ in self.bays_data]
        for block_id, assignment in assignments.items():
            bay_id = int(assignment["bay_id"])
            if bay_id < 0 or bay_id >= len(self.bays_data):
                return _infeasible_result(stage=5)
            block_data = self.blocks[block_id]
            obj1 += max(0.0, int(assignment["exit_time"]) - int(block_data["due_date"]))
            bay_loads[bay_id] += float(block_data["workload"])
            preferences = block_data["bay_preferences"]
            obj3 += max(preferences) - preferences[bay_id]

        bay_areas = [float(bay["width"]) * float(bay["height"]) for bay in self.bays_data]
        avg_area = sum(bay_areas) / max(1, len(bay_areas))
        bay_weights = [avg_area / area if area > 0 else 1.0 for area in bay_areas]
        if len(self.bays_data) >= 2:
            obj2 = math.floor(
                max(
                    abs(bay_weights[first] * bay_loads[first] - bay_weights[second] * bay_loads[second])
                    for first in range(len(self.bays_data))
                    for second in range(len(self.bays_data))
                    if first != second
                )
            )
        else:
            obj2 = 0.0
        objective = w1 * obj1 + w2 * obj2 + w3 * obj3
        return {
            "feasible": True,
            "stage": 5,
            "violations": [],
            "objective": objective,
            "obj1": obj1,
            "obj2": obj2,
            "obj3": obj3,
        }

    def _precompute_bounds(self) -> None:
        for block_id, block_data in enumerate(self.blocks):
            for orient_idx, orientation in enumerate(block_data.get("shape", [])):
                layers = resolve_layers(orientation.get("layers", []))
                layer_boxes = [_polygon_bbox(layer) for layer in layers]
                self.layer_bounds[(block_id, orient_idx)] = layer_boxes
                if layer_boxes:
                    self.orientation_bounds[(block_id, orient_idx)] = _union_bbox(layer_boxes)
                else:
                    self.orientation_bounds[(block_id, orient_idx)] = (0.0, 0.0, 0.0, 0.0)

    def _affected_bays(
        self,
        incumbent_assignments: dict[int, dict],
        candidate_assignments: dict[int, dict],
        changed_ids: set[int],
    ) -> set[int]:
        affected = set()
        for block_id in changed_ids:
            if block_id in incumbent_assignments and "bay_id" in incumbent_assignments[block_id]:
                affected.add(int(incumbent_assignments[block_id]["bay_id"]))
            if block_id in candidate_assignments and "bay_id" in candidate_assignments[block_id]:
                affected.add(int(candidate_assignments[block_id]["bay_id"]))
        return {bay_id for bay_id in affected if 0 <= bay_id < len(self.bays_data)}

    def _build_row(self, block_id: int, assignment: dict, bays: list, Block) -> dict | None:
        required = {"block_id", "bay_id", "x", "y", "orient_idx", "entry_time", "exit_time"}
        if not required.issubset(assignment):
            self.stats.reject_missing += 1
            return None
        if block_id < 0 or block_id >= len(self.blocks):
            self.stats.reject_missing += 1
            return None

        block_data = self.blocks[block_id]
        bay_id = int(assignment["bay_id"])
        orient_idx = int(assignment["orient_idx"])
        if bay_id < 0 or bay_id >= len(bays) or orient_idx < 0 or orient_idx >= len(block_data.get("shape", [])):
            self.stats.reject_missing += 1
            return None

        entry_time = int(assignment["entry_time"])
        exit_time = int(assignment["exit_time"])
        if entry_time < int(block_data["release_time"]):
            self.stats.reject_time += 1
            return None
        if exit_time - entry_time != int(block_data["processing_time"]):
            self.stats.reject_time += 1
            return None

        block = Block(
            block_id,
            block_data,
            x=int(assignment["x"]),
            y=int(assignment["y"]),
            orient_idx=orient_idx,
        )
        bay = bays[bay_id]
        if not bay.contains_block(block):
            self.stats.reject_boundary += 1
            return None

        return {
            "block_id": block_id,
            "assignment": assignment,
            "block": block,
            "bay_id": bay_id,
            "entry_time": entry_time,
            "exit_time": exit_time,
            "entry_seq": int(assignment.get("_seq", block_id)),
            "placed_bbox": self._placed_bbox(block_id, orient_idx, int(assignment["x"]), int(assignment["y"])),
            "placed_layer_boxes": self._placed_layer_boxes(
                block_id,
                orient_idx,
                int(assignment["x"]),
                int(assignment["y"]),
            ),
        }

    def _affected_bay_feasible(self, bay, rows: list[dict], check_entry, check_exit) -> bool:
        for index, row in enumerate(rows):
            for other in rows[index + 1 :]:
                if not _time_overlaps(row["entry_time"], row["exit_time"], other["entry_time"], other["exit_time"]):
                    continue
                if self.maybe_pair_collision(bay, row, other):
                    self.stats.reject_collision += 1
                    return False

            present_at_entry = [
                other
                for other in rows
                if other is not row and _is_present_before_entry(other, row["entry_time"], row["entry_seq"])
            ]
            relevant_entry = self.relevant_for_crane(row, present_at_entry)
            if check_entry(bay, [other["block"] for other in relevant_entry], row["block"], fast=True):
                self.stats.reject_entry += 1
                return False

            present_at_exit = [
                other
                for other in rows
                if other is not row and _is_present_at_exit_before_removal(
                    other,
                    row["exit_time"],
                    row["block_id"],
                )
            ]
            relevant_exit = self.relevant_for_crane(row, present_at_exit)
            if check_exit(bay, [row["block"], *[other["block"] for other in relevant_exit]], row["block"], fast=True):
                self.stats.reject_exit += 1
                return False
        return True

    def maybe_pair_collision(self, bay, row_a: dict, row_b: dict) -> bool:
        if not _bbox_overlap(row_a["placed_bbox"], row_b["placed_bbox"]):
            self.stats.bbox_pair_skips += 1
            return False
        if not _same_layer_bbox_overlap(row_a["placed_layer_boxes"], row_b["placed_layer_boxes"]):
            self.stats.layer_pair_skips += 1
            return False

        key = self._collision_key(bay, row_a, row_b)
        cached = self._collision_cache.get(key)
        if cached is not None:
            self.stats.collision_cache_hits += 1
            return cached

        from utils import check_collisions

        self.stats.exact_collision_calls += 1
        result = bool(check_collisions(bay, [row_a["block"], row_b["block"]]))
        if len(self._collision_cache) >= 200_000:
            self._collision_cache.clear()
        self._collision_cache[key] = result
        return result

    def relevant_for_crane(self, moving_row: dict, present_rows: list[dict]) -> list[dict]:
        relevant = []
        for row in present_rows:
            if not _bbox_overlap(moving_row["placed_bbox"], row["placed_bbox"]):
                continue
            if _crane_layer_bbox_overlap(moving_row["placed_layer_boxes"], row["placed_layer_boxes"]):
                relevant.append(row)
        return relevant

    def _placed_bbox(self, block_id: int, orient_idx: int, x: int, y: int) -> tuple[float, float, float, float]:
        return _translate_bbox(self.orientation_bounds[(block_id, orient_idx)], x, y)

    def _placed_layer_boxes(
        self,
        block_id: int,
        orient_idx: int,
        x: int,
        y: int,
    ) -> list[tuple[float, float, float, float]]:
        return [_translate_bbox(bbox, x, y) for bbox in self.layer_bounds[(block_id, orient_idx)]]

    def _collision_key(self, bay, row_a: dict, row_b: dict) -> tuple:
        key_a = (
            int(row_a["block_id"]),
            int(row_a["assignment"]["x"]),
            int(row_a["assignment"]["y"]),
            int(row_a["assignment"]["orient_idx"]),
        )
        key_b = (
            int(row_b["block_id"]),
            int(row_b["assignment"]["x"]),
            int(row_b["assignment"]["y"]),
            int(row_b["assignment"]["orient_idx"]),
        )
        if key_b < key_a:
            key_a, key_b = key_b, key_a
        return (int(bay.id), key_a, key_b)


def _polygon_bbox(layer: list) -> tuple[float, float, float, float]:
    if not layer:
        return (0.0, 0.0, 0.0, 0.0)
    xs = [float(vertex[0]) for vertex in layer]
    ys = [float(vertex[1]) for vertex in layer]
    return (min(xs), min(ys), max(xs), max(ys))


def _union_bbox(boxes: list[tuple[float, float, float, float]]) -> tuple[float, float, float, float]:
    return (
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    )


def _translate_bbox(bbox: tuple[float, float, float, float], x: int, y: int) -> tuple[float, float, float, float]:
    return (bbox[0] + x, bbox[1] + y, bbox[2] + x, bbox[3] + y)


def _bbox_overlap(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> bool:
    return left[0] < right[2] and right[0] < left[2] and left[1] < right[3] and right[1] < left[3]


def _same_layer_bbox_overlap(
    left_layers: list[tuple[float, float, float, float]],
    right_layers: list[tuple[float, float, float, float]],
) -> bool:
    for left, right in zip(left_layers, right_layers):
        if _bbox_overlap(left, right):
            return True
    return False


def _crane_layer_bbox_overlap(
    moving_layers: list[tuple[float, float, float, float]],
    present_layers: list[tuple[float, float, float, float]],
) -> bool:
    for moving_idx, moving_bbox in enumerate(moving_layers):
        for present_idx in range(moving_idx, len(present_layers)):
            if _bbox_overlap(moving_bbox, present_layers[present_idx]):
                return True
    return False


def _time_overlaps(left_entry: int, left_exit: int, right_entry: int, right_exit: int) -> bool:
    return int(left_entry) < int(right_exit) and int(right_entry) < int(left_exit)


def _is_present_before_entry(row: dict, entry_time: int, entry_seq: int) -> bool:
    other_entry = int(row["entry_time"])
    other_exit = int(row["exit_time"])
    if other_entry < entry_time < other_exit:
        return True
    if other_entry == entry_time and other_exit > entry_time:
        return int(row["entry_seq"]) < int(entry_seq)
    return False


def _is_present_at_exit_before_removal(row: dict, exit_time: int, target_id: int) -> bool:
    other_entry = int(row["entry_time"])
    other_exit = int(row["exit_time"])
    if other_entry < exit_time < other_exit:
        return True
    if other_entry < exit_time and other_exit == exit_time:
        return int(row["block_id"]) > int(target_id)
    return False
