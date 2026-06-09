"""Planner entrypoints."""

from __future__ import annotations

from .heuristics.greedy import sequential_empty_bay


def build_initial_solution(prob_info: dict, deadline: float | None = None) -> dict:
    """Build an initial solution with the configured baseline strategy."""

    return sequential_empty_bay(prob_info, deadline=deadline)
