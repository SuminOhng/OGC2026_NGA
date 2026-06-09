"""Objective-related helper functions."""

from __future__ import annotations


def tardiness(block_data: dict, exit_time: int) -> int:
    return max(0, int(exit_time) - int(block_data["due_date"]))


def preference_penalty(block_data: dict, bay_id: int) -> float:
    preferences = block_data.get("bay_preferences", [])
    if not preferences:
        return 0.0
    return max(preferences) - preferences[bay_id]
