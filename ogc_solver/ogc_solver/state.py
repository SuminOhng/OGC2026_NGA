"""Small data and geometry helpers independent from the provided utils.py."""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Placement:
    block_id: int
    bay_id: int
    x: int
    y: int
    orient_idx: int
    entry_time: int
    exit_time: int


def resolve_layers(raw_layers: list) -> list:
    """Return explicit non-empty layer polygons."""

    return [list(layer) for layer in raw_layers if layer]


def orientation_bbox(block_data: dict, orient_idx: int) -> tuple[float, float, float, float]:
    """Return the union bounding box for one block orientation."""

    layers = resolve_layers(block_data["shape"][orient_idx].get("layers", []))
    vertices = [vertex for layer in layers for vertex in layer]
    if not vertices:
        return (0.0, 0.0, 1.0, 1.0)

    xs = [vertex[0] for vertex in vertices]
    ys = [vertex[1] for vertex in vertices]
    return (min(xs), min(ys), max(xs), max(ys))


def lower_left_integer_position(
    bbox: tuple[float, float, float, float],
) -> tuple[int, int]:
    """Place a bounding box against the bay lower-left boundary."""

    min_x, min_y, _, _ = bbox
    return (max(0, math.ceil(-min_x)), max(0, math.ceil(-min_y)))


def fits_in_bay(
    bay_data: dict,
    bbox: tuple[float, float, float, float],
    x: int,
    y: int,
) -> bool:
    """Return True if the placed orientation bounding box stays inside a bay."""

    _, _, max_x, max_y = bbox
    return (
        x + max_x <= bay_data["width"] + 1e-6
        and y + max_y <= bay_data["height"] + 1e-6
    )


def iter_feasible_orientations(block_data: dict, bay_data: dict):
    """Yield simple lower-left placements that fit inside a bay."""

    for orient_idx in range(len(block_data.get("shape", []))):
        bbox = orientation_bbox(block_data, orient_idx)
        x, y = lower_left_integer_position(bbox)
        if fits_in_bay(bay_data, bbox, x, y):
            yield orient_idx, x, y
