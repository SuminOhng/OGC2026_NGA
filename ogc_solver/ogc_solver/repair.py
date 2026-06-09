"""Repair hooks for future infeasibility handling."""

from __future__ import annotations


def repair_solution(prob_info: dict, solution: dict, deadline: float | None = None) -> dict:
    """Return the solution unchanged until dedicated repair logic is added."""

    return solution
