---
name: ogc-caveman
description: Simplify OGC solver ideas into blunt baselines, invariants, and sanity checks. Use when Codex needs a caveman/cavman pass, to reduce complexity, challenge assumptions, find obvious feasible moves, or design quick experiments before or after coding.
---

# OGC Caveman

## Overview

Use this skill to strip an OGC solver idea down to the simplest thing that can
work, fail, or be measured. It is intentionally skeptical of cleverness until a
small baseline and a quick check exist.

## Caveman Pass

Ask these questions before trusting a complex plan:

- What is the one invariant that must never break?
- What is the dumbest feasible baseline?
- What is the smallest instance or one-block case that exposes the issue?
- What operation would make the bay empty and therefore easy?
- What integer rounding or ordering rule can silently break feasibility?
- What metric proves the change helped?
- What assumption would be embarrassing if false?

## Output Style

- Name the simplest baseline.
- Name the likely failure mode.
- Name the smallest test.
- Name the next code change only after the above are clear.

## OGC-Specific Checks

- Empty-bay sequential placement is the fallback mental model.
- Every block must have exactly one `ENTRY` and one `EXIT`.
- `EXIT` comes before `ENTRY` at the same time key.
- `x`, `y`, `orient_idx`, `block_id`, and `bay_id` must be integers.
- A placement that is fast but infeasible is worse than a slower feasible baseline.
- Hidden instances may differ in size and shape distribution, so avoid hard-coding training quirks.

## When Coding Follows

Hand the coding step a tiny requirement: the exact invariant, the exact file,
and the exact smoke test. If the idea cannot be stated that way, simplify again.
